"""User media: idea pictures and profile pictures — rules, viewing rights, removal.

Privacy rules enforced here (never in the frontend only):
* Telegram file_ids and message ids never leave the server; clients get short-lived signed URLs bound
  to their own session, issued only to people allowed to see the file at that moment.
* An idea picture is visible like its idea (hidden while pending approval or after reports). Posting one needs
  the per-account permission users.can_post_images, granted by an admin only (V6 phase 17).
* V6 phase 17: pictures in chats are switched off for good. Uploads are refused before a byte is read (also by
  the security middleware and Caddy); the old ones are deleted everywhere by purge_chat_images (admin_cli
  purge-chat-images), except those tied to a report, kept as evidence for the moderators.
"""

from __future__ import annotations

import contextvars
import logging
import re
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import HOUR, Settings
from app.errors import AppError, not_found
from app.models import MediaItem, Message, Post, User
from app.services import media_urls
from app.services.messaging import (
    Effects,
    _check_limits,
    _require_can_send,
    iso,
    require_adult,
)
from app.services.rate_limit import Limit
from app.i18n import N_, tr

log = logging.getLogger("dzplay.media_items")

MEDIA_ID = re.compile(r"^[a-f0-9]{32}$")
PURPOSE_KIND = {"idea": "image", "avatar": "image"}
MB = 1024 * 1024
# Session key of the current request (set by api.deps.current_user): signed URLs are bound to it.
MEDIA_KEY: contextvars.ContextVar[str | None] = contextvars.ContextVar("dz_media_key", default=None)


# ---------------------------------------------------------------------------
# limits shown to the phone before it uploads anything
# ---------------------------------------------------------------------------


def caption_check(settings: Settings, text: str | None) -> None:
    """Word filter for text that accompanies a picture."""
    if not text:
        return
    from app.services.moderation import scan

    blocked = {c.strip() for c in settings.CAPTION_BLOCK_CATEGORIES.split(",") if c.strip()}
    found = scan(text, settings.MODERATION_EXTRA_WORDS)
    if blocked & set(found.categories):
        raise AppError(400, "caption_blocked", "الوصف يحتوي كلمات غير مسموحة. عدّله ثم أعد المحاولة.")


def images_not_allowed() -> AppError:
    return AppError(403, "images_not_allowed", "نشر الصور غير مفعّل لحسابك. تمنحه الإدارة فقط.")


def chat_images_disabled() -> AppError:
    return AppError(410, "chat_images_disabled", "إرسال الصور في المحادثات متوقف نهائيًا.")


def can_post_images(user: User) -> bool:
    """V6 phase 17: a per-account permission, granted and taken back by an admin only (never by the membership)."""
    return bool(user.can_post_images)


def idea_quota(db: Session, settings: Settings, user: User) -> dict:
    limit = settings.IDEA_IMAGE_LIMIT_PER_24H
    since = clock.utcnow() - timedelta(hours=24)
    times = list(db.execute(select(MediaItem.attached_at).where(
        MediaItem.owner_id == user.id, MediaItem.purpose == "idea", MediaItem.attached_at.is_not(None),
        MediaItem.attached_at > since).order_by(MediaItem.attached_at)).scalars())
    used = len(times)
    next_at = times[used - limit] + timedelta(hours=24) if limit > 0 and used >= limit else None
    return {"enabled": bool(settings.IDEA_IMAGES_ENABLED and limit > 0), "limit": limit, "used": used,
            "remaining": max(0, limit - used), "next_at": iso(next_at), "allowed": can_post_images(user)}


def avatar_quota(db: Session, settings: Settings, user: User) -> dict:
    """V6 phase 3: profile pictures set in the last 24 h (AVATAR_CHANGES_PER_DAY)."""
    limit = settings.AVATAR_CHANGES_PER_DAY
    since = clock.utcnow() - timedelta(hours=24)
    times = list(db.execute(select(MediaItem.attached_at).where(
        MediaItem.owner_id == user.id, MediaItem.purpose == "avatar", MediaItem.attached_at.is_not(None),
        MediaItem.attached_at > since).order_by(MediaItem.attached_at)).scalars())
    used = len(times)
    next_at = times[used - limit] + timedelta(hours=24) if limit > 0 and used >= limit else None
    return {"limit": limit, "used": used, "remaining": max(0, limit - used), "next_at": iso(next_at)}


def _avatar_limit(quota: dict) -> AppError:
    return AppError(429, "avatar_limit", "يمكنك تغيير صورتك {n} مرات في اليوم. حاول لاحقًا.", params={"n": quota["limit"]},
                    retry_after=_retry(quota["next_at"]))


def upload_config(db: Session, settings: Settings, user: User, available: bool) -> dict:
    return {
        "available": available,
        "image_types": sorted(settings.image_types),
        "image_max_mb": settings.UPLOAD_IMAGE_MAX_MB,
        "image_min_side": settings.UPLOAD_IMAGE_MIN_SIDE,
        "image_max_side": settings.UPLOAD_IMAGE_MAX_SIDE,
        "device_max_side": settings.DEVICE_IMAGE_MAX_SIDE,
        "device_quality": settings.DEVICE_IMAGE_QUALITY,
        "nsfw": {"device": settings.DEVICE_NSFW_CHECK, "block": settings.NSFW_BLOCK_THRESHOLD,
                 "sexy": settings.NSFW_SEXY_THRESHOLD},
        "idea": idea_quota(db, settings, user),
        "avatar": avatar_quota(db, settings, user),
        "chat": {"enabled": False},  # V6 phase 17: off for good (kept for apps that still read it)
    }


# ---------------------------------------------------------------------------
# upload start (before a single byte of the file is read)
# ---------------------------------------------------------------------------


def begin_upload(db: Session, settings: Settings, limiter, user: User, *, purpose: object, length: int | None,
                 caption: object = None) -> tuple[MediaItem, dict]:
    _require_can_send(user)
    require_adult(user)
    if purpose == "chat":
        raise chat_images_disabled()
    if purpose not in PURPOSE_KIND:
        raise AppError(400, "invalid_purpose", "طلب غير صالح.")
    cap = int(settings.UPLOAD_IMAGE_MAX_MB * MB)
    if length is None:
        raise AppError(411, "length_required", "طلب غير صالح.")
    if length <= 0:
        raise AppError(400, "empty_file", "الملف فارغ.")
    if length > cap:
        raise AppError(413, "file_too_large", "الملف أكبر من الحد المسموح ({mb} MB).", params={"mb": cap // MB})
    if isinstance(caption, str):
        caption_check(settings, caption)
    if purpose == "idea":
        quota = idea_quota(db, settings, user)
        if not quota["enabled"]:
            raise AppError(403, "idea_images_off", "نشر الصور مع الأفكار متوقف حاليًا.")
        if not quota["allowed"]:  # V6 phase 17: the admin's permission (not the membership)
            raise images_not_allowed()
        if quota["remaining"] <= 0:
            raise AppError(429, "idea_image_limit", "يمكنك نشر صورة واحدة كل 24 ساعة.", retry_after=_retry(quota["next_at"]))
    elif purpose == "avatar":
        quota = avatar_quota(db, settings, user)
        if quota["remaining"] <= 0:
            raise _avatar_limit(quota)
    _check_limits(limiter, [Limit(f"upload_hour:{user.id}", settings.UPLOADS_PER_HOUR, HOUR)])
    return _new_item(db, settings, user, purpose, length)


def _new_item(db: Session, settings: Settings, user: User, purpose: str, length: int) -> tuple[MediaItem, dict]:
    kind = PURPOSE_KIND[purpose]
    item = MediaItem(owner_id=user.id, owner_public_id=user.public_id, purpose=purpose, kind=kind, state="uploaded",
                     source_size=length, created_at=clock.utcnow())
    db.add(item)
    db.flush()
    job = {"id": item.id, "kind": kind, "purpose": purpose, "types": sorted(settings.image_types),
           "min_side": settings.UPLOAD_IMAGE_MIN_SIDE, "max_side": settings.UPLOAD_IMAGE_MAX_SIDE,
           "max_pixels": settings.IMAGE_MAX_PIXELS, "nsfw": settings.SERVER_NSFW_CHECK,
           "nsfw_block": settings.NSFW_BLOCK_THRESHOLD, "nsfw_sexy": settings.NSFW_SEXY_THRESHOLD,
           "blur": False}
    return item, job


def begin_official_upload(db: Session, settings: Settings, length: int) -> tuple[MediaItem, dict]:
    """V6 phase 19: a picture for the official account (panel or bot): the same checks and storage, no quota."""
    from app.services.admin_content import official_user

    cap = int(settings.UPLOAD_IMAGE_MAX_MB * MB)
    if length <= 0:
        raise AppError(400, "empty_file", "الملف فارغ.")
    if length > cap:
        raise AppError(413, "file_too_large", "الملف أكبر من الحد المسموح ({mb} MB).", params={"mb": cap // MB})
    return _new_item(db, settings, official_user(db), "idea", length)


def _retry(next_at: str | None) -> int:
    from app.services.messaging import parse_iso

    when = parse_iso(next_at) if next_at else None
    return max(60, int((when - clock.utcnow()).total_seconds())) if when else 3600


def status_of(db: Session, user: User, media_id: object) -> dict:
    item = _own(db, user, media_id)
    return {"id": item.id, "state": item.state, "error": tr(item.error) if item.state in ("rejected", "failed") and item.error else None,
            "width": item.width, "height": item.height}


def _own(db: Session, user: User, media_id: object) -> MediaItem:
    if not isinstance(media_id, str) or not MEDIA_ID.match(media_id):
        raise not_found()
    item = db.get(MediaItem, media_id)
    if item is None or item.owner_id != user.id:
        raise not_found()
    return item


def claim(db: Session, user: User, media_id: object, purpose: str) -> MediaItem:
    """A finished upload of this user, for this purpose, not used yet."""
    item = _own(db, user, media_id)
    if item.purpose != purpose:
        raise not_found()
    if item.state in ("uploaded", "processing"):
        raise AppError(409, "media_processing", "الملف ما زال قيد المعالجة. انتظر لحظة.")
    if item.state in ("rejected", "failed"):
        raise AppError(400, "media_rejected", item.error or N_("الملف مرفوض."))
    if item.state != "ready" or item.attached_id:
        raise AppError(409, "media_used", "هذا الملف مستخدم بالفعل.")
    return item


# ---------------------------------------------------------------------------
# idea pictures
# ---------------------------------------------------------------------------


def lock_user(db: Session, user_id: str) -> None:
    """Serialize quota checks of one user (PostgreSQL row lock until commit): two publishes sent at the same
    moment cannot both pass a "1 per 24 h" check."""
    db.execute(select(User.id).where(User.id == user_id).with_for_update())


def claim_for_idea(db: Session, settings: Settings, user: User, media_id: object) -> MediaItem:
    item = claim(db, user, media_id, "idea")
    lock_user(db, user.id)
    quota = idea_quota(db, settings, user)
    if not quota["enabled"]:
        raise AppError(403, "idea_images_off", "نشر الصور مع الأفكار متوقف حاليًا.")
    if not quota["allowed"]:
        raise images_not_allowed()
    if quota["remaining"] <= 0:
        raise AppError(429, "idea_image_limit", "يمكنك نشر صورة واحدة كل 24 ساعة.", retry_after=_retry(quota["next_at"]))
    return item


def attach_to_post(db: Session, settings: Settings, item: MediaItem, post: Post, effects: Effects | None) -> None:
    item.state, item.attached_type, item.attached_id, item.attached_at = "attached", "post", post.id, clock.utcnow()
    if settings.IDEA_IMAGE_REQUIRE_APPROVAL:
        item.review = "pending"
        post.status = "pending"
    if effects is not None:
        from app.services import media_moderation

        effects.later(media_moderation.announce, item.id, "review" if item.review == "pending" else "published")


def post_media(db: Session, post: Post, viewer_id: str) -> dict | None:
    if not post.media_id:
        return None
    item = db.get(MediaItem, post.media_id)
    if item is None or item.state != "attached" or not item.tg_file_id:
        return None
    mine = post.author_id == viewer_id
    if item.hidden and not mine:
        return None
    key = MEDIA_KEY.get()
    settings = _settings()
    url = media_urls.media_url(settings, item.id, "img", key) if key and settings else None
    return {"kind": item.kind, "width": item.width, "height": item.height, "url": url,
            "review": item.review if mine else None}


def _settings() -> Settings | None:
    return _SETTINGS.get()


_SETTINGS: contextvars.ContextVar[Settings | None] = contextvars.ContextVar("dz_settings", default=None)


def bind_request(settings: Settings, session_key: str) -> None:
    """Called once per authenticated request: URLs serialised during it are signed for this session."""
    _SETTINGS.set(settings)
    MEDIA_KEY.set(session_key)


def remove_for_post(db: Session, post: Post, actor: str, effects: Effects | None) -> None:
    """The idea was deleted: its picture goes too (Telegram + cache), unless kept as evidence."""
    if not post.media_id:
        return
    item = db.get(MediaItem, post.media_id)
    if item is not None and item.state not in ("removed", "expired"):
        discard(db, item, actor, "removed", effects)


# ---------------------------------------------------------------------------
# V6 phase 17: chat pictures — switched off for good
# ---------------------------------------------------------------------------


def message_media(db: Session, m: Message, viewer_id: str) -> dict:
    """An old picture message: shown as deleted (the file is gone, or kept only as evidence for the moderators)."""
    return {"kind": "image", "state": "expired", "blur": None}


def open_chat_image(db: Session, settings: Settings, user: User, message_id: object, effects: Effects) -> dict:
    raise AppError(410, "media_expired", "انتهت صلاحية الصورة.")


def _purge_plan(db: Session) -> tuple[list[MediaItem], list[MediaItem]]:
    """(chat pictures to delete, reported chat pictures to keep as evidence) — only what still needs work."""
    from app.models import Report

    reported = set(db.scalars(select(Report.media_id).where(Report.media_id.is_not(None))))
    gone, keep = [], []
    for item in db.scalars(select(MediaItem).where(MediaItem.purpose == "chat")).all():
        done = item.state in ("removed", "expired")
        evidence = bool(item.legal_hold) and (item.id in reported or (item.reports_count or 0) > 0)
        if evidence:  # reported and still held (a moderator's «keep» or the end of the retention released it)
            if not done:
                keep.append(item)
        elif not done or item.legal_hold or item.tg_file_id or item.tg_message_id:
            gone.append(item)
    return gone, keep


def chat_images_left(db: Session) -> int:
    gone, keep = _purge_plan(db)
    return len(gone) + len(keep)


def purge_chat_images(db: Session, actor: str = "system:purge") -> dict:
    """Every chat picture that is not tied to a report: made unreachable now (state, file id, cache) and listed for
    deletion from the Telegram storage channel (the caller deletes them and clears tg_message_id). A reported one is
    kept as evidence (legal_hold) until MEDIA_EVIDENCE_RETENTION_DAYS, as before. Pictures held by the old archive
    mode without a report go too. Safe to run again: only what is left is touched."""
    gone, keep = _purge_plan(db)
    for item in keep:
        discard(db, item, actor, item.state if item.state in ("removed", "expired") else "removed", None)
    storage = []
    for item in gone:
        item.legal_hold = False
        discard(db, item, actor, item.state if item.state in ("removed", "expired") else "removed", None)
        if item.tg_message_id:
            storage.append((item.id, item.tg_message_id))
    db.flush()
    return {"removed": len(gone), "kept": len(keep), "storage": storage}


# ---------------------------------------------------------------------------
# V6 phase 3: profile pictures
# ---------------------------------------------------------------------------


def avatar_url(user: User | None) -> str | None:
    """Signed, session-bound URL of the user's profile picture (None: no picture, removed or hidden)."""
    if user is None or not user.avatar_media_id:
        return None
    from sqlalchemy.orm import object_session

    db = object_session(user)
    item = db.get(MediaItem, user.avatar_media_id) if db is not None else None
    if item is None or item.state != "attached" or item.hidden or not item.tg_file_id:
        return None
    key, settings = MEDIA_KEY.get(), _settings()
    return media_urls.media_url(settings, item.id, "img", key) if key and settings else None


def set_avatar(db: Session, settings: Settings, user: User, media_id: object, effects: Effects) -> dict:
    _require_can_send(user)
    item = claim(db, user, media_id, "avatar")
    lock_user(db, user.id)
    quota = avatar_quota(db, settings, user)
    if quota["remaining"] <= 0:
        raise _avatar_limit(quota)
    old = db.get(MediaItem, user.avatar_media_id) if user.avatar_media_id else None
    if old is not None and old.state == "attached":
        discard(db, old, "owner", "removed", effects)
    item.state, item.attached_type, item.attached_id, item.attached_at = "attached", "user", user.id, clock.utcnow()
    user.avatar_media_id = item.id
    db.flush()
    from app.services import media_moderation

    effects.later(media_moderation.announce, item.id, "published")
    return {"avatar_url": avatar_url(user)}


def remove_avatar(db: Session, user: User, effects: Effects) -> dict:
    old = db.get(MediaItem, user.avatar_media_id) if user.avatar_media_id else None
    if old is not None and old.state == "attached":
        discard(db, old, "owner", "removed", effects)
    user.avatar_media_id = None
    return {"avatar_url": None}


# ---------------------------------------------------------------------------
# who may download a file (/media/<id>/<variant>)
# ---------------------------------------------------------------------------


def can_view(db: Session, user: User, item: MediaItem) -> str | None:
    """Cache-Control for an allowed viewer, None when the file must not be served."""
    if item.state != "attached" or not item.tg_file_id:
        return None
    if item.attached_type == "post":
        post = db.get(Post, item.attached_id)
        if post is None:
            return None
        if post.author_id == user.id:
            return "private, max-age=600"
        if post.status != "visible" or item.hidden:
            return None
        return "private, max-age=3600"
    if item.attached_type == "user":  # V6: profile picture, seen by any signed-in member
        if item.attached_id == user.id:
            return "private, max-age=600"
        owner = db.get(User, item.attached_id)
        if item.hidden or owner is None or owner.avatar_media_id != item.id or owner.status == "banned":
            return None
        return "private, max-age=3600"
    return None  # V6 phase 17: chat pictures are never served again


# ---------------------------------------------------------------------------
# removal (owner, moderators, expiry)
# ---------------------------------------------------------------------------


def discard(db: Session, item: MediaItem, actor: str, state: str, effects: Effects | None,
            purge_storage: bool = True) -> None:
    """Make the file unreachable now: state, cache files. The Telegram copy is deleted in the background
    (now, or after the report window for expired chat pictures), except while kept as evidence (legal_hold)."""
    from app.services.media_moderation import delete_from_storage

    item.state = state
    item.removed_at = item.removed_at or clock.utcnow()
    item.removed_by = item.removed_by or actor
    if purge_storage and effects is not None and not item.legal_hold and item.tg_message_id:
        effects.later(delete_from_storage, item.id)
    if not item.legal_hold:
        item.tg_file_id = None  # forgotten: nothing can fetch it again
    _forget_cache(db, item.id)


_store = None  # MediaStore of the running app (set by install)


def _forget_cache(db: Session, item_id: str) -> None:
    if _store is not None:
        _store.forget_asset(db, item_id)


def install(store) -> None:
    global _store
    _store = store


def tick(db: Session, settings: Settings, effects: Effects) -> dict:
    """Drop uploads never used, purge old evidence (V6 phase 17: chat pictures are handled by purge_chat_images)."""
    now = clock.utcnow()
    done = {"orphans": 0, "evidence": 0}
    stale = now - timedelta(hours=2)
    for item in db.execute(select(MediaItem).where(
            MediaItem.state.in_(("ready", "uploaded", "processing")), MediaItem.created_at < stale).limit(200)).scalars().all():
        if item.state == "ready":  # uploaded but never posted
            discard(db, item, "unused", "removed", effects)
        else:  # stuck (worker restarted)
            item.state, item.error = "failed", N_("انتهت مهلة المعالجة.")
        done["orphans"] += 1
    old = now - timedelta(days=2)
    for item in db.execute(select(MediaItem).where(
            MediaItem.state.in_(("rejected", "failed")), MediaItem.created_at < old).limit(500)).scalars().all():
        db.delete(item)  # nothing of it is stored anywhere
    gone = now - timedelta(days=30)
    for item in db.execute(select(MediaItem).where(
            MediaItem.state.in_(("removed", "expired")), MediaItem.legal_hold.is_not(True),
            MediaItem.tg_message_id.is_(None), MediaItem.removed_at < gone).limit(500)).scalars().all():
        db.delete(item)
    keep_until = now - timedelta(days=settings.MEDIA_EVIDENCE_RETENTION_DAYS)
    for item in db.execute(select(MediaItem).where(
            MediaItem.legal_hold.is_(True), MediaItem.state.in_(("removed", "expired")),
            MediaItem.removed_at < keep_until).limit(100)).scalars().all():
        item.legal_hold = False
        if item.tg_message_id:
            from app.services.media_moderation import delete_from_storage

            effects.later(delete_from_storage, item.id)
        item.tg_file_id = None
        done["evidence"] += 1
    db.flush()
    return done
