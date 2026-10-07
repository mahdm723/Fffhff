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
"""

from __future__ import annotations

import logging
from datetime import timedelta

from sqlalchemy import and_, delete, or_
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import AdminAuditLog, AdminSession, AuthSession, AuthThrottle, ContentFlag, Conversation, Message, Report, SecurityEvent, UsedChallenge

log = logging.getLogger("dzplay.cleanup")


def run_cleanup(db: Session, settings: Settings, media=None) -> dict[str, int]:
    now = clock.utcnow()
    counts: dict[str, int] = {}

    def purge(name: str, stmt) -> None:
        counts[name] = db.execute(stmt).rowcount or 0

    purge("messages", delete(Message).where(Message.expires_at <= now))
    expired_convs = [c for (c,) in db.query(Conversation.id).filter(Conversation.expires_at <= now).all()]
    if expired_convs:
        db.execute(delete(Message).where(Message.conversation_id.in_(expired_convs)))
    purge("conversations", delete(Conversation).where(Conversation.expires_at <= now))
    purge("sessions", delete(AuthSession).where(AuthSession.expires_at <= now))
    purge("challenges", delete(UsedChallenge).where(UsedChallenge.expires_at <= now))
    stale = now - timedelta(seconds=settings.LOGIN_FAILURE_WINDOW)
    purge("auth_throttle", delete(AuthThrottle).where(
        AuthThrottle.last_failure_at < stale,
        or_(AuthThrottle.blocked_until.is_(None), AuthThrottle.blocked_until <= now),
    ))
    purge("security_events", delete(SecurityEvent).where(
        SecurityEvent.created_at < now - timedelta(seconds=settings.SECURITY_EVENT_RETENTION)))
    purge("reports", delete(Report).where(and_(Report.expires_at <= now, Report.status != "open")))
    idle = now - timedelta(seconds=settings.ADMIN_SESSION_IDLE)
    purge("admin_sessions", delete(AdminSession).where(or_(AdminSession.expires_at <= now, AdminSession.last_seen_at < idle)))
    purge("audit_log", delete(AdminAuditLog).where(AdminAuditLog.created_at < now - timedelta(seconds=settings.AUDIT_LOG_RETENTION)))
    purge("flags", delete(ContentFlag).where(and_(ContentFlag.expires_at <= now, ContentFlag.status != "open")))
    from app.services.password_reset import purge as purge_resets

    counts["password_resets"] = purge_resets(db)
    from app.services import notify

    counts["notifications"] = notify.purge_old(db)  # V6 phase 4: older than notify.KEEP_DAYS
    if media is not None:
        purged = media.purge(db)
        counts["media_files"] = purged["expired"] + purged["lru"] + purged["orphans"]
    if any(counts.values()):
        log.info("cleanup: %s", counts)
    return counts
