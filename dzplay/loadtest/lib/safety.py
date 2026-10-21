"""V6 phase 21: load tests touch STAGING only. Every load-test script calls these checks first, and stops at the first
doubt.

* APP_ENV=staging must be set in the environment of the script itself.
* A target URL is refused when its host is one of production's names (DOMAIN, APP_DOMAIN, PUBLIC_URL, LEGACY_HOSTS
  read from production's .env — /opt/dzplay/dzplay/.env, or LT_PRODUCTION_ENV) or when it uses port 80 or 443
  (production's ports: staging listens on 8443 only). It must then answer GET /healthz/env with
  {"env": "staging", "loadtest": true} — an app only says that with ENV=staging and LOADTEST_MODE.
* A database is refused unless its name ends with "_staging". The first seeding marks it (app_settings
  loadtest.staging) and only an empty database can be marked; a database with users and no mark is refused.

    APP_ENV=staging LOADTEST_TOKEN=… python loadtest/lib/safety.py target https://staging.test:8443   (exit 0 = safe)
    LT_RESOLVE=<server address> …   when staging.test does not resolve to the server (a separate load generator)
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import ssl
import sys
from pathlib import Path
from urllib.parse import urlsplit

PRODUCTION_ENV_FILES = [os.environ.get("LT_PRODUCTION_ENV", ""), "/opt/dzplay/dzplay/.env"]
FORBIDDEN_PORTS = {80, 443}
MARKER = "loadtest.staging"


class Unsafe(SystemExit):
    def __init__(self, why: str):
        super().__init__(f"✗ REFUSED (load tests touch staging only): {why}")


def require_staging_env() -> None:
    if os.environ.get("APP_ENV") != "staging":
        raise Unsafe("APP_ENV=staging is not set for this script")


def _env_values(path: str) -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep and key.strip().isupper():
                out[key.strip()] = value.split(" #", 1)[0].strip().strip('"')
    except OSError:
        pass
    return out


def production_hosts() -> set[str]:
    hosts: set[str] = set()
    for path in filter(None, PRODUCTION_ENV_FILES):
        env = _env_values(path)
        if env.get("ENV") == "staging":
            continue  # a staging .env is not production's
        for key in ("DOMAIN", "APP_DOMAIN"):
            if env.get(key) and not env[key].startswith(":"):
                hosts.add(env[key].lower())
        if env.get("PUBLIC_URL"):
            host = urlsplit(env["PUBLIC_URL"]).hostname
            if host:
                hosts.add(host.lower())
        hosts |= {h.strip().lower() for h in env.get("LEGACY_HOSTS", "").split(",") if h.strip()}
    return hosts


def check_target(url: str, token: str, fetch=None) -> None:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    if not host:
        raise Unsafe(f"not a URL: {url!r}")
    if host in production_hosts():
        raise Unsafe(f"{host} is production")
    if port in FORBIDDEN_PORTS:
        raise Unsafe(f"port {port} is production's (staging listens on 8443)")
    answer = (fetch or _fetch)(url.rstrip("/") + "/healthz/env", token)
    if answer != {"env": "staging", "loadtest": True}:
        raise Unsafe(f"{url} does not answer as staging ({answer!r})")


class _Resolved(http.client.HTTPSConnection):
    """HTTPS to `address` with the URL's own name in TLS (SNI) and Host — like curl --resolve. Staging's certificate
    is for staging.test; the generator reaches it by the server's address (LT_RESOLVE)."""

    def __init__(self, name: str, address: str, port: int, context: ssl.SSLContext):
        super().__init__(address, port, timeout=10, context=context)
        self._name = name

    def connect(self) -> None:
        sock = socket.create_connection((self.host, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self._name)


def _fetch(url: str, token: str) -> object:
    ctx = ssl.create_default_context()
    ctx.check_hostname, ctx.verify_mode = False, ssl.CERT_NONE  # staging: Caddy's internal certificate
    parts = urlsplit(url)
    conn = _Resolved(parts.hostname, os.environ.get("LT_RESOLVE") or parts.hostname, parts.port or 443, ctx)
    try:
        conn.request("GET", (parts.path or "/") + (f"?{parts.query}" if parts.query else ""), headers={"Host": parts.netloc, "X-Loadtest-Token": token,
                                                         "User-Agent": "dz-loadtest-safety"})
        r = conn.getresponse()
        return json.loads(r.read().decode()) if r.status == 200 else f"HTTP {r.status}"
    except Exception as exc:  # noqa: BLE001
        return f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


def check_database(cur) -> None:
    """`cur`: a DB-API cursor on the target database. Marks an empty staging database; refuses anything else."""
    cur.execute("SELECT current_database()")
    name = cur.fetchone()[0]
    if not str(name).endswith("_staging"):
        raise Unsafe(f"database {name!r} is not a staging database (its name must end with _staging)")
    cur.execute("SELECT value FROM app_settings WHERE key = %s", (MARKER,))
    if cur.fetchone():
        return
    cur.execute("SELECT count(*) FROM users WHERE is_official IS NOT TRUE AND is_system IS NOT TRUE")
    if cur.fetchone()[0] > 0:
        raise Unsafe(f"database {name!r} already has users and was never marked as staging")
    cur.execute("INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (%s, '1', now(), 'loadtest')",
                (MARKER,))


if __name__ == "__main__":
    require_staging_env()
    if len(sys.argv) == 3 and sys.argv[1] == "target":
        check_target(sys.argv[2], os.environ.get("LOADTEST_TOKEN", ""))
        print(f"✓ {sys.argv[2]} is staging")
    else:
        raise SystemExit("usage: APP_ENV=staging LOADTEST_TOKEN=… python loadtest/lib/safety.py target <url>")
