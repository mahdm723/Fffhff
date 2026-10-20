"""Periodic purge of temporary data (TTL policy).

* messages        – deleted at `expires_at` (MESSAGE_TTL, shortened to
                    MESSAGE_TTL_AFTER_READ once read)
* conversations   – deleted after CONVERSATION_IDLE_TTL without activity
                    (their remaining messages go with them)
* sessions, used anti-bot challenges, expired login blocks
* security events older than SECURITY_EVENT_RETENTION
* closed reports older than REPORT_RETENTION (open reports are kept)

Clients keep a local copy of the conversation history, so deleting delivered
messages on the server does not break the conversation on the device.
Deletes are idempotent; running on several instances at once is safe.

V6 phase 21: the tables that can pile up (messages, conversations, sessions, notifications, security events) are purged
at most BATCH rows per run: a short transaction holds its row locks for milliseconds, never the minutes a big backlog
took (users marking notifications read waited behind it); the rest goes at the next run (CLEANUP_INTERVAL).
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import AdminAuditLog, AdminSession, AuthSession, AuthThrottle, ContentFlag, Conversation, Message, Report, SecurityEvent, SignupPending, UsedChallenge

log = logging.getLogger("dzplay.cleanup")
BATCH = 5000  # rows per table per run


def batched(model, *where):
    """DELETE of at most BATCH rows of `model` matching `where` (oldest first is not needed: they all go eventually)."""
    pk = model.__mapper__.primary_key[0]
    return delete(model).where(pk.in_(select(pk).where(*where).limit(BATCH)))


def run_cleanup(db: Session, settings: Settings, media=None) -> dict[str, int]:
    now = clock.utcnow()
    counts: dict[str, int] = {}

    def purge(name: str, stmt) -> None:
        counts[name] = db.execute(stmt).rowcount or 0

    purge("messages", batched(Message, Message.expires_at <= now))
    expired_convs = [c for (c,) in db.query(Conversation.id).filter(Conversation.expires_at <= now).limit(BATCH // 10).all()]
    if expired_convs:
        db.execute(delete(Message).where(Message.conversation_id.in_(expired_convs)))
        purge("conversations", delete(Conversation).where(Conversation.id.in_(expired_convs)))
    purge("sessions", batched(AuthSession, AuthSession.expires_at <= now))
    purge("challenges", delete(UsedChallenge).where(UsedChallenge.expires_at <= now))
    purge("signup_pending", delete(SignupPending).where(SignupPending.expires_at <= now))  # V6 phase 15: never confirmed
    stale = now - timedelta(seconds=settings.LOGIN_FAILURE_WINDOW)
    purge("auth_throttle", delete(AuthThrottle).where(
        AuthThrottle.last_failure_at < stale,
        or_(AuthThrottle.blocked_until.is_(None), AuthThrottle.blocked_until <= now),
    ))
    purge("security_events", batched(SecurityEvent,
                                     SecurityEvent.created_at < now - timedelta(seconds=settings.SECURITY_EVENT_RETENTION)))
    purge("reports", delete(Report).where(and_(Report.expires_at <= now, Report.status != "open")))
    idle = now - timedelta(seconds=settings.ADMIN_SESSION_IDLE)
    purge("admin_sessions", delete(AdminSession).where(or_(AdminSession.expires_at <= now, AdminSession.last_seen_at < idle)))
    purge("audit_log", delete(AdminAuditLog).where(AdminAuditLog.created_at < now - timedelta(seconds=settings.AUDIT_LOG_RETENTION)))
    purge("flags", delete(ContentFlag).where(and_(ContentFlag.expires_at <= now, ContentFlag.status != "open")))
    from app.services.password_reset import purge as purge_resets

    counts["password_resets"] = purge_resets(db)
    from app.services import notify

    counts["notifications"] = notify.purge_old(db, limit=BATCH)  # V6 phase 4: older than notify.KEEP_DAYS
    if media is not None:
        purged = media.purge(db)
        counts["media_files"] = purged["expired"] + purged["lru"] + purged["orphans"]
    if any(counts.values()):
        log.info("cleanup: %s", counts)
    return counts
