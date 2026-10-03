"""Test fixtures.

Database: a fresh SQLite file per test by default. Set
DZ_TEST_DATABASE_URL=postgresql+psycopg://... to run the whole suite on
PostgreSQL (tables are dropped/recreated per test). Set DZ_TEST_REDIS_URL to
run with the Redis rate limiter + pub/sub.
"""

from __future__ import annotations

import itertools
import logging
import os
import warnings

import pytest

warnings.filterwarnings("ignore", category=DeprecationWarning)
logging.getLogger("httpx").setLevel(logging.WARNING)

from fastapi.testclient import TestClient  # noqa: E402

from app import clock  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import Base  # noqa: E402
from app.main import create_app  # noqa: E402
from app.security.pow import solve  # noqa: E402

PASSWORD = "Str0ng-Pass!"
_FFPROBE_SHIM = None


def ffprobe_shim() -> str:
    """Executable `ffprobe` stand-in (PyAV) — the sandbox has ffmpeg but no ffprobe."""
    global _FFPROBE_SHIM
    if _FFPROBE_SHIM is None:
        import stat
        import sys
        import tempfile
        from pathlib import Path

        target = Path(tempfile.mkdtemp(prefix="dz-ffprobe-")) / "ffprobe"
        shim = Path(__file__).with_name("ffprobe_shim.py")
        target.write_text(f"#!{sys.executable}\nimport runpy, sys\nrunpy.run_path({str(shim)!r}, run_name='__main__')\n")
        target.chmod(target.stat().st_mode | stat.S_IEXEC)
        _FFPROBE_SHIM = str(target)
    return _FFPROBE_SHIM


def ffmpeg_binary() -> str:
    import shutil

    if shutil.which("ffmpeg"):
        return "ffmpeg"
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()
ADMIN_PATH = "/test-panel"
ADMIN_PASSWORD = "Admin-Pass-0123456"
_ip_counter = itertools.count(1)


def make_settings(tmp_path, **overrides) -> Settings:
    url = os.environ.get("DZ_TEST_DATABASE_URL") or f"sqlite:///{tmp_path}/test.db"
    base = dict(
        ENV="test",
        SECRET_KEY="test-secret-key",
        DATABASE_URL=url,
        REDIS_URL=os.environ.get("DZ_TEST_REDIS_URL", ""),
        POW_MAX_NUMBER=300,
        POW_ELEVATED_MAX_NUMBER=1500,
        CLEANUP_INTERVAL=0,
        TRUST_PROXY_HEADERS=True,
        GOOGLE_CLIENT_ID="test-client.apps.googleusercontent.com",
        REPORT_AUTO_SUSPEND_THRESHOLD=0,
        LOG_LEVEL="WARNING",
        ADMIN_PATH=ADMIN_PATH,
        ENGAGEMENT_TICK_SECONDS=0,  # tests advance gradual jobs explicitly
        CALL_TICK_SECONDS=0,  # tests run calls.tick explicitly
        TURN_SECRET="test-turn-secret",
        TURN_HOST="turn.example.test",
        MEDIA_CACHE_DIR=str(tmp_path / "media-cache"),
        FFMPEG_BINARY=ffmpeg_binary(),
        FFPROBE_BINARY="ffprobe" if __import__("shutil").which("ffprobe") else ffprobe_shim(),
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    """Creates an app and per-user clients (each with its own cookie jar and IP)."""

    def __init__(self, tmp_path, telegram_transport=None, **overrides):
        clock.reset()
        self.settings = make_settings(tmp_path, **overrides)
        self.app = create_app(self.settings, telegram_transport=telegram_transport)
        state = self.app.state.dz
        Base.metadata.drop_all(state.database.engine)
        if self.settings.REDIS_URL:
            state.limiter.reset()
        self._lifespan = TestClient(self.app)
        self._lifespan.__enter__()  # runs startup (create tables, hub)
        self.state = state
        self.admin_secrets: dict[str, str] = {}
        self._admin_clients: dict[str, AdminClient] = {}

    def client(self, ip: str | None = None) -> TestClient:
        ip = ip or f"10.0.{next(_ip_counter) % 250}.{next(_ip_counter) % 250}"
        c = TestClient(self.app, headers={"X-DZ-Requested": "1", "X-Forwarded-For": ip})
        c.ip = ip  # type: ignore[attr-defined]
        return c

    def challenge(self, c: TestClient, purpose: str) -> dict:
        r = c.post("/api/auth/challenge", json={"purpose": purpose})
        assert r.status_code == 200, r.text
        ch = r.json()
        ch["number"] = solve(ch)
        return ch

    def register(self, c: TestClient, email: str, password: str = PASSWORD, **extra):
        body = {"email": email, "password": password, "password_confirm": password, "antibot": self.challenge(c, "register"),
                "gender": "unspecified", "age_confirmed": True}
        body.update(extra)
        return c.post("/api/auth/register", json=body)

    def login(self, c: TestClient, email: str, password: str = PASSWORD):
        return c.post("/api/auth/login", json={"email": email, "password": password, "antibot": self.challenge(c, "login")})

    def user(self, email: str | None = None, ip: str | None = None) -> TestClient:
        c = self.client(ip)
        email = email or f"user{next(_ip_counter)}@example.com"
        r = self.register(c, email)
        assert r.status_code == 201, r.text
        c.email = email  # type: ignore[attr-defined]
        return c

    def db(self):
        return self.state.database.session()

    # --- admin panel -------------------------------------------------------
    def create_admin(self, username: str = "owner") -> str:
        """Create an admin account; returns its TOTP secret."""
        from app.services import admin_auth

        with self.db() as db:
            _admin, secret = admin_auth.create_admin(db, self.settings, username, ADMIN_PASSWORD)
        self.admin_secrets[username] = secret
        return secret

    def admin_login(self, c: TestClient, username: str = "owner", code: str | None = None, password: str = ADMIN_PASSWORD):
        from app.security import totp

        secret = self.admin_secrets.get(username) or self.create_admin(username)
        if code is None:
            # each TOTP step can be used once: move to the next step for every new login
            clock.advance(totp.STEP)
            code = totp.code_at(secret, totp.current_step(clock.timestamp()))
        return c.post(f"{ADMIN_PATH}/api/admin/login", json={"username": username, "password": password, "code": code})

    def admin(self, username: str = "owner", ip: str | None = None) -> "AdminClient":
        """A client signed in to the admin panel (cached per username)."""
        if username not in self._admin_clients:
            c = self.client(ip)
            r = self.admin_login(c, username)
            assert r.status_code == 200, r.text
            self._admin_clients[username] = AdminClient(c)
        return self._admin_clients[username]

    def close(self):
        self._lifespan.__exit__(None, None, None)
        self.state.database.engine.dispose()
        clock.reset()


class AdminClient:
    """Wraps a signed-in TestClient; "/api/admin/..." paths go to the secret admin prefix."""

    def __init__(self, client: TestClient):
        self.client = client

    @staticmethod
    def _path(path: str) -> str:
        return ADMIN_PATH + path if path.startswith("/api/admin") else path

    def get(self, path: str, **kw):
        return self.client.get(self._path(path), **kw)

    def post(self, path: str, **kw):
        return self.client.post(self._path(path), **kw)

    def put(self, path: str, **kw):
        return self.client.put(self._path(path), **kw)

    def delete(self, path: str, **kw):
        return self.client.delete(self._path(path), **kw)


@pytest.fixture
def make_harness(tmp_path):
    created: list[Harness] = []

    def factory(**overrides) -> Harness:
        h = Harness(tmp_path, **overrides)
        created.append(h)
        return h

    yield factory
    for h in created:
        h.close()


@pytest.fixture
def hx(make_harness) -> Harness:
    return make_harness()


def send(c: TestClient, content: str = "أحتاج أن أتحدث مع شخص اليوم.", **extra):
    return c.post("/api/messages", json={"content": content, **extra})


def reply(c: TestClient, cid: str, content: str, **extra):
    return c.post(f"/api/conversations/{cid}/messages", json={"content": content, **extra})
