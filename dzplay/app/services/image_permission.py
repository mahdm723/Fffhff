"""V6 phase 17: posting pictures with ideas is a per-account permission, granted and taken back by an admin only.

It has nothing to do with the membership (a member without it cannot post pictures; anyone with it can). Every
account starts without it; the daily limit (IDEA_IMAGE_LIMIT_PER_24H) still applies to those who have it. The panel
lists the accounts that posted pictures before, for a quick grant. Every change is audited and the account's open
app updates at once (signal "account"). Old picture posts stay as they are.
"""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import AppError
from app.models import Post, User
from app.services import audit
from app.services.messaging import Effects, iso

MAX_BULK = 200


def _row(u: User, posts: int = 0, last=None) -> dict:
    return {"id": u.id, "public_id": u.public_id, "name": u.display_name, "email": u.email, "status": u.status,
            "allowed": bool(u.can_post_images), "granted_at": iso(u.images_granted_at), "granted_by": u.images_granted_by,
            "image_posts": posts, "last_image_post": iso(last)}


def view(db: Session, user: User) -> dict:
    """For the user page of the panel."""
    posts = db.scalar(select(func.count()).select_from(Post).where(Post.author_id == user.id, Post.media_id.is_not(None))) or 0
    return {"allowed": bool(user.can_post_images), "granted_at": iso(user.images_granted_at),
            "granted_by": user.images_granted_by, "image_posts": posts}


def set_allowed(db: Session, user_ids: list[str], on: bool, actor: str, effects: Effects) -> dict:
    """Grant (on) or take back the permission for these accounts. Accounts already in that state are left alone."""
    ids = list(dict.fromkeys(str(i)[:32] for i in user_ids if i))
    if not ids or len(ids) > MAX_BULK:
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    changed = 0
    for user in db.scalars(select(User).where(User.id.in_(ids), User.is_system.is_not(True))).all():
        if bool(user.can_post_images) == on:
            continue
        user.can_post_images = True if on else None
        user.images_granted_at, user.images_granted_by = (clock.utcnow(), actor[:80]) if on else (None, None)
        audit.record(db, actor, "images_grant" if on else "images_revoke", target_type="user", target_id=user.public_id)
        effects.signal(user.id, "account")
        changed += 1
    db.flush()
    return {"changed": changed}


def lists(db: Session, limit: int = 200) -> dict:
    """«من نشر صورًا سابقًا» (newest first) and «لديهم الإذن الآن»."""
    rows = db.execute(select(Post.author_id, func.count(), func.max(Post.created_at))
                      .where(Post.media_id.is_not(None)).group_by(Post.author_id)
                      .order_by(func.max(Post.created_at).desc()).limit(limit)).all()
    users = {u.id: u for u in db.scalars(select(User).where(User.id.in_([r[0] for r in rows]))).all()} if rows else {}
    former = [_row(users[a], n, last) for a, n, last in rows if a in users and not users[a].is_system]
    holders = db.scalars(select(User).where(User.can_post_images.is_(True)).order_by(User.images_granted_at.desc())
                         .limit(limit)).all()
    return {"former": former, "holders": [_row(u) for u in holders]}
