"""Team comments from the admin panel (disclosed in the privacy policy).

From the library (chosen items or N random from a category) or new text, posted from internal
system accounts shown as "dzplay" (default) or from the official account with its badge. The
same text is never posted twice on the same idea. Immediate or spread over a duration
(EngagementJob advanced by `tick`).

V6: the V5 "boost" (numbers added to likes/dislikes) was removed: counts are real only.
"""

from __future__ import annotations

import json
import random
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, not_found
from app.models import Block, CannedComment, Comment, CommentCategory, EngagementJob, Post, User
from app.services.content import clean_message
from app.services.messaging import Effects, iso
from app.services.moderation import normalize

TARGETS = {"idea": Post}
DEFAULT_CATEGORIES = {"welcome": "ترحيب", "encourage": "تشجيع", "support": "دعم", "engage": "تفاعل", "humor": "فكاهة",
                      "thanks": "شكر", "other": "أخرى"}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _target(db: Session, target_type: str, target_id: str):
    model = TARGETS.get(target_type)
    if model is None:
        raise AppError(400, "invalid_target", "نوع المنشور غير صالح.")
    obj = db.get(model, str(target_id)[:32])
    if obj is None:
        raise not_found()
    return obj


def _check_targets(settings: Settings, target_type: str, ids: list[str]) -> list[str]:
    if target_type not in TARGETS:
        raise AppError(400, "invalid_target", "نوع المنشور غير صالح.")
    ids = list(dict.fromkeys(str(i)[:32] for i in ids if i))
    if not ids or len(ids) > settings.ENGAGEMENT_MAX_TARGETS:
        raise AppError(400, "invalid_targets", f"اختر من 1 إلى {settings.ENGAGEMENT_MAX_TARGETS} منشورًا.")
    return ids


def _window(settings: Settings, minutes: int | None) -> tuple[datetime, datetime] | None:
    if not minutes:
        return None
    if minutes < 0 or minutes > settings.ENGAGEMENT_MAX_DURATION_HOURS * 60:
        raise AppError(400, "invalid_duration", "مدة غير صالحة.")
    now = clock.utcnow()
    return now, now + timedelta(minutes=minutes)


# ---------------------------------------------------------------------------
# accounts the team comments from
# ---------------------------------------------------------------------------


def official_account(db: Session) -> User:
    from app.services.admin_content import official_user

    return official_user(db)


def _system_pool(db: Session, settings: Settings) -> list[User]:
    pool = list(db.execute(select(User).where(User.is_system.is_(True))).scalars())
    while len(pool) < settings.SYSTEM_ACCOUNTS:
        u = User(email=f"system-{secrets.token_hex(6)}@dzplay.invalid", password_hash=None, google_sub=None,
                 status="active", is_system=True, privacy_ack_version=PRIVACY_VERSION)
        db.add(u)
        pool.append(u)
    db.flush()
    return pool


def _author_for(db: Session, settings: Settings, appearance: str, owner_id: str | None) -> User:
    if appearance == "official":
        return official_account(db)
    pool = _system_pool(db, settings)
    if owner_id:  # an owner who blocked one of these accounts never hears from it again
        blocked = set(db.execute(select(Block.blocked_id).where(Block.blocker_id == owner_id)).scalars())
        pool = [u for u in pool if u.id not in blocked] or pool
    return random.choice(pool)


# ---------------------------------------------------------------------------
# library
# ---------------------------------------------------------------------------


def ensure_categories(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(CommentCategory)):
        return
    for key, name in DEFAULT_CATEGORIES.items():
        db.add(CommentCategory(id=key, name=name))
    db.flush()


def categories(db: Session) -> list[dict]:
    ensure_categories(db)
    counts = dict(db.execute(select(CannedComment.category, func.count()).group_by(CannedComment.category)).all())
    return [{"id": c.id, "name": c.name, "items": counts.get(c.id, 0)}
            for c in db.execute(select(CommentCategory).order_by(CommentCategory.created_at, CommentCategory.name)).scalars()]


def category_save(db: Session, cat_id: str | None, name: object) -> dict:
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
        raise AppError(400, "invalid_name", "اسم التصنيف غير صالح.")
    name = name.strip()
    clash = db.scalar(select(CommentCategory.id).where(CommentCategory.name == name))
    if clash and clash != cat_id:
        raise AppError(409, "duplicate", "هذا التصنيف موجود.")
    if cat_id:
        cat = db.get(CommentCategory, cat_id[:32])
        if cat is None:
            raise not_found()
        cat.name = name
    else:
        cat = CommentCategory(name=name)
        db.add(cat)
    db.flush()
    return {"id": cat.id, "name": cat.name}


def category_delete(db: Session, cat_id: str) -> None:
    cat = db.get(CommentCategory, cat_id[:32])
    if cat is None:
        raise not_found()
    if db.scalar(select(func.count()).select_from(CannedComment).where(CannedComment.category == cat.id)):
        raise AppError(409, "not_empty", "التصنيف يحتوي تعليقات. انقلها أو احذفها أولًا.")
    db.delete(cat)


def _item(c: CannedComment, names: dict[str, str]) -> dict:
    return {"id": c.id, "category": c.category, "category_name": names.get(c.category, c.category), "text": c.text,
            "usage_count": c.usage_count or 0, "updated_at": iso(c.updated_at)}


def library(db: Session, q: str = "", category: str = "") -> dict:
    ensure_categories(db)
    names = dict(db.execute(select(CommentCategory.id, CommentCategory.name)).all())
    stmt = select(CannedComment).order_by(CannedComment.category, CannedComment.created_at)
    if category:
        stmt = stmt.where(CannedComment.category == category)
    if q.strip():
        from app.services.admin_access import _like

        stmt = stmt.where(CannedComment.text.ilike(_like(q.strip()), escape="\\"))
    return {"items": [_item(c, names) for c in db.execute(stmt).scalars()], "categories": categories(db)}


def item_save(db: Session, settings: Settings, item_id: str | None, category: object, text: object) -> dict:
    ensure_categories(db)
    if not isinstance(category, str) or db.get(CommentCategory, category[:32]) is None:
        raise AppError(400, "invalid_category", "تصنيف غير صالح.")
    body = clean_message(text, settings.MAX_COMMENT_LENGTH, "allow_plain")
    now = clock.utcnow()
    if item_id:
        item = db.get(CannedComment, item_id[:32])
        if item is None:
            raise not_found()
        item.category, item.text, item.updated_at = category, body, now
    else:
        item = CannedComment(category=category, text=body, usage_count=0, created_at=now, updated_at=now)
        db.add(item)
    db.flush()
    return _item(item, dict(db.execute(select(CommentCategory.id, CommentCategory.name)).all()))


def item_delete(db: Session, item_id: str) -> None:
    item = db.get(CannedComment, item_id[:32])
    if item is None:
        raise not_found()
    db.delete(item)


def bulk_import(db: Session, settings: Settings, category: str, text: object) -> dict:
    ensure_categories(db)
    if not isinstance(category, str) or db.get(CommentCategory, category[:32]) is None:
        raise AppError(400, "invalid_category", "تصنيف غير صالح.")
    if not isinstance(text, str):
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    existing = {normalize(t) for t in db.execute(select(CannedComment.text).where(CannedComment.category == category)).scalars()}
    added = skipped = 0
    now = clock.utcnow()
    for line in text.splitlines()[:2000]:
        line = line.strip()
        if not line:
            continue
        try:
            body = clean_message(line, settings.MAX_COMMENT_LENGTH, "allow_plain")
        except AppError:
            skipped += 1
            continue
        key = normalize(body)
        if key in existing:
            skipped += 1
            continue
        existing.add(key)
        db.add(CannedComment(category=category, text=body, usage_count=0, created_at=now, updated_at=now))
        added += 1
    db.flush()
    return {"added": added, "skipped": skipped}


# ---------------------------------------------------------------------------
# team comments
# ---------------------------------------------------------------------------


def _team_texts_on(db: Session, target_type: str, target_id: str) -> set[str]:
    team = select(User.id).where((User.is_system.is_(True)) | (User.is_official.is_(True)))
    rows = db.execute(select(Comment.content).where(Comment.post_id == target_id, Comment.author_id.in_(team))).scalars()
    return {normalize(t) for t in rows}


def _resolve_items(db: Session, settings: Settings, source: dict) -> list[dict]:
    kind = source.get("kind")
    if kind == "text":
        text = clean_message(source.get("text"), settings.MAX_COMMENT_LENGTH, "allow_plain")
        return [{"text": text, "library_id": None}]
    if kind == "library":
        ids = [str(i)[:32] for i in (source.get("library_ids") or [])][: settings.ENGAGEMENT_MAX_COMMENTS]
        items = [db.get(CannedComment, i) for i in ids]
        if not items or any(i is None for i in items):
            raise AppError(400, "invalid_library", "اختر تعليقات من المكتبة.")
        return [{"text": i.text, "library_id": i.id} for i in items]
    if kind == "random":
        cat = str(source.get("category") or "")[:32]
        count = source.get("count")
        if not isinstance(count, int) or not (1 <= count <= settings.ENGAGEMENT_MAX_COMMENTS):
            raise AppError(400, "invalid_count", "عدد غير صالح.")
        items = list(db.execute(select(CannedComment).where(CannedComment.category == cat)).scalars())
        if not items:
            raise AppError(400, "empty_category", "هذا التصنيف فارغ.")
        return [{"text": i.text, "library_id": i.id, "pool": True} for i in items] + [{"count": count}]
    raise AppError(400, "invalid_source", "مصدر التعليقات غير صالح.")


def _plan_for_target(items: list[dict], already: set[str]) -> list[dict]:
    """Pick the comments for one target, never repeating a text already there (or twice in the plan)."""
    if items and "count" in items[-1]:  # random from a category
        count = items[-1]["count"]
        pool = [i for i in items[:-1] if normalize(i["text"]) not in already]
        random.shuffle(pool)
        chosen = pool[:count]
    else:
        chosen, seen = [], set()
        for i in items:
            key = normalize(i["text"])
            if key not in already and key not in seen:
                seen.add(key)
                chosen.append(i)
    return [{"text": i["text"], "library_id": i.get("library_id")} for i in chosen]


def _post_one(db: Session, settings: Settings, target_type: str, target, item: dict, appearance: str,
              effects: Effects) -> bool:
    if normalize(item["text"]) in _team_texts_on(db, target_type, target.id):
        return False
    if target.status != "visible":
        return False
    author = _author_for(db, settings, appearance, target.author_id)
    db.add(Comment(post_id=target.id, author_id=author.id, content=item["text"], created_at=clock.utcnow()))
    db.execute(update(Post).where(Post.id == target.id).values(
        comments_count=Post.comments_count + 1, unseen_comments_count=Post.unseen_comments_count + 1))
    effects.signal(target.author_id, "comment")
    if item.get("library_id"):
        db.execute(update(CannedComment).where(CannedComment.id == item["library_id"])
                   .values(usage_count=func.coalesce(CannedComment.usage_count, 0) + 1))
    db.flush()
    return True


def post_comments(db: Session, settings: Settings, *, target_type: str, ids: list[str], source: dict, appearance: str,
                  duration_minutes: int | None, actor: str, effects: Effects) -> dict:
    ids = _check_targets(settings, target_type, ids)
    if appearance not in ("dzplay", "official"):
        raise AppError(400, "invalid_appearance", "invalid appearance")
    items = _resolve_items(db, settings, source)
    window = _window(settings, duration_minutes)
    batch = secrets.token_hex(8)
    report = []
    for tid in ids:
        target = _target(db, target_type, tid)
        plan = _plan_for_target(items, _team_texts_on(db, target_type, target.id))
        if window is None:
            posted = sum(_post_one(db, settings, target_type, target, it, appearance, effects) for it in plan)
            report.append({"id": target.id, "planned": len(plan), "posted": posted})
        else:
            if plan:
                db.add(EngagementJob(batch_id=batch, kind="comment", target_type=target_type, target_id=target.id,
                                     total=len(plan), applied=0, payload=json.dumps({"items": plan, "appearance": appearance},
                                                                                    ensure_ascii=False),
                                     start_at=window[0], end_at=window[1], created_by=actor))
            report.append({"id": target.id, "planned": len(plan), "posted": 0})
    db.flush()
    return {"batch_id": batch, "gradual": window is not None, "targets": report}


# ---------------------------------------------------------------------------
# scheduler
# ---------------------------------------------------------------------------


def tick(db: Session, settings: Settings, effects: Effects, now: datetime | None = None) -> int:
    """Advance every running job to where it should be now. Returns the number of jobs touched."""
    now = now or clock.utcnow()
    touched = 0
    for job in db.execute(select(EngagementJob).where(EngagementJob.status == "running")).scalars().all():
        span = max(1.0, (job.end_at - job.start_at).total_seconds())
        frac = min(1.0, max(0.0, (now - job.start_at).total_seconds() / span))
        due = int(job.total * frac) if job.total >= 0 else -int(-job.total * frac)
        if now >= job.end_at:
            due = job.total
        model = TARGETS.get(job.target_type)
        target = db.get(model, job.target_id) if model is not None and job.kind == "comment" else None
        if target is None:
            job.status = "failed"
            continue
        data = json.loads(job.payload or "{}")
        for item in data.get("items", [])[job.applied:due]:
            _post_one(db, settings, job.target_type, target, item, data.get("appearance", "dzplay"), effects)
            job.applied += 1
            touched += 1
        if job.applied == job.total or now >= job.end_at:
            job.status = "done"
    db.flush()
    return touched


def jobs(db: Session, status: str = "", limit: int = 100) -> list[dict]:
    stmt = (select(EngagementJob).where(EngagementJob.kind == "comment")
            .order_by(EngagementJob.created_at.desc()).limit(min(limit, 500)))
    if status:
        stmt = stmt.where(EngagementJob.status == status)
    return [{"id": j.id, "batch_id": j.batch_id, "kind": j.kind, "target_type": j.target_type, "target_id": j.target_id,
             "total": j.total, "applied": j.applied, "status": j.status, "start_at": iso(j.start_at),
             "end_at": iso(j.end_at), "created_by": j.created_by} for j in db.execute(stmt).scalars()]


def cancel(db: Session, job_or_batch: str) -> int:
    key = job_or_batch[:32]
    n = db.execute(update(EngagementJob).where(EngagementJob.status == "running",
                                               (EngagementJob.id == key) | (EngagementJob.batch_id == key))
                   .values(status="cancelled")).rowcount or 0
    if not n:
        raise not_found()
    return n
