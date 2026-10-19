"""V6 phase 20: ready for the domain behind Cloudflare — APP_DOMAIN everywhere, the old address (301 except what the
installed app needs), the caller's own address check, nothing in the mail pointing at the server, and
deploy/cloudflare-ips.sh (the list checks, Caddy's trusted ranges, the Cloudflare-only firewall in DOCKER-USER).
The Caddy side (real client address from Cloudflare only, WebSocket, origin certificate) is e2e/run_cloudflare_e2e.sh."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

from app.config import Settings
from app.services import mailer
from tests.smtp_sink import SmtpSink

ROOT = Path(__file__).resolve().parent.parent
V4 = ["173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18", "108.162.192.0/18",
      "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17", "162.158.0.0/15", "104.16.0.0/13"]
V6 = ["2400:cb00::/32", "2606:4700::/32", "2803:f800::/32", "2405:b500::/32", "2a06:98c0::/29"]


# ----------------------------------------------------------------- one setting for the address


def test_app_domain_drives_the_public_url():
    s = Settings(APP_DOMAIN="HTTPS://Dalta.Example/", PUBLIC_URL="https://1-2-3-4.sslip.io", LEGACY_HOSTS="1-2-3-4.sslip.io, dalta.example")
    assert s.APP_DOMAIN == "dalta.example" and s.PUBLIC_URL == "https://dalta.example"
    assert s.legacy_hosts == {"1-2-3-4.sslip.io"} and s.mail_hostname == "dalta.example"
    assert Settings(APP_DOMAIN="dalta.example", PUBLIC_URL="https://dalta.example:8443").PUBLIC_URL == "https://dalta.example:8443"
    assert Settings(PUBLIC_URL="https://old.example").mail_hostname == "old.example"
    assert Settings().mail_hostname == "localhost"
    assert Settings(APP_DOMAIN="dalta.example/some/path").APP_DOMAIN == "dalta.example"
    for bad in ("dalta", "dalta..example", "-dalta.example", "dal ta.example", "1.2.3.4", "dalta.example:443"):
        with pytest.raises(ValueError):
            Settings(APP_DOMAIN=bad)


def test_links_use_the_domain(make_harness):
    hx = make_harness(APP_DOMAIN="dalta.example")
    page = hx.client().get("/download").text
    assert "https://dalta.example/download" in page


# ----------------------------------------------------------------- the old address during the move


@pytest.fixture
def moved(make_harness):
    return make_harness(APP_DOMAIN="dalta.example", LEGACY_HOSTS="1-2-3-4.sslip.io")


def test_old_address_redirects_everything_but_the_app(moved):
    c = moved.client()
    old = {"host": "1-2-3-4.sslip.io"}
    r = c.get("/?ref=abc", headers=old, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://dalta.example/?ref=abc"
    r = c.get("/test-panel", headers=old, follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"] == "https://dalta.example/test-panel"
    r = c.post("/api/auth/login", headers={**old, "X-DZ-Requested": "1"}, json={}, follow_redirects=False)
    assert r.status_code == 308 and r.headers["location"] == "https://dalta.example/api/auth/login"
    # still served there: the download page and the APK's update check, the files they load, the bot's webhook
    for path in ("/download", "/api/app/version", "/healthz", "/sw.js", "/css/app.css", "/js/app.js"):
        assert c.get(path, headers=old, follow_redirects=False).status_code == 200, path
    # the installed Android app (until its mandatory update moves it) keeps working on the old address
    app_ua = {**old, "user-agent": "Mozilla/5.0 (Linux; Android 14; wv) DZPLAYApp/3.2.0"}
    assert c.get("/api/me", headers=app_ua, follow_redirects=False).status_code != 301
    # the domain itself, and any other name, are served as usual
    assert c.get("/", headers={"host": "dalta.example"}, follow_redirects=False).status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200


def test_no_redirect_without_a_domain(make_harness):
    hx = make_harness(LEGACY_HOSTS="1-2-3-4.sslip.io")
    assert hx.client().get("/", headers={"host": "1-2-3-4.sslip.io"}, follow_redirects=False).status_code == 200


# ----------------------------------------------------------------- the caller's own address


def test_healthz_ip_is_the_callers_own_address(make_harness):
    hx = make_harness(TRUST_PROXY_HEADERS=True, TRUSTED_PROXY_COUNT=1)
    r = hx.client().get("/healthz/ip", headers={"X-Forwarded-For": "6.6.6.6, 198.51.100.7"})
    assert r.json() == {"ip": "198.51.100.7"} and r.headers["cache-control"] == "no-store"
    plain = make_harness(TRUST_PROXY_HEADERS=False)  # no proxy in front: the header is the caller's own words
    assert plain.client().get("/healthz/ip", headers={"X-Forwarded-For": "6.6.6.6"}).json()["ip"] != "6.6.6.6"


# ----------------------------------------------------------------- the mail does not point at the server


def test_mail_names_the_domain_not_the_machine():
    with SmtpSink() as sink:
        s = Settings(APP_DOMAIN="dalta.example", SMTP_HOST="127.0.0.1", SMTP_PORT=sink.port, SMTP_SECURITY="none",
                     SMTP_FROM="DALTA.BIT <noreply@dalta.example>")
        mailer.send_text(s, "a@example.com", "رمز", "النص", automatic=True)
        mailer.send_text(s, "b@example.com", "رد الدعم", "النص")
        first, second = sink.messages
        assert first["helo"] == "dalta.example"
        msg = first["msg"]
        assert msg["Message-ID"].endswith("@dalta.example>") and msg["Date"] and msg["Auto-Submitted"] == "auto-generated"
        assert second["msg"]["Auto-Submitted"] is None  # a support reply is written by a person
        s2 = Settings(APP_DOMAIN="dalta.example", SMTP_HOST="127.0.0.1", SMTP_PORT=sink.port, SMTP_SECURITY="none",
                      SMTP_FROM="no address")
        mailer.send_text(s2, "c@example.com", "x", "y")
        assert sink.messages[-1]["msg"]["Message-ID"].endswith("@dalta.example>")


# ----------------------------------------------------------------- deploy/cloudflare-ips.sh


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


FAKE_IPTABLES = r'''#!/usr/bin/env python3
"""iptables / ip6tables stand-in: keeps chains in a JSON file and logs every call."""
import json, os, sys
name = os.path.basename(sys.argv[0]); args = sys.argv[1:]
st_path = os.path.join(os.environ["FAKE_STATE"], name + ".json")
st = json.load(open(st_path)) if os.path.exists(st_path) else {"DOCKER-USER": []}
open(os.path.join(os.environ["FAKE_STATE"], "calls.log"), "a").write(name + " " + " ".join(args) + "\n")
op, chain, rule = args[0], (args[1] if len(args) > 1 else ""), args[2:]
code = 0
if op == "-n" and args[1] == "-L":
    code = 0 if args[2] in st else 1
elif op == "-N":
    code = 1 if chain in st else 0; st.setdefault(chain, [])
elif op == "-F":
    if chain in st: st[chain] = []
    else: code = 1
elif op == "-X":
    code = 0 if st.pop(chain, None) is not None else 1
elif op == "-A":
    st[chain].append(rule)
elif op == "-I":
    st[chain].insert(int(rule[0]) - 1, rule[1:])
elif op == "-C":
    code = 0 if rule in st.get(chain, []) else 1
elif op == "-D":
    if rule in st.get(chain, []): st[chain].remove(rule)
    else: code = 1
elif op == "-S":
    print("\n".join("-A %s %s" % (chain, " ".join(r)) for r in st.get(chain, [])))
json.dump(st, open(st_path, "w"))
sys.exit(code)
'''

FAKE_IP = r'''#!/bin/sh
case "$1" in
  -6) echo "2606:4700:4700::1111 from :: via fe80::1 dev eth0 proto ra src 2001:db8::5 metric 100 pref medium" ;;
  *) echo "1.1.1.1 via 10.0.0.1 dev eth0 src 10.0.0.5 uid 0" ;;
esac
'''

FAKE_DOCKER = r'''#!/bin/sh
echo "docker $*" >> "$FAKE_STATE/calls.log"
case "$*" in *"ps -q caddy"*) echo caddy123 ;; esac
exit 0
'''


@pytest.fixture
def cf(tmp_path):
    app = tmp_path / "app"
    (app / "deploy" / "caddy").mkdir(parents=True)
    shutil.copy(ROOT / "deploy" / "cloudflare-ips.sh", app / "deploy" / "cloudflare-ips.sh")
    bin_dir, state = tmp_path / "bin", tmp_path / "fake"
    bin_dir.mkdir()
    state.mkdir()
    for name in ("iptables", "ip6tables"):
        _exe(bin_dir / name, FAKE_IPTABLES)
    _exe(bin_dir / "ip", FAKE_IP)
    _exe(bin_dir / "docker", FAKE_DOCKER)
    lists = tmp_path / "lists"
    lists.mkdir()
    (lists / "v4").write_text("\n".join(V4) + "\n")
    (lists / "v6").write_text("\n".join(V6) + "\n")

    class CF:
        root, fake = app, state

        @staticmethod
        def env(text: str) -> None:
            (app / ".env").write_text(text)

        @staticmethod
        def run(*args: str, v4: Path | None = None, v6: Path | None = None) -> subprocess.CompletedProcess:
            env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_STATE": str(state),
                   "CF_STATE_DIR": str(tmp_path / "state"), "CF_IPS_V4_URL": f"file://{v4 or lists / 'v4'}",
                   "CF_IPS_V6_URL": f"file://{v6 or lists / 'v6'}"}
            return subprocess.run(["bash", str(app / "deploy" / "cloudflare-ips.sh"), *args], env=env,
                                  capture_output=True, text=True, timeout=60)

        @staticmethod
        def snippet() -> str:
            return (app / "deploy" / "caddy" / "cloudflare-ips.caddy").read_text()

        @staticmethod
        def chains(name: str = "iptables") -> dict:
            p = state / f"{name}.json"
            return json.loads(p.read_text()) if p.exists() else {}

        @staticmethod
        def calls() -> list[str]:
            p = state / "calls.log"
            return p.read_text().splitlines() if p.exists() else []

    CF.lists = lists
    return CF


def _parse(cf, family: str, lines: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ}
    return subprocess.run(["bash", str(cf.root / "deploy" / "cloudflare-ips.sh"), f"parse{family}"],
                          input="\n".join(lines) + "\n", capture_output=True, text=True, env=env, cwd=cf.root)


def test_the_list_checks(cf):
    assert _parse(cf, "4", V4).stdout.split() == V4
    assert _parse(cf, "6", V6).stdout.split() == V6
    assert _parse(cf, "4", [*V4, "  ", "\r"]).returncode == 0  # blank lines and CRLF are fine
    for bad in ("1.2.3.4/33", "256.1.1.1/24", "1.2.3.4", "evil.example", "1.0.0.0/7", "1.2.3.4/24; reboot", "$(id)"):
        assert _parse(cf, "4", [*V4, bad]).returncode != 0, bad
    for bad in ("2400:::1/32", "2400:cb00::/8", "12345::/32", "2400:cb00::", "1.2.3.4/24", "2400::cb00::1/32"):
        assert _parse(cf, "6", [*V6, bad]).returncode != 0, bad
    assert _parse(cf, "4", V4[:3]).returncode != 0  # a truncated list is not a list
    assert _parse(cf, "6", V6[:2]).returncode != 0


def test_ranges_written_for_caddy_and_bad_lists_never_used(cf):
    cf.env("TLS_MODE=auto\n")
    r = cf.run()
    assert r.returncode == 0, r.stderr
    assert cf.snippet().splitlines()[-1] == "trusted_proxies static " + " ".join(V4 + V6)
    assert not any(c.startswith(("iptables", "ip6tables", "docker")) for c in cf.calls())  # nothing else touched
    bad = cf.lists / "bad4"
    bad.write_text("\n".join([*V4[:5], "0.0.0.0/0"]) + "\n")
    r = cf.run(v4=bad)
    assert r.returncode == 0 and "keeping the last good one" in r.stderr
    assert cf.snippet().splitlines()[-1] == "trusted_proxies static " + " ".join(V4 + V6)  # unchanged
    missing = cf.lists / "nothing"
    r = cf.run(v4=missing, v6=missing)
    assert r.returncode == 0 and " ".join(V4 + V6) in cf.snippet()


def test_first_run_without_any_list_uses_the_built_in_copy(cf):
    cf.env("")
    missing = cf.lists / "nothing"
    assert cf.run(v4=missing, v6=missing).returncode == 0
    assert "173.245.48.0/20" in cf.snippet() and "2606:4700::/32" in cf.snippet()


def test_caddy_reloads_only_when_the_ranges_change(cf):
    cf.env("TLS_MODE=cloudflare\n")
    cf.run()
    cf.run()
    reloads = [c for c in cf.calls() if "caddy reload" in c]
    assert len(reloads) == 1


@pytest.mark.skipif(os.geteuid() != 0, reason="the firewall part refuses to run without root")
def test_firewall_cloudflare_only_in_docker_user_and_idempotent(cf):
    cf.env("TLS_MODE=cloudflare\nCF_FIREWALL=off\n")
    cf.run()
    assert not cf.chains()  # off: iptables is never called
    cf.env("TLS_MODE=cloudflare\nCF_FIREWALL=on\n")
    for _ in range(2):
        r = cf.run()
        assert r.returncode == 0, r.stderr
    for name, ranges in (("iptables", V4), ("ip6tables", V6)):
        ch = cf.chains(name)
        assert ch["DZ-CLOUDFLARE"] == [["-s", c, "-j", "RETURN"] for c in ranges] + [["-j", "DROP"]]
        hooks = ch["DOCKER-USER"]
        assert hooks == [["-i", "eth0", "-p", "tcp", "-m", "conntrack", "--ctstate", "NEW", "--ctdir", "ORIGINAL",
                          "--ctorigdstport", port, "-j", "DZ-CLOUDFLARE"] for port in ("80", "443")]
    assert not any("--dport 22" in c or "ctorigdstport 22" in c for c in cf.calls())  # SSH never touched
    status = cf.run("status").stdout
    assert "Firewall IPv4: ON (12 ranges)" in status and "Firewall IPv6: ON (5 ranges)" in status
    r = cf.run("firewall-off")
    assert r.returncode == 0
    assert cf.chains()["DOCKER-USER"] == [] and "DZ-CLOUDFLARE" not in cf.chains()
    assert "Firewall IPv4: off" in cf.run("status").stdout


def test_the_caddy_parts_exist_for_both_modes():
    caddy = ROOT / "deploy" / "caddy"
    caddyfile = (ROOT / "Caddyfile").read_text()
    assert "import /etc/caddy/conf.d/proxies-{$TLS_MODE:auto}.caddy" in caddyfile
    assert "import /etc/caddy/conf.d/tls-{$TLS_MODE:auto}.caddy" in caddyfile
    assert "header_up X-Forwarded-For {client_ip}" in caddyfile and "-Server" in caddyfile and "-Via" in caddyfile
    for mode in ("auto", "cloudflare"):
        assert (caddy / f"proxies-{mode}.caddy").exists() and (caddy / f"tls-{mode}.caddy").exists()
    assert "trusted_proxies" not in (caddy / "proxies-auto.caddy").read_text()  # direct clients: no header trusted
    assert "client_ip_headers CF-Connecting-IP" in (caddy / "proxies-cloudflare.caddy").read_text()
    assert "tls /etc/caddy/origin/cert.pem /etc/caddy/origin/key.pem" in (caddy / "tls-cloudflare.caddy").read_text()
    ignored = (ROOT / ".gitignore").read_text()
    assert "deploy/origin/" in ignored and "deploy/caddy/cloudflare-ips.caddy" in ignored
    assert "--no-server-header" in (ROOT / "Dockerfile").read_text()
