"""deploy/monitor.sh: alerts once per problem, recovery message, intrusion spikes (fake docker on PATH)."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

FAKE_DOCKER = r"""#!/usr/bin/env bash
# fake "docker compose": services from $FAKE_RUNNING, alerts appended to $FAKE_ALERTS
shift  # compose
case "$1 $2" in
  "ps --status") printf '%s\n' $FAKE_RUNNING ;;
  "config --services") printf '%s\n' app db redis caddy coturn media-worker ;;
  "exec -T")
    shift 2
    case "$1 $2" in
      "app python") [ "${FAKE_APP_SEND:-1}" = 1 ] || exit 1; printf '%s\n---\n' "${@: -1}" >> "$FAKE_ALERTS" ;;
      "app printenv") echo queue ;;
      "redis redis-cli") echo "${FAKE_HB:-1}" ;;
    esac ;;
esac
"""

ALL = "app db redis caddy coturn media-worker"


@pytest.fixture()
def box(tmp_path):
    if shutil.which("flock") is None:
        pytest.skip("flock missing")
    (tmp_path / "deploy").mkdir()
    shutil.copy(ROOT / "deploy" / "monitor.sh", tmp_path / "deploy" / "monitor.sh")
    (tmp_path / ".env").write_text("MONITOR_DISK_PCT=100\nMONITOR_LOAD_PER_CPU=1000\nMONITOR_MEM_PCT=100\n"
                                   "MONITOR_BANS_PER_HOUR=3\nMONITOR_REMIND_HOURS=6\n"
                                   "TELEGRAM_BOT_TOKEN=123:secret-token-value\nTELEGRAM_ADMIN_CHAT_ID=42\n")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "docker").write_text(FAKE_DOCKER)
    (bindir / "docker").chmod(0o755)
    (bindir / "curl").write_text("#!/bin/sh\ncat > \"$FAKE_CURL_STDIN\"; echo \"$@\" > \"$FAKE_CURL_ARGS\"; exit 0\n")
    (bindir / "curl").chmod(0o755)
    alerts = tmp_path / "alerts.txt"

    def run(cmd="check", running=ALL, **extra):
        env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "FAKE_RUNNING": running,
               "FAKE_ALERTS": str(alerts), "MONITOR_STATE_DIR": str(tmp_path / "state"),
               "MONITOR_F2B_LOG": str(tmp_path / "f2b.log"), "FAKE_CURL_STDIN": str(tmp_path / "curl.in"),
               "FAKE_CURL_ARGS": str(tmp_path / "curl.args"), **extra}
        res = subprocess.run(["bash", str(tmp_path / "deploy" / "monitor.sh"), cmd], env=env,
                             capture_output=True, text=True, timeout=30)
        assert res.returncode == 0, res.stderr
        return res

    def sent():
        return alerts.read_text().split("---\n")[:-1] if alerts.exists() else []

    return tmp_path, run, sent


def test_quiet_when_everything_is_fine(box):
    _, run, sent = box
    run()
    assert sent() == []
    assert "OK" in run("status").stdout


def test_service_down_alerts_once_then_recovers(box):
    _, run, sent = box
    run(running="app db redis caddy coturn")  # media-worker stopped
    assert len(sent()) == 1 and "media-worker" in sent()[0] and "مشكلة جديدة" in sent()[0]
    run(running="app db redis caddy coturn")  # still down: no duplicate before the reminder delay
    assert len(sent()) == 1
    run()
    assert len(sent()) == 2 and "عاد طبيعيًا" in sent()[1] and "media-worker" in sent()[1]
    run()
    assert len(sent()) == 2


def test_worker_heartbeat_and_disk(box):
    tmp, run, sent = box
    run(FAKE_HB="0")
    assert "عامل الوسائط" in sent()[0]
    (tmp / ".env").write_text((tmp / ".env").read_text().replace("MONITOR_DISK_PCT=100", "MONITOR_DISK_PCT=0"))
    run(FAKE_HB="0")
    assert "القرص" in sent()[1] and "عامل الوسائط" not in sent()[1]


def test_intrusion_spike_counts_only_the_last_hour(box):
    tmp, run, sent = box
    now, old = datetime.now(), datetime.now() - timedelta(hours=3)
    lines = [f"{old:%Y-%m-%d %H:%M:%S},1 fail2ban.actions [1]: NOTICE [sshd] Ban 10.0.0.{i}" for i in range(9)]
    lines += [f"{now:%Y-%m-%d %H:%M:%S},1 fail2ban.actions [1]: NOTICE [sshd] Ban 10.0.1.{i}" for i in range(2)]
    (tmp / "f2b.log").write_text("\n".join(lines) + "\n")
    run()
    assert sent() == []  # 2 bans in the last hour < 3
    with (tmp / "f2b.log").open("a") as fh:
        fh.write(f"{now:%Y-%m-%d %H:%M:%S},2 fail2ban.actions [1]: NOTICE [sshd] Ban 10.0.1.9\n")
    run()
    assert "3 عنوان IP" in sent()[0]


def test_direct_fallback_keeps_the_token_off_the_command_line(box):
    tmp, run, sent = box
    res = run(running="app db redis caddy coturn", FAKE_APP_SEND="0")
    assert sent() == [] and "sent (direct)" in res.stdout and "secret-token-value" not in res.stdout
    assert "secret-token-value" in (tmp / "curl.in").read_text()  # config on stdin
    assert "secret-token-value" not in (tmp / "curl.args").read_text()  # never in argv (ps)
    assert "media-worker" in (tmp / "curl.args").read_text()
