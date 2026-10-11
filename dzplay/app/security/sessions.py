"""Opaque session tokens.

The browser holds a random 256-bit token in an HttpOnly cookie; the database
stores only its SHA-256, so a database leak does not leak usable sessions.
Sessions slide forward while used and are revocable (logout / ban).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import AuthSession, User
from app.security.crypto import new_token, sha256_hex

_TOUCH_EVERY = timedelta(minutes=5)


def create_session(db: Session, settings: Settings, user: User) -> str:
    token = new_token()
    now = clock.utcnow()
    db.add(
        AuthSession(
            token_hash=sha256_hex(token),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + timedelta(seconds=settings.SESSION_DURATION),
        )
    )
    user.last_active_at = now
    return token


def resolve_session(db: Session, settings: Settings, token: str | None) -> User | None:
    if not token or len(token) > 128:
        return None
    sess = db.get(AuthSession, sha256_hex(token))
    now = clock.utcnow()
    if sess is None or sess.expires_at <= now:
        return None
    user = db.get(User, sess.user_id)
    if user is None or user.status == "banned":
        return None
    if now - sess.last_seen_at > _TOUCH_EVERY:
        sess.last_seen_at = now
        sess.expires_at = now + timedelta(seconds=settings.SESSION_DURATION)
        user.last_active_at = now
    return user


def revoke_session(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AuthSession).where(AuthSession.token_hash == sha256_hex(token)))


def revoke_all_sessions(db: Session, user_id: str) -> None:
    db.execute(delete(AuthSession).where(AuthSession.user_id == user_id))
