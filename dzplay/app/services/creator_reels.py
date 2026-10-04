"""V5 «استوديو DZPLAY»: verified creators publish short videos into the Reels feed.

* Verified accounts only (checked on the server at every step).
* CREATOR_REEL_LIMIT_PER_24H (rejected ones count if REJECTED_COUNTS_TOWARD_LIMIT), duration/size limits,
  REELS_REQUIRE_APPROVAL (approve / reject from the moderation group or the panel).
* With the name: name + star, linked to the profile (optionally listed on it). Without the name: the reel
  looks like platform content and can never be listed on the profile.
* The video file goes through the same pipeline as every upload (worker → Telegram storage). The reel's
  asset re-uses the upload's id, so the prepared cache files are shared.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import MediaItem, Reel, ReelAsset, User
from app.services import reels
from app.services.messaging import Effects, iso

REVIEW_AR = {"pending": "قيد المراجعة", "approved": "منشور", "rejected": "مرفوض", "removed": "حُذف بقرار الإشراف"}


def require_creator(settings: Settings, user: User) -> None:
    if not settings.CREATOR_REELS_ENABLED:
        raise AppError(403, "studio_off", "الاستوديو متوقف حاليًا.")
    if user.verified_at is None:
        raise AppError(403, "not_verified", "الاستوديو متاح للحسابات الموثّقة (النجمة الزرقاء) فقط.")


def quota(db: Session, settings: Settings, user: User) -> dict:
    limit = settings.CREATOR_REEL_LIMIT_PER_24H
    since = clock.utcnow() - timedelta(hours=24)
    q = select(Reel.created_at).where(Reel.owner_id == user.id, Reel.source == "studio", Reel.created_at > since)
    if not settings.REJECTED_COUNTS_TOWARD_LIMIT:
        q = q.where(or_(Reel.review_status.is_(None), Reel.review_status != "rejected"))
    times = sorted(db.execute(q).scalars())
    used = len(times)
    next_at = times[used - limit] + timedelta(hours=24) if limit > 0 and used >= limit else None
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "next_at": iso(next_at)}


def _own(db: Session, user: User, reel_id: object) -> Reel:
    reel = db.get(Reel, reel_id) if isinstance(reel_id, str) and len(reel_id) <= 32 else None
    if reel is None or reel.owner_id != user.id or reel.source != "studio":
        raise not_found()
    return reel


def _item_of(db: Session, reel: Reel) -> MediaItem | None:
    return db.scalar(select(MediaItem).where(MediaItem.attached_type == "reel", MediaItem.attached_id == reel.id))


def view(settings: Settings, reel: Reel, skey: str | None, asset: ReelAsset | None = None) -> dict:
    out = {"id": reel.id, "caption": reel.caption, "status": reel.review_status or "approved",
           "status_label": REVIEW_AR.get(reel.review_status or "approved"), "note": reel.review_note,
           "visible": reel.status == "visible", "show_author": bool(reel.show_author),
           "show_on_profile": bool(reel.show_on_profile), "created_at": iso(reel.created_at),
           "likes": reel.likes_count, "views": reel.views_count, "comments": reel.comments_count}
    if asset is not None and skey and reel.review_status not in ("rejected", "removed"):
        out["poster"] = reels.media_url(settings, asset.id, "poster", skey)
        out["src"] = reels.media_url(settings, asset.id, "mp4", skey)
        out["duration"] = asset.duration
    return out


def submit(db: Session, settings: Settings, user: User, *, media_id: object, caption: object, show_author: object,
           show_on_profile: object, effects: Effects, skey: str | None) -> dict:
    from app.services import media_items
    from app.services.media_moderation import announce
    from app.services.messaging import _require_can_send

    _require_can_send(user)
    require_creator(settings, user)
    item = media_items.claim(db, user, media_id, "reel")
    media_items.lock_user(db, user.id)
    q = quota(db, settings, user)
    if q["remaining"] <= 0:
        raise AppError(429, "reel_limit", "وصلت إلى حد الفيديوهات اليوم.")
    text = caption if isinstance(caption, str) else ""
    media_items.caption_check(settings, text)
    named = show_author is True
    reel = reels.create_reel(db, settings, "video", text)
    approval = settings.REELS_REQUIRE_APPROVAL
    reel.owner_id, reel.source = user.id, "studio"
    reel.show_author, reel.show_on_profile = named, bool(named and show_on_profile is True)
    reel.review_status = "pending" if approval else "approved"
    reel.status = "hidden" if approval else "visible"
    asset = ReelAsset(id=item.id, reel_id=reel.id, position=0, kind="video", tg_file_id=item.tg_file_id,
                      tg_unique_id=item.tg_unique_id, tg_message_id=item.tg_message_id, source_size=item.source_size,
                      width=item.width, height=item.height, duration=item.duration, ready=bool(item.ready))
    db.add(asset)
    item.state, item.attached_type, item.attached_id, item.attached_at = "attached", "reel", reel.id, clock.utcnow()
    item.review = "pending" if approval else None
    db.flush()
    effects.later(announce, item.id, "review" if approval else "published")
    return {"reel": view(settings, reel, skey, asset)}


def approve(db: Session, settings: Settings, reel: Reel, actor: str, effects: Effects) -> None:
    reel.review_status, reel.review_note, reel.status, reel.updated_at = "approved", None, "visible", clock.utcnow()
    if reel.owner_id:
        effects.signal(reel.owner_id, "studio")


def reject(db: Session, settings: Settings, reel: Reel, actor: str, effects: Effects, removed: bool = False,
           note: str = "") -> None:
    reel.review_status = "removed" if removed else "rejected"
    reel.review_note = (note or ("حُذف لمخالفة إرشادات المجتمع." if removed else "لا يتوافق مع إرشادات المجتمع."))[:255]
    reel.status, reel.updated_at = "hidden", clock.utcnow()
    if reel.owner_id:
        effects.signal(reel.owner_id, "studio")


def my_reels(db: Session, settings: Settings, user: User, skey: str | None, studio_only: bool = False) -> dict:
    q = select(Reel).where(Reel.owner_id == user.id, Reel.source == "studio").order_by(Reel.created_at.desc())
    if studio_only:  # the studio window keeps a video only CREATOR_UPLOAD_CHAT_TTL
        q = q.where(Reel.created_at > clock.utcnow() - timedelta(seconds=settings.CREATOR_UPLOAD_CHAT_TTL))
    rows = db.execute(q.limit(100)).scalars().all()
    assets = {a.reel_id: a for a in db.execute(select(ReelAsset).where(ReelAsset.reel_id.in_([r.id for r in rows]))).scalars()} if rows else {}
    return {"reels": [view(settings, r, skey, assets.get(r.id)) for r in rows], "quota": quota(db, settings, user),
            "limits": {"max_seconds": settings.CREATOR_REEL_MAX_SECONDS, "min_seconds": settings.CREATOR_REEL_MIN_SECONDS,
                       "max_mb": settings.CREATOR_REEL_MAX_MB, "approval": settings.REELS_REQUIRE_APPROVAL,
                       "window_days": max(1, settings.CREATOR_UPLOAD_CHAT_TTL // 86400)}}


def update(db: Session, settings: Settings, user: User, reel_id: object, body: dict, skey: str | None) -> dict:
    reel = _own(db, user, reel_id)
    if "show_author" in body:
        reel.show_author = body.get("show_author") is True
    if "show_on_profile" in body:
        reel.show_on_profile = body.get("show_on_profile") is True
    if not reel.show_author:
        reel.show_on_profile = False  # a reel without the name can never be linked to the profile
    reel.updated_at = clock.utcnow()
    asset = db.scalar(select(ReelAsset).where(ReelAsset.reel_id == reel.id))
    return {"reel": view(settings, reel, skey, asset)}


def delete(db: Session, store, user: User, reel_id: object, effects: Effects) -> None:
    from app.services.media_items import discard

    reel = _own(db, user, reel_id)
    item = _item_of(db, reel)
    if item is not None and item.state not in ("removed", "expired"):
        discard(db, item, "owner", "removed", effects)
    reels.delete_reel(db, store, reel)


def author_of(db: Session, reel_list: list[Reel]) -> dict[str, dict]:
    """Author block for named creator reels (name + star + profile link); nothing for the others."""
    from app.services import ideas, names

    named = [r for r in reel_list if r.owner_id and r.show_author]
    if not named:
        return {}
    users = {u.id: u for u in db.execute(select(User).where(User.id.in_({r.owner_id for r in named}))).scalars()}
    out = {}
    for r in named:
        u = users.get(r.owner_id)
        if u is None or u.status == "banned":
            continue
        out[r.id] = {"name": names.shown_name(u), "verified": u.verified_at is not None, "gender": names.public_gender(u),
                     "ref": ideas.profile_ref_for(db, u.id)}
    return out


def profile_reels(db: Session, settings: Settings, owner_id: str, skey: str | None) -> list[dict]:
    rows = db.execute(select(Reel).where(Reel.owner_id == owner_id, Reel.status == "visible", Reel.show_author.is_(True),
                                         Reel.show_on_profile.is_(True)).order_by(Reel.created_at.desc()).limit(60)).scalars().all()
    assets = {a.reel_id: a for a in db.execute(select(ReelAsset).where(ReelAsset.reel_id.in_([r.id for r in rows]))).scalars()} if rows else {}
    out = []
    for r in rows:
        a = assets.get(r.id)
        if a is None or not skey:
            continue
        out.append({"id": r.id, "caption": r.caption, "likes": r.likes_count, "poster": reels.media_url(settings, a.id, "poster", skey),
                    "src": reels.media_url(settings, a.id, "mp4", skey)})
    return out


def published_stats(db: Session, user_id: str) -> tuple[int, int]:
    count, likes = db.execute(select(func.count(Reel.id), func.coalesce(func.sum(Reel.likes_count), 0)).where(
        Reel.owner_id == user_id, Reel.source == "studio", Reel.status == "visible")).one()
    return int(count or 0), int(likes or 0)


def report(db: Session, settings: Settings, limiter, user: User, reel_id: object, *, reason: object, details: object,
           effects: Effects) -> dict:
    """Report a creator reel (platform content is reported through support)."""
    import json

    from app.models import Report
    from app.services.media_moderation import on_report
    from app.services.messaging import REPORT_REASONS, _check_limits, _clean_details, _maybe_auto_suspend
    from app.services.rate_limit import Limit

    reel = db.get(Reel, reel_id) if isinstance(reel_id, str) and len(reel_id) <= 32 else None
    if reel is None or reel.status != "visible" or not reel.owner_id:
        raise not_found()
    if reel.owner_id == user.id:
        raise AppError(400, "cannot_report_own", "لا يمكنك الإبلاغ عن محتواك.")
    if reason not in REPORT_REASONS:
        raise AppError(400, "invalid_reason", "اختر سبب البلاغ.")
    item = _item_of(db, reel)
    dup = db.scalar(select(Report.id).where(Report.reporter_id == user.id, Report.media_id == (item.id if item else None),
                                            Report.status == "open")) if item else None
    if dup:
        return {"id": dup, "duplicate": True}
    _check_limits(limiter, [Limit(f"report:{user.id}", settings.MAX_REPORTS_PER_HOUR, 3600)])
    now = clock.utcnow()
    rep = Report(reporter_id=user.id, reported_user_id=reel.owner_id, media_id=item.id if item else None, reason=reason,
                 details=_clean_details(details),
                 snapshot=json.dumps([{"content": f"Reel: {reel.caption}", "created_at": iso(reel.created_at)}], ensure_ascii=False),
                 created_at=now, expires_at=now + timedelta(seconds=settings.REPORT_RETENTION))
    db.add(rep)
    db.flush()
    if item is not None:
        on_report(db, settings, item, str(reason), effects)
        if item.hidden:
            reel.status = "hidden"  # auto-hidden until a moderator decides ("keep" shows it again)
            reel.review_status = "pending"
    _maybe_auto_suspend(db, settings, reel.owner_id)
    return {"id": rep.id, "duplicate": False}
