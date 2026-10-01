"""Internal administration (no message content except reported evidence).

Admin policy:
* Aggregated statistics never include message content.
* Message content is visible to admins only through report snapshots — the
  minimal evidence copied when a user explicitly reports a message or
  conversation — and only to handle that abuse report.
* Users are referred to by an opaque internal reference; e-mail addresses are
  not exposed through the admin API.
"""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import AppError, not_found
from app.models import AuthSession, AuthThrottle, Block, Conversation, Message, Report, SecurityEvent, User
from app.security.sessions import revoke_all_sessions
from app.services.messaging import iso


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def stats(db: Session) -> dict:
    now = clock.utcnow()
    day = now - timedelta(days=1)
    return {
        "users": {
            "total": _count(db, User),
            "active_24h": _count(db, User, User.last_active_at > day),
            "new_24h": _count(db, User, User.created_at > day),
            "suspended": _count(db, User, User.status == "suspended"),
            "banned": _count(db, User, User.status == "banned"),
            "google": _count(db, User, User.google_sub.is_not(None)),
        },
        "messages": {
            "total_sent_all_time": db.scalar(select(func.coalesce(func.sum(User.messages_sent), 0))) or 0,
            "stored_now": _count(db, Message),
            "sent_24h": _count(db, Message, Message.created_at > day),
        },
        "conversations": {
            "stored_now": _count(db, Conversation),
            "active": _count(db, Conversation, Conversation.status == "active"),
            "closed": _count(db, Conversation, Conversation.status == "closed"),
            "created_24h": _count(db, Conversation, Conversation.created_at > day),
        },
        "safety": {
            "blocks": _count(db, Block),
            "reports_open": _count(db, Report, Report.status == "open"),
            "failed_logins_24h": _count(db, SecurityEvent, SecurityEvent.type == "login_failed", SecurityEvent.created_at > day),
            "active_login_blocks": _count(db, AuthThrottle, AuthThrottle.blocked_until > now),
            "registrations_limited_24h": _count(db, SecurityEvent, SecurityEvent.type == "register_limited", SecurityEvent.created_at > day),
        },
        "sessions_active": _count(db, AuthSession, AuthSession.expires_at > now),
        "generated_at": iso(now),
    }


def list_reports(db: Session, status: str = "open", limit: int = 50) -> list[dict]:
    rows = db.execute(select(Report).where(Report.status == status).order_by(Report.created_at.desc()).limit(limit)).scalars()
    out = []
    for r in rows:
        out.append({
            "id": r.id,
            "reason": r.reason,
            "details": r.details,
            "evidence": json.loads(r.snapshot or "[]"),
            "reported_user_ref": r.reported_user_id,
            "reported_user_reports_total": _count(db, Report, Report.reported_user_id == r.reported_user_id),
            "status": r.status,
            "resolution": r.resolution,
            "created_at": iso(r.created_at),
        })
    return out


def resolve_report(db: Session, report_id: str, action: str) -> dict:
    if action not in ("dismiss", "warn", "suspend", "ban"):
        raise AppError(400, "invalid_action", "invalid action")
    rep = db.get(Report, report_id)
    if rep is None:
        raise not_found()
    rep.status = "dismissed" if action == "dismiss" else "resolved"
    rep.resolution = action
    rep.resolved_at = clock.utcnow()
    if action in ("suspend", "ban"):
        set_user_status(db, rep.reported_user_id, "suspended" if action == "suspend" else "banned")
    return {"id": rep.id, "status": rep.status, "resolution": action}


def set_user_status(db: Session, user_ref: str, status: str) -> dict:
    if status not in ("active", "suspended", "banned"):
        raise AppError(400, "invalid_status", "invalid status")
    user = db.get(User, user_ref)
    if user is None:
        raise not_found()
    user.status = status
    if status == "banned":
        revoke_all_sessions(db, user.id)
    db.add(SecurityEvent(type=f"admin_set_{status}", user_id=user.id, created_at=clock.utcnow()))
    return {"user_ref": user.id, "status": status}


def security_events(db: Session, type_: str | None, limit: int = 100) -> list[dict]:
    q = select(SecurityEvent).order_by(SecurityEvent.created_at.desc()).limit(min(limit, 500))
    if type_:
        q = q.where(SecurityEvent.type == type_)
    return [
        {"type": e.type, "user_ref": e.user_id, "ip_ref": (e.ip_hash or "")[:12] or None, "detail": e.detail, "at": iso(e.created_at)}
        for e in db.execute(q).scalars()
    ]
