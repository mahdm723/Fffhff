"""Admin panel: content (Ideas), the official DZPLAY account, the comment
library, users and network blocks. Every mutating call is audited by the API layer.

Reaction counters are never editable here: they only reflect real users.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, not_found
from app.models import (
    AuthThrottle,
    CannedComment,
    Comment,
    ContentFlag,
    Post,
    Report,
    SecurityEvent,
    User,
)
from app.services.content import clean_message
from app.services.messaging import Effects, iso

OFFICIAL_EMAIL = "official@dzplay.invalid"  # not a real mailbox; the account has no password and cannot sign in


# ---------------------------------------------------------------------------
# official account
# ---------------------------------------------------------------------------


def official_user(db: Session) -> User:
    user = db.scalar(select(User).where(User.is_official.is_(True)))
    if user is None:
        user = User(email=OFFICIAL_EMAIL, password_hash=None, google_sub=None, status="active", is_official=True,
                    privacy_ack_version=PRIVACY_VERSION)
        db.add(user)
        db.flush()
    return user


def _comment_text(db: Session, settings: Settings, text: object, library_id: object, max_len: int) -> str:
    if library_id:
        item = db.get(CannedComment, str(library_id)[:32])
        if item is None:
            raise not_found()
        text = item.text
        item.usage_count = (item.usage_count or 0) + 1
    return clean_message(text, max_len, "allow_plain")


def official_comment(db: Session, settings: Settings, *, target: str, target_id: str, text: object,
                     library_id: object, effects: Effects) -> dict:
    user = official_user(db)
    if target == "idea":
        post = db.get(Post, str(target_id)[:32])
        if post is None or post.status != "visible":
            raise not_found()
        body = _comment_text(db, settings, text, library_id, settings.MAX_COMMENT_LENGTH)
        comment = Comment(post_id=post.id, author_id=user.id, content=body, created_at=clock.utcnow())
        db.add(comment)
        db.execute(update(Post).where(Post.id == post.id).values(
            comments_count=Post.comments_count + 1, unseen_comments_count=Post.unseen_comments_count + 1))
        db.flush()
        effects.signal(post.author_id, "comment")  # like any Ideas comment: only the post owner will see it
        return {"target": "idea", "comment_id": comment.id}
    raise AppError(400, "invalid_target", "invalid target")


# ---------------------------------------------------------------------------
# Ideas list
# ---------------------------------------------------------------------------


def ideas_list(db: Session, limit: int, before: str | None) -> dict:
    from app.services.messaging import parse_iso

    q = select(Post).where(Post.status == "visible").order_by(Post.created_at.desc()).limit(min(limit, 100) + 1)
    b = parse_iso(before)
    if b:
        q = q.where(Post.created_at < b)
    rows = list(db.execute(q).scalars())
    more = len(rows) > limit
    rows = rows[:limit]
    return {"ideas": [{"id": p.id, "content": p.content, "likes": p.likes_count, "dislikes": p.dislikes_count,
                       "comments": p.comments_count, "created_at": iso(p.created_at)} for p in rows],
            "next_before": iso(rows[-1].created_at) if more and rows else None}


def asset_for_admin(db: Session, asset_id: str):
    """A user's picture — including evidence kept after removal."""
    from app.models import MediaItem

    item = db.get(MediaItem, asset_id)
    if item is None or not item.tg_file_id:
        raise not_found()
    return item


# ---------------------------------------------------------------------------
# users & network blocks (no e-mails, no password hashes — ever)
# ---------------------------------------------------------------------------


def users_list(db: Session, which: str, limit: int = 100) -> list[dict]:
    reports = select(Report.reported_user_id.label("uid"), func.count().label("n")).group_by(Report.reported_user_id).subquery()
    flags = select(ContentFlag.offender_id.label("uid"), func.count().label("n")).group_by(ContentFlag.offender_id).subquery()
    q = (select(User.id, User.status, User.created_at, User.last_active_at,
                func.coalesce(reports.c.n, 0), func.coalesce(flags.c.n, 0))
         .outerjoin(reports, reports.c.uid == User.id).outerjoin(flags, flags.c.uid == User.id)
         .where(User.is_official.is_not(True), User.is_system.is_not(True)))
    if which in ("suspended", "banned"):
        q = q.where(User.status == which)
    elif which == "reported":
        q = q.where(or_(reports.c.n > 0, flags.c.n > 0))
    else:
        raise AppError(400, "invalid_filter", "invalid filter")
    q = q.order_by(func.coalesce(reports.c.n, 0).desc(), User.last_active_at.desc()).limit(limit)
    return [{"ref": uid, "status": status, "created_at": iso(created), "last_active_at": iso(active),
             "reports": int(r or 0), "flags": int(f or 0)}
            for uid, status, created, active, r, f in db.execute(q).all()]


def ip_blocks(db: Session, settings: Settings) -> list[dict]:
    now = clock.utcnow()
    out = []
    for row in db.execute(select(AuthThrottle).where(AuthThrottle.blocked_until > now)
                          .order_by(AuthThrottle.blocked_until.desc())).scalars():
        kind, _, rest = row.key.partition(":")
        out.append({"id": f"login:{row.key}", "kind": "login", "scope": kind, "ref": rest[:12],
                    "until": iso(row.blocked_until), "failures": row.failures})
    since = now - timedelta(seconds=settings.RESET_IP_WINDOW)
    rows = db.execute(select(SecurityEvent.ip_hash, func.count(), func.max(SecurityEvent.created_at))
                      .where(SecurityEvent.type == "reset_request", SecurityEvent.created_at > since,
                             SecurityEvent.ip_hash.is_not(None))
                      .group_by(SecurityEvent.ip_hash)).all()
    for ip_hash, n, last in rows:
        if n >= settings.RESET_MAX_REQUESTS:
            out.append({"id": f"reset:{ip_hash}", "kind": "reset", "scope": "ip", "ref": ip_hash[:12],
                        "until": iso(last + timedelta(seconds=settings.RESET_IP_BLOCK_DURATION)), "failures": n})
    return out


def lift_ip_block(db: Session, block_id: str) -> None:
    kind, _, key = block_id.partition(":")
    if kind == "login":
        row = db.get(AuthThrottle, key)
        if row is None:
            raise not_found()
        db.delete(row)
    elif kind == "reset" and key:
        db.execute(update(SecurityEvent).where(SecurityEvent.type == "reset_request", SecurityEvent.ip_hash == key)
                   .values(type="reset_request_cleared"))
    else:
        raise not_found()


def extra_stats(db: Session, settings: Settings, media) -> dict:
    day = clock.utcnow() - timedelta(days=1)
    from app.models import PasswordReset

    def count(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "password_resets": {
            "requests_24h": count(SecurityEvent, SecurityEvent.type == "reset_request", SecurityEvent.created_at > day),
            "waiting_admin": count(PasswordReset, PasswordReset.status == "pending"),
            "completed_24h": count(SecurityEvent, SecurityEvent.type == "password_reset", SecurityEvent.created_at > day),
        },
        "ip_blocks_active": len(ip_blocks(db, settings)),
        "media_cache_bytes": media.total_size(db) if media is not None else 0,
    }
