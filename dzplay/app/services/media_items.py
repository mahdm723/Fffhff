"""User media (V5): idea pictures and ephemeral chat pictures — rules, viewing rights, expiry.

Privacy rules enforced here (never in the frontend only):
* Telegram file_ids and message ids never leave the server; clients get short-lived signed URLs bound
  to their own session, issued only to people allowed to see the file at that moment.
* A chat picture is shown only to its recipient, only after they tap it, and only for
  CHAT_IMAGE_TTL_AFTER_VIEW seconds; then it is deleted from the cache and from Telegram (unless it was
  reported: then it is kept as evidence for the moderators). Expired URLs answer 404.
* An idea picture is visible like its idea (hidden while pending approval or after reports).
"""

from __future__ import annotations

import contextvars
import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import HOUR, Settings
from app.errors import AppError, not_found
from app.models import Conversation, MediaItem, Message, Post, User
from app.services import media_urls
from app.services.messaging import (
    Effects,
    _blocked_between,
    _check_limits,
    _require_can_send,
    iso,
    require_adult,
)
from app.services.rate_limit import Limit

log = logging.getLogger("dzplay.media_items")

MEDIA_ID = re.compile(r"^[a-f0-9]{32}$")
PURPOSE_KIND = {"idea": "image", "chat": "image", "avatar": "image"}
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


def idea_quota(db: Session, settings: Settings, user: User) -> dict:
    limit = settings.IDEA_IMAGE_LIMIT_PER_24H
    since = clock.utcnow() - timedelta(hours=24)
    times = list(db.execute(select(MediaItem.attached_at).where(
        MediaItem.owner_id == user.id, MediaItem.purpose == "idea", MediaItem.attached_at.is_not(None),
        MediaItem.attached_at > since).order_by(MediaItem.attached_at)).scalars())
    used = len(times)
    next_at = times[used - limit] + timedelta(hours=24) if limit > 0 and used >= limit else None
    return {"enabled": bool(settings.IDEA_IMAGES_ENABLED and limit > 0), "limit": limit, "used": used,
            "remaining": max(0, limit - used), "next_at": iso(next_at)}


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
    return AppError(429, "avatar_limit", f"يمكنك تغيير صورتك {quota['limit']} مرات في اليوم. حاول لاحقًا.",
                    retry_after=_retry(quota["next_at"]))


def _chat_sent_last_hour(db: Session, user: User) -> int:
    since = clock.utcnow() - timedelta(seconds=HOUR)
    return db.scalar(select(func.count()).select_from(MediaItem).where(
        MediaItem.owner_id == user.id, MediaItem.purpose == "chat", MediaItem.attached_at > since)) or 0


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
        "chat": {"enabled": settings.CHAT_IMAGES_ENABLED, "per_hour": settings.CHAT_IMAGE_PER_HOUR,
                 "ttl_after_view": settings.CHAT_IMAGE_TTL_AFTER_VIEW},
    }


# ---------------------------------------------------------------------------
# upload start (before a single byte of the file is read)
# ---------------------------------------------------------------------------


def chat_target(db: Session, user: User, conversation_id: object) -> Conversation:
    """A chat the user may send a picture to: active, not blocked, and the other side has replied."""
    from app.services.messaging import _get_visible_conversation, require_open

    conv = _get_visible_conversation(db, user, conversation_id if isinstance(conversation_id, str) else "")
    require_open(conv)  # V6: old anonymous chats are read-only
    peer = conv.peer_of(user.id)
    if conv.status != "active" or _blocked_between(db, user.id, peer):
        raise AppError(403, "conversation_closed", "هذه المحادثة لم تعد متاحة.")
    if not conv.has_sent(peer):
        raise AppError(403, "peer_not_replied", "يمكنك إرسال الصور بعد أن يرد عليك الطرف الآخر.")
    return conv


def begin_upload(db: Session, settings: Settings, limiter, user: User, *, purpose: object, conversation_id: object,
                 length: int | None, caption: object = None) -> tuple[MediaItem, dict]:
    _require_can_send(user)
    require_adult(user)
    if purpose not in PURPOSE_KIND:
        raise AppError(400, "invalid_purpose", "طلب غير صالح.")
    kind = PURPOSE_KIND[purpose]
    cap = int(settings.UPLOAD_IMAGE_MAX_MB * MB)
    if length is None:
        raise AppError(411, "length_required", "طلب غير صالح.")
    if length <= 0:
        raise AppError(400, "empty_file", "الملف فارغ.")
    if length > cap:
        raise AppError(413, "file_too_large", f"الملف أكبر من الحد المسموح ({cap // MB} MB).")
    if isinstance(caption, str):
        caption_check(settings, caption)
    conv = None
    if purpose == "idea":
        quota = idea_quota(db, settings, user)
        if not quota["enabled"]:
            raise AppError(403, "idea_images_off", "نشر الصور مع الأفكار متوقف حاليًا.")
        if quota["remaining"] <= 0:
            raise AppError(429, "idea_image_limit", "يمكنك نشر صورة واحدة كل 24 ساعة.", retry_after=_retry(quota["next_at"]))
    elif purpose == "avatar":
        quota = avatar_quota(db, settings, user)
        if quota["remaining"] <= 0:
            raise _avatar_limit(quota)
    elif purpose == "chat":
        if not settings.CHAT_IMAGES_ENABLED:
            raise AppError(403, "chat_images_off", "إرسال الصور في المحادثات متوقف حاليًا.")
        conv = chat_target(db, user, conversation_id)
        if _chat_sent_last_hour(db, user) >= settings.CHAT_IMAGE_PER_HOUR:
            raise AppError(429, "chat_image_limit", "أرسلت صورًا كثيرة هذه الساعة. حاول لاحقًا.", retry_after=600)
    _check_limits(limiter, [Limit(f"upload_hour:{user.id}", settings.UPLOADS_PER_HOUR, HOUR)])
    item = MediaItem(owner_id=user.id, owner_public_id=user.public_id, purpose=purpose, kind=kind, state="uploaded",
                     conversation_id=conv.id if conv else None, source_size=length, created_at=clock.utcnow())
    db.add(item)
    db.flush()
    job = {"id": item.id, "kind": kind, "purpose": purpose, "types": sorted(settings.image_types),
           "min_side": settings.UPLOAD_IMAGE_MIN_SIDE, "max_side": settings.UPLOAD_IMAGE_MAX_SIDE,
           "max_pixels": settings.IMAGE_MAX_PIXELS, "nsfw": settings.SERVER_NSFW_CHECK,
           "nsfw_block": settings.NSFW_BLOCK_THRESHOLD, "nsfw_sexy": settings.NSFW_SEXY_THRESHOLD,
           "blur": purpose == "chat"}
    return item, job


def _retry(next_at: str | None) -> int:
    from app.services.messaging import parse_iso

    when = parse_iso(next_at) if next_at else None
    return max(60, int((when - clock.utcnow()).total_seconds())) if when else 3600


def status_of(db: Session, user: User, media_id: object) -> dict:
    item = _own(db, user, media_id)
    return {"id": item.id, "state": item.state, "error": item.error if item.state in ("rejected", "failed") else None,
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
        raise AppError(400, "media_rejected", item.error or "الملف مرفوض.")
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
# ephemeral chat pictures
# ---------------------------------------------------------------------------


def send_chat_image(db: Session, settings: Settings, limiter, user: User, conversation_id: object, *, media_id: object,
                    client_id: object, effects: Effects) -> dict:
    from app.services.messaging import (
        _after_send,
        _check_direct_request,
        _clean_client_id,
        _existing_by_client_id,
        _message_limits,
        serialize_message,
    )

    _require_can_send(user)
    if not settings.CHAT_IMAGES_ENABLED:
        raise AppError(403, "chat_images_off", "إرسال الصور في المحادثات متوقف حاليًا.")
    cid = _clean_client_id(client_id)
    existing = _existing_by_client_id(db, user, cid)
    if existing is not None:
        return {"message": serialize_message(existing, user.id)}
    conv = chat_target(db, user, conversation_id)
    item = claim(db, user, media_id, "chat")
    if item.conversation_id != conv.id:
        raise not_found()
    _check_direct_request(settings, conv, user.id)
    if conv.last_sender_id == user.id and conv.consecutive_count >= settings.MAX_CONSECUTIVE_MESSAGES:
        raise AppError(429, "wait_for_reply", "أرسلت عدة رسائل متتالية. انتظر رد الطرف الآخر.", retry_after=60)
    if _chat_sent_last_hour(db, user) >= settings.CHAT_IMAGE_PER_HOUR:
        raise AppError(429, "chat_image_limit", "أرسلت صورًا كثيرة هذه الساعة. حاول لاحقًا.", retry_after=600)
    _check_limits(limiter, _message_limits(settings, user.id))
    now = clock.utcnow()
    peer_id = conv.peer_of(user.id)
    msg = Message(conversation_id=conv.id, sender_id=user.id, recipient_id=peer_id, content="", client_id=cid,
                  kind="image", media_id=item.id, created_at=now,
                  expires_at=now + timedelta(seconds=settings.MESSAGE_TTL))
    db.add(msg)
    db.flush()
    item.state, item.attached_type, item.attached_id, item.attached_at = "attached", "message", msg.id, now
    item.expires_at = now + timedelta(seconds=settings.CHAT_IMAGE_UNOPENED_TTL)
    _after_send(db, settings, conv, user, peer_id, now, effects)
    return {"message": serialize_message(msg, user.id)}


def _chat_state(item: MediaItem | None, now) -> str:
    if item is None or item.state != "attached":
        return "expired"
    if item.view_expires_at is not None:
        return "open" if now < item.view_expires_at else "expired"
    if item.expires_at is not None and now >= item.expires_at:
        return "expired"
    return "sealed"


def message_media(db: Session, m: Message, viewer_id: str) -> dict:
    item = db.get(MediaItem, m.media_id) if m.media_id else None
    now = clock.utcnow()
    state = _chat_state(item, now)
    out = {"kind": "image", "state": state, "blur": item.blur if item is not None and state != "expired" else None}
    if state == "open":
        out["seconds_left"] = max(0, int((item.view_expires_at - now).total_seconds()))
        out["view_expires_at"] = iso(item.view_expires_at)
    return out


def open_chat_image(db: Session, settings: Settings, user: User, message_id: object, effects: Effects) -> dict:
    """The recipient taps the blurred picture: the countdown starts (once) and a short-lived URL is issued."""
    from app.services.messaging import _get_visible_conversation

    if not isinstance(message_id, str) or len(message_id) > 32:
        raise not_found()
    msg = db.get(Message, message_id)
    if msg is None or msg.kind != "image" or msg.recipient_id != user.id:
        raise not_found()
    conv = _get_visible_conversation(db, user, msg.conversation_id)
    if conv.status != "active" or _blocked_between(db, user.id, msg.sender_id):
        raise AppError(403, "conversation_closed", "هذه المحادثة لم تعد متاحة.")
    item = db.get(MediaItem, msg.media_id) if msg.media_id else None
    now = clock.utcnow()
    state = _chat_state(item, now)
    if state == "expired" or item is None or item.hidden or not item.tg_file_id:
        raise AppError(410, "media_expired", "انتهت صلاحية الصورة.")
    if item.viewed_at is None:
        item.viewed_at = now
        item.view_expires_at = now + timedelta(seconds=settings.CHAT_IMAGE_TTL_AFTER_VIEW)
        msg.read_at = msg.read_at or now
        effects.signal([msg.sender_id, user.id], "media")
    key = MEDIA_KEY.get()
    return {"url": media_urls.media_url(settings, item.id, "img", key) if key else None,
            "seconds_left": max(0, int((item.view_expires_at - now).total_seconds())),
            "view_expires_at": iso(item.view_expires_at), "secure": bool(settings.CHAT_IMAGE_FLAG_SECURE)}


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
    if item.attached_type == "message":
        msg = db.get(Message, item.attached_id)
        if msg is None or msg.recipient_id != user.id or item.hidden:
            return None
        if _chat_state(item, clock.utcnow()) != "open":
            return None
        return "no-store"
    return None


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
    """Expire chat pictures, drop uploads never used, purge old evidence."""
    now = clock.utcnow()
    done = {"expired": 0, "orphans": 0, "evidence": 0}
    expiring = db.execute(select(MediaItem).where(
        MediaItem.purpose == "chat", MediaItem.state == "attached",
        ((MediaItem.view_expires_at.is_not(None)) & (MediaItem.view_expires_at <= now))
        | ((MediaItem.view_expires_at.is_(None)) & (MediaItem.expires_at <= now))).limit(200)).scalars().all()
    for item in expiring:
        if settings.CHAT_IMAGE_ARCHIVE:
            item.legal_hold = True  # archive mode (stated in the privacy policy): kept in the storage channel
        discard(db, item, "expiry", "expired", effects, purge_storage=settings.CHAT_IMAGE_REPORT_GRACE <= 0)
        msg = db.get(Message, item.attached_id) if item.attached_id else None
        if msg is not None:
            effects.signal([msg.sender_id, msg.recipient_id], "media")
        done["expired"] += 1
    grace = now - timedelta(seconds=settings.CHAT_IMAGE_REPORT_GRACE)
    from app.services.media_moderation import delete_from_storage

    for item_id in db.execute(select(MediaItem.id).where(
            MediaItem.purpose == "chat", MediaItem.state == "expired", MediaItem.legal_hold.is_not(True),
            MediaItem.tg_message_id.is_not(None), MediaItem.removed_at <= grace).limit(200)).scalars().all():
        effects.later(delete_from_storage, item_id)  # report window over: the storage copy goes too
        done["purged"] = done.get("purged", 0) + 1
    stale = now - timedelta(hours=2)
    for item in db.execute(select(MediaItem).where(
            MediaItem.state.in_(("ready", "uploaded", "processing")), MediaItem.created_at < stale).limit(200)).scalars().all():
        if item.state == "ready":  # uploaded but never posted
            discard(db, item, "unused", "removed", effects)
        else:  # stuck (worker restarted)
            item.state, item.error = "failed", "انتهت مهلة المعالجة."
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
