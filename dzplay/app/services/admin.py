"""Internal administration (no message content except reported evidence).

Admin policy:
* Aggregated statistics never include message content.
* Message content is visible to admins only for user protection, as told to
  users in the in-app privacy notice: report snapshots, automatically flagged
  content (ContentFlag), and the stored conversations of a user who has been
  reported or flagged. Every such conversation view is logged
  (security event "admin_view_messages").
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
from app.models import (
    AuthSession,
    AuthThrottle,
    Block,
    Comment,
    ContentFlag,
    Conversation,
    Message,
    Post,
    PostReaction,
    ReelComment,
    Report,
    SecurityEvent,
    User,
)
from app.security.sessions import revoke_all_sessions
from app.services.messaging import iso
from app.services.reels import remove_comment as remove_reel_comment


# Real people only: the official and system accounts are not users.
_REAL = (User.is_official.is_not(True), User.is_system.is_not(True))


def _count(db: Session, model, *where) -> int:
    return db.scalar(select(func.count()).select_from(model).where(*where)) or 0


def stats(db: Session, settings=None, media=None) -> dict:
    now = clock.utcnow()
    day = now - timedelta(days=1)
    return {
        "users": {
            "total": _count(db, User, *_REAL),
            "active_24h": _count(db, User, User.last_active_at > day, *_REAL),
            "new_24h": _count(db, User, User.created_at > day, *_REAL),
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
        "ideas": {
            "posts": _count(db, Post, Post.status == "visible"),
            "posts_24h": _count(db, Post, Post.created_at > day),
            "removed": _count(db, Post, Post.status == "removed"),
            "comments": _count(db, Comment),
            "reactions": _count(db, PostReaction),
        },
        "safety": {
            "blocks": _count(db, Block),
            "reports_open": _count(db, Report, Report.status == "open"),
            "flags_open": _count(db, ContentFlag, ContentFlag.status == "open"),
            "failed_logins_24h": _count(db, SecurityEvent, SecurityEvent.type == "login_failed", SecurityEvent.created_at > day),
            "active_login_blocks": _count(db, AuthThrottle, AuthThrottle.blocked_until > now),
            "registrations_limited_24h": _count(db, SecurityEvent, SecurityEvent.type == "register_limited", SecurityEvent.created_at > day),
        },
        "sessions_active": _count(db, AuthSession, AuthSession.expires_at > now),
        "generated_at": iso(now),
        **_extra(db, settings, media),
    }


def _extra(db: Session, settings, media) -> dict:
    if settings is None:
        return {}
    from app.services.admin_content import extra_stats

    return extra_stats(db, settings, media)


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
            "target": ("reel_comment" if r.reel_comment_id else "comment" if r.comment_id else "post" if r.post_id
                       else "message" if r.message_id else "conversation"),
            "reported_user_reports_total": _count(db, Report, Report.reported_user_id == r.reported_user_id),
            "reported_user_flags_total": _count(db, ContentFlag, ContentFlag.offender_id == r.reported_user_id),
            "status": r.status,
            "resolution": r.resolution,
            "created_at": iso(r.created_at),
        })
    return out


def resolve_report(db: Session, report_id: str, action: str) -> dict:
    """dismiss | warn | remove (hide the reported post / delete the comment) | suspend | ban"""
    if action not in ("dismiss", "warn", "remove", "suspend", "ban"):
        raise AppError(400, "invalid_action", "invalid action")
    rep = db.get(Report, report_id)
    if rep is None:
        raise not_found()
    rep.status = "dismissed" if action == "dismiss" else "resolved"
    rep.resolution = action
    rep.resolved_at = clock.utcnow()
    if action == "remove":
        if rep.reel_comment_id:
            rc = db.get(ReelComment, rep.reel_comment_id)
            if rc:
                remove_reel_comment(db, rc)
        elif rep.comment_id:
            comment = db.get(Comment, rep.comment_id)
            if comment:
                db.delete(comment)
        elif rep.post_id:
            post = db.get(Post, rep.post_id)
            if post:
                post.status = "removed"
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


ACTIVITY_SERIES = ("users", "posts", "conversations", "failed_logins")


def activity(db: Session, days: int = 14, tz_offset_minutes: int = 0) -> dict:
    """Per-day counts (no content, no identities) for the dashboard chart.

    Days are local to the admin's browser: `tz_offset_minutes` is JavaScript's
    getTimezoneOffset() (UTC minus local time). Conversations are counted from
    what is still stored, so ones already purged by the TTL are not included.
    """
    shift = timedelta(minutes=-tz_offset_minutes)
    local_today = (clock.utcnow() + shift).date()
    first = local_today - timedelta(days=days - 1)
    since = (clock.utcnow() + shift).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=days - 1) - shift
    sources = {
        "users": select(User.created_at).where(User.created_at >= since, *_REAL),
        "posts": select(Post.created_at).where(Post.created_at >= since),
        "conversations": select(Conversation.created_at).where(Conversation.created_at >= since),
        "failed_logins": select(SecurityEvent.created_at).where(SecurityEvent.type == "login_failed", SecurityEvent.created_at >= since),
    }
    index = {(first + timedelta(days=i)).isoformat(): i for i in range(days)}
    series = {}
    for name, query in sources.items():
        counts = [0] * days
        for ts in db.execute(query).scalars():
            i = index.get((ts + shift).date().isoformat())
            if i is not None:
                counts[i] += 1
        series[name] = counts
    return {"days": list(index), "series": series, "generated_at": iso(clock.utcnow())}


# ---------------------------------------------------------------------------
# automatic flags + conversation review (user protection)
# ---------------------------------------------------------------------------

FLAG_ACTIONS = ("dismiss", "warn", "remove", "suspend", "ban")


def list_flags(db: Session, status: str = "open", limit: int = 50) -> list[dict]:
    rows = db.execute(select(ContentFlag).where(ContentFlag.status == status)
                      .order_by(ContentFlag.created_at.desc()).limit(limit)).scalars()
    out = []
    for f in rows:
        out.append({
            "id": f.id,
            "target": f.target,
            "categories": [c for c in f.categories.split(",") if c],
            "terms": json.loads(f.terms or "[]"),
            "content": f.snapshot,
            "offender_ref": f.offender_id,
            "victim_ref": f.victim_id,
            "conversation_id": f.conversation_id,
            "offender_flags_total": _count(db, ContentFlag, ContentFlag.offender_id == f.offender_id),
            "offender_reports_total": _count(db, Report, Report.reported_user_id == f.offender_id),
            "status": f.status,
            "resolution": f.resolution,
            "created_at": iso(f.created_at),
        })
    return out


def resolve_flag(db: Session, flag_id: str, action: str) -> dict:
    """dismiss | warn | remove (delete the message / comment) | suspend | ban"""
    if action not in FLAG_ACTIONS:
        raise AppError(400, "invalid_action", "invalid action")
    flag = db.get(ContentFlag, flag_id)
    if flag is None:
        raise not_found()
    flag.status = "dismissed" if action == "dismiss" else "resolved"
    flag.resolution = action
    flag.resolved_at = clock.utcnow()
    if action == "remove":
        if flag.target == "reel_comment":
            rc = db.get(ReelComment, flag.comment_id) if flag.comment_id else None
            if rc is not None:
                remove_reel_comment(db, rc)
        else:
            source = (db.get(Message, flag.message_id) if flag.message_id
                      else db.get(Comment, flag.comment_id) if flag.comment_id else None)
            if source is not None:
                db.delete(source)
    if action in ("suspend", "ban"):
        set_user_status(db, flag.offender_id, "suspended" if action == "suspend" else "banned")
    return {"id": flag.id, "status": flag.status, "resolution": action}


def _under_review(db: Session, user_id: str) -> bool:
    """Conversations may only be opened for a user who was reported or flagged."""
    return bool(db.scalar(select(Report.id).where(Report.reported_user_id == user_id).limit(1))
                or db.scalar(select(ContentFlag.id).where(ContentFlag.offender_id == user_id).limit(1)))


def user_conversations(db: Session, user_ref: str, conv_limit: int = 50, msg_limit: int = 200) -> dict:
    user = db.get(User, user_ref)
    if user is None:
        raise not_found()
    convs = db.execute(
        select(Conversation)
        .where((Conversation.initiator_id == user.id) | (Conversation.recipient_id == user.id))
        .order_by(Conversation.last_message_at.desc()).limit(conv_limit)
    ).scalars().all()
    flagged = {m for (m,) in db.execute(select(ContentFlag.message_id).where(
        ContentFlag.offender_id == user.id, ContentFlag.message_id.is_not(None)))}
    out = []
    for c in convs:
        msgs = db.execute(select(Message).where(Message.conversation_id == c.id)
                          .order_by(Message.created_at.desc()).limit(msg_limit)).scalars().all()
        out.append({
            "id": c.id,
            "peer_ref": c.peer_of(user.id),
            "started_by": "user" if c.initiator_id == user.id else "peer",
            "status": c.status,
            "created_at": iso(c.created_at),
            "last_message_at": iso(c.last_message_at),
            "messages": [{
                "id": m.id,
                "from": "user" if m.sender_id == user.id else "peer",
                "content": m.content,
                "created_at": iso(m.created_at),
                "read": m.read_at is not None,
                "flagged": m.id in flagged,
            } for m in reversed(msgs)],
        })
    db.add(SecurityEvent(type="admin_view_messages", user_id=user.id, detail=f"{len(out)} conversations",
                         created_at=clock.utcnow()))
    return {"user_ref": user.id, "status": user.status, "conversations": out}
