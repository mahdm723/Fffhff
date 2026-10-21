"""V6 phase 21: one background task, one runner — with several workers (WEB_CONCURRENCY) or instances.

Each tick of a loop (cleanup, team comments, media expiry, market refresh) first takes a PostgreSQL advisory lock for the
length of its own transaction (pg_try_advisory_xact_lock): whoever does not get it skips this tick, and the lock goes
away with the commit or the rollback, even if a worker dies. SQLite (tests, one process): always yes.
"""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.orm import Session

# "DZ" + a number (db.STARTUP_LOCK is 1)
KEYS = {"cleanup": 0x445A0011, "engagement": 0x445A0012, "media": 0x445A0013, "market": 0x445A0014}


def take(db: Session, task: str) -> bool:
    """True when this worker runs `task` now (until the end of the current transaction)."""
    if db.get_bind().dialect.name != "postgresql":
        return True
    return bool(db.execute(text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": KEYS[task]}).scalar())
