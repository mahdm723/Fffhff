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
        MEDIA_CACHE_DIR=str(tmp_path / "media-cache"),
        UPLOAD_TMP_DIR=str(tmp_path / "upload-tmp"),
        MEDIA_TICK_SECONDS=0,  # tests run media_items.tick explicitly
        MARKET_REFRESH_SECONDS=0,  # no Bybit calls from tests (tests/test_market.py uses a fake transport)
    )
    base.update(overrides)
    return Settings(**base)


def _drop_legacy_tables(engine) -> None:
    """Tables of features removed in V6 (left by older test runs or by tests/test_v6_cleanup.py): they reference
    `users`, so they must go before drop_all."""
    from sqlalchemy import text

    from tests.legacy_v5_schema import LEGACY_TABLES

    cascade = " CASCADE" if engine.dialect.name == "postgresql" else ""
    with engine.begin() as conn:
        for t in reversed(LEGACY_TABLES):
            conn.execute(text(f"DROP TABLE IF EXISTS {t}{cascade}"))


class Harness:
    """Creates an app and per-user clients (each with its own cookie jar and IP)."""

    def __init__(self, tmp_path, telegram_transport=None, **overrides):
        clock.reset()
        self.settings = make_settings(tmp_path, **overrides)
        self.app = create_app(self.settings, telegram_transport=telegram_transport)
        state = self.app.state.dz
        _drop_legacy_tables(state.database.engine)
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


def public_id(c: TestClient) -> str:
    pid = getattr(c, "public_id", None)
    if pid is None:
        pid = c.get("/api/me").json()["public_id"]
        c.public_id = pid  # type: ignore[attr-defined]
    return pid


def send(c: TestClient, to, content: str = "أحتاج أن أتحدث معك اليوم.", **extra):
    """V6: a direct message to `to` (a client or a public ID). The first one is a message request;
    random anonymous messages were removed in phase 1b."""
    pid = to if isinstance(to, str) else public_id(to)
    return c.post(f"/api/people/{pid}/messages", json={"content": content, **extra})


def chat(a: TestClient, b: TestClient, content: str = "أحتاج أن أتحدث معك اليوم.") -> str:
    """A direct chat from a to b that b already accepted; returns its id."""
    r = send(a, b, content)
    assert r.status_code == 201, r.text
    cid = r.json()["conversation"]["id"]
    assert b.post(f"/api/conversations/{cid}/request", json={"action": "accept"}).status_code == 200
    return cid


def legacy_anonymous(hx, a: TestClient, b: TestClient, content: str = "رسالة مجهولة قديمة") -> str:
    """An old random anonymous conversation (kind NULL, from before V6 phase 1b) a → b, written to the database."""
    from datetime import timedelta

    from app.models import Conversation, Message, User

    now = clock.utcnow()
    with hx.db() as db:
        ua = db.query(User).filter_by(email=a.email).one()
        ub = db.query(User).filter_by(email=b.email).one()
        conv = Conversation(initiator_id=ua.id, recipient_id=ub.id, created_at=now, last_message_at=now, updated_at=now,
                            expires_at=now + timedelta(seconds=hx.settings.CONVERSATION_IDLE_TTL), last_sender_id=ua.id,
                            consecutive_count=1, initiator_sent=True)
        db.add(conv)
        db.flush()
        db.add(Message(conversation_id=conv.id, sender_id=ua.id, recipient_id=ub.id, content=content, created_at=now,
                       expires_at=now + timedelta(seconds=hx.settings.MESSAGE_TTL)))
        db.commit()
        return conv.id


def reply(c: TestClient, cid: str, content: str, **extra):
    return c.post(f"/api/conversations/{cid}/messages", json={"content": content, **extra})
