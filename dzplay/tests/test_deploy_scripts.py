"""Deploy scripts: they parse, no line continuation is broken (a `\\\\` at the end of a line is a literal
backslash argument, not a continuation — it made install.sh stop at `chmod` under `set -e`), every script
install.sh makes executable exists, and deploy/perms.sh finds and tightens sensitive files that are too open."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

import pytest

DEPLOY = Path(__file__).resolve().parent.parent / "deploy"
SCRIPTS = sorted(DEPLOY.glob("*.sh"))
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _code_lines(path: Path):
    """Lines outside here-documents, with their numbers."""
    end = None
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        yield n, line
        m = HEREDOC.search(line)
        if m and not line.lstrip().startswith("#"):
            end = m.group(2)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_scripts_parse_and_have_no_broken_continuation(script):
    assert subprocess.run(["bash", "-n", str(script)], capture_output=True).returncode == 0
    broken = [n for n, line in _code_lines(script) if re.search(r"(?<!\\)(\\\\)+\s*$", line)]
    assert not broken, f"{script.name}: line(s) {broken} end with an escaped backslash, not a continuation"


def test_install_makes_every_listed_script_executable():
    text = (DEPLOY / "install.sh").read_text()
    m = re.search(r"^chmod 700 ((?:[^\n]*\\\n)*[^\n]*)$", text, re.M)
    assert m, "chmod line not found"
    listed = re.findall(r"deploy/([\w.-]+\.sh)", m.group(1))
    assert "perms.sh" in listed and "v6-cleanup.sh" in listed
    for name in listed:
        assert (DEPLOY / name).exists(), name
    assert "deploy/perms.sh\" fix" in text  # install tightens permissions on every run


@pytest.mark.skipif(os.geteuid() != 0, reason="ownership checks need root (as on the server)")
def test_perms_finds_and_fixes_open_files_and_signing_keys(tmp_path):
    app = tmp_path / "src" / "dzplay"
    (app / "deploy").mkdir(parents=True)
    for s in SCRIPTS:
        (app / "deploy" / s.name).write_text(s.read_text())
        (app / "deploy" / s.name).chmod(0o755)
    env_file = app / ".env"
    env_file.write_text("SECRET_KEY=x\n")
    env_file.chmod(0o644)
    aside = app / ".env.before-restore-20260101T000000Z"
    aside.write_text("SECRET_KEY=old\n")
    aside.chmod(0o640)
    (app / ".env.example").write_text("SECRET_KEY=\n")
    (app / ".env.example").chmod(0o644)
    backups = tmp_path / "backups"
    backups.mkdir(mode=0o755)
    backups.chmod(0o755)
    (backups / "dzplay-1.sql.gpg").write_text("x")
    (backups / "dzplay-1.sql.gpg").chmod(0o644)
    home = tmp_path / "home" / "u"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh").chmod(0o755)
    (home / ".ssh" / "authorized_keys").write_text("ssh-ed25519 AAAA")
    (home / ".ssh" / "authorized_keys").chmod(0o644)
    sshd = tmp_path / "ssh"
    sshd.mkdir()
    (sshd / "ssh_host_ed25519_key").write_text("k")
    (sshd / "ssh_host_ed25519_key").chmod(0o644)
    cron = tmp_path / "cron"
    cron.mkdir()
    (cron / "dzplay-backup").write_text("x")
    (cron / "dzplay-backup").chmod(0o666)
    docker = tmp_path / "docker"
    (docker / "volumes").mkdir(parents=True)
    key = app / "android-signing" / "release.jks"
    key.parent.mkdir()
    key.write_text("k")
    env = {**os.environ, "APP_DIR": str(app), "SRC_DIR": str(app.parent), "BACKUP_DIR": str(backups),
           "PERMS_LOG_DIR": str(tmp_path / "log"), "PERMS_LIB_DIR": str(tmp_path / "lib"),
           "PERMS_CRON_DIR": str(cron), "PERMS_SSHD_DIR": str(sshd), "PERMS_HOMES": str(tmp_path / "home" / "*"),
           "PERMS_DOCKER_DIR": str(docker)}
    script = str(app / "deploy" / "perms.sh")

    def run(*args):
        return subprocess.run(["bash", script, *args], env=env, capture_output=True, text=True)

    r = run()
    assert r.returncode == 1
    for name in (".env is 644", "before-restore", "backups is 755", "sql.gpg is 644", "authorized_keys is 644",
                 "ssh_host_ed25519_key is 644", "dzplay-backup is 666", "release.jks"):
        assert name in r.stdout, (name, r.stdout)
    assert ".env.example" not in r.stdout
    assert oct(env_file.stat().st_mode & 0o777) == "0o644"  # check changes nothing

    run("fix")
    mode = lambda p: stat.S_IMODE(p.stat().st_mode)  # noqa: E731
    assert mode(env_file) == 0o600 and mode(aside) == 0o600 and mode(backups) == 0o700
    assert mode(backups / "dzplay-1.sql.gpg") == 0o600 and mode(home / ".ssh") == 0o700
    assert mode(home / ".ssh" / "authorized_keys") == 0o600 and mode(sshd / "ssh_host_ed25519_key") == 0o600
    assert mode(cron / "dzplay-backup") == 0o644 and mode(docker) & 0o066 == 0
    assert mode(app / ".env.example") == 0o644  # not a secret, left alone
    assert key.exists() and "release.jks" in run().stdout  # a signing key is reported, never deleted for you
    key.unlink()
    r = run()
    assert r.returncode == 0 and "All sensitive files have safe permissions." in r.stdout
    # never loosens: a stricter mode stays as it is
    env_file.chmod(0o400)
    run("fix")
    assert mode(env_file) == 0o400
