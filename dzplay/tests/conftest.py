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
    )
    base.update(overrides)
    return Settings(**base)


class Harness:
    """Creates an app and per-user clients (each with its own cookie jar and IP)."""

    def __init__(self, tmp_path, **overrides):
        clock.reset()
        self.settings = make_settings(tmp_path, **overrides)
        self.app = create_app(self.settings)
        state = self.app.state.dz
        Base.metadata.drop_all(state.database.engine)
        if self.settings.REDIS_URL:
            state.limiter.reset()
        self._lifespan = TestClient(self.app)
        self._lifespan.__enter__()  # runs startup (create tables, hub)
        self.state = state

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
        body = {"email": email, "password": password, "password_confirm": password, "antibot": self.challenge(c, "register")}
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

    def close(self):
        self._lifespan.__exit__(None, None, None)
        self.state.database.engine.dispose()
        clock.reset()


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
