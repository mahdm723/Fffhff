from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


STARTUP_LOCK = 0x445A0001  # advisory lock keys of the app: "DZ" + a number (services/leader.py: the loops)


def make_engine(url: str, pool_size: int = 10, max_overflow: int = 20, pool_timeout: int = 30) -> Engine:
    if url.startswith("sqlite"):
        engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover - trivial
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.close()

        return engine
    return create_engine(url, pool_pre_ping=True, pool_size=pool_size, max_overflow=max_overflow, pool_timeout=pool_timeout)


class Database:
    def __init__(self, url: str, pool_size: int = 10, max_overflow: int = 20, pool_timeout: int = 30):
        self.engine = make_engine(url, pool_size, max_overflow, pool_timeout)
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False, autoflush=False)
        # V6 phase 21: called after a commit that ended sessions (logout, ban, password change…) with their user ids,
        # so their open WebSockets re-check at once (state.py connects it to the hub)
        self.on_sessions_ended: Callable[[set[str]], None] | None = None

    @contextmanager
    def startup_lock(self) -> Iterator[None]:
        """V6 phase 21: several workers start together; the schema steps and the official account run in one at a time
        (a PostgreSQL advisory lock held by a connection of its own; nothing to do on SQLite)."""
        if self.engine.dialect.name != "postgresql":
            yield
            return
        with self.engine.connect() as conn:
            conn.execute(text("SELECT pg_advisory_lock(:k)"), {"k": STARTUP_LOCK})
            try:
                yield
            finally:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": STARTUP_LOCK})

    def create_all(self) -> None:
        from app import models  # noqa: F401  (register models)
        from app.migrations import add_missing_columns, backfill, search_index

        Base.metadata.create_all(self.engine)
        add_missing_columns(self.engine)
        backfill(self.engine)
        search_index(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        db = self.SessionLocal()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
        ended = db.info.pop("dz_sessions_ended", None)
        if ended and self.on_sessions_ended is not None:
            self.on_sessions_ended(ended)
