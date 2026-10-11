"""V6 phase 4: in-app notifications — written in the same transaction as what they announce, pushed to the
phone with a realtime "notify" signal after the commit. Never sent to the person who caused them."""

from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import clock, i18n
from app.models import Comment, Notification, User
from app.services import brand
from app.services.content import preview

KEEP_DAYS = 90
PAGE = 50


def msg(msgid: str, *, translate: tuple[str, ...] = (), **params) -> dict:
    """V6 phase 12: the data of a notification shown in the reader's language: an Arabic msgid (marked with N_) and
    its {params}; the params named in `translate` are msgids too. Numbers are formatted when shown."""
    data: dict = {"msg": msgid, "params": params}
    if translate:
        data["params"]["_t"] = list(translate)
    return data


def text_of(data: dict | None, lang: str | None = None) -> str | None:
    """The text of a notification in lang (older rows hold an Arabic "text": translated when it is a known text)."""
    if not data:
        return None
    if data.get("msg"):
        return i18n.tr_params(data["msg"], lang, data.get("params"))
    return i18n.tr(data["text"], lang) if data.get("text") else None


def create(db: Session, user_id: str | None, kind: str, effects, *, actor_id: str | None = None,
           post_id: str | None = None, comment_id: str | None = None, data: dict | None = None) -> None:
    if not user_id or user_id == actor_id:
        return
    db.add(Notification(user_id=user_id, kind=kind, actor_id=actor_id, post_id=post_id, comment_id=comment_id,
                        data=json.dumps(data, ensure_ascii=False) if data else None, created_at=clock.utcnow()))
    if effects is not None:
        effects.signal(user_id, "notify")


def unread(db: Session, user_id: str) -> int:
    return int(db.scalar(select(func.count()).select_from(Notification).where(
        Notification.user_id == user_id, Notification.read_at.is_(None))) or 0)


def listing(db: Session, user: User) -> dict:
    from app.services import names
    from app.services.media_items import avatar_url

    since = clock.utcnow() - timedelta(days=KEEP_DAYS)
    rows = list(db.execute(select(Notification).where(Notification.user_id == user.id, Notification.created_at > since)
                           .order_by(Notification.created_at.desc()).limit(PAGE)).scalars())
    actors = {u.id: u for u in db.execute(select(User).where(User.id.in_({n.actor_id for n in rows if n.actor_id}))).scalars()}
    comments = {c.id: c for c in db.execute(select(Comment).where(Comment.id.in_({n.comment_id for n in rows if n.comment_id}))).scalars()}
    out = []
    for n in rows:
        a = actors.get(n.actor_id)
        team = bool(a and (a.is_system or a.is_official))
        c = comments.get(n.comment_id)
        out.append({
            "id": n.id, "kind": n.kind, "post_id": n.post_id, "comment_id": n.comment_id,
            "actor": None if a is None else {"name": brand.team_name() if team else names.shown_name(a),
                                             "public_id": None if team else a.public_id,
                                             "avatar_url": None if team else avatar_url(a)},
            "preview": preview(c.content, 120) if c is not None and c.deleted_at is None else None,
            "data": _shown(n.data),
            "created_at": n.created_at.isoformat(timespec="milliseconds") + "Z", "read": n.read_at is not None,
        })
    return {"notifications": out, "unread": unread(db, user.id)}


def _shown(raw: str | None) -> dict | None:
    if not raw:
        return None
    data = json.loads(raw)
    text = text_of(data)
    return {**{k: v for k, v in data.items() if k not in ("msg", "params")}, **({"text": text} if text else {})}


def mark_read(db: Session, user: User) -> dict:
    db.execute(update(Notification).where(Notification.user_id == user.id, Notification.read_at.is_(None))
               .values(read_at=clock.utcnow()))
    return {"unread": 0}


def purge_old(db: Session) -> int:
    from sqlalchemy import delete

    return db.execute(delete(Notification).where(
        Notification.created_at < clock.utcnow() - timedelta(days=KEEP_DAYS))).rowcount or 0
