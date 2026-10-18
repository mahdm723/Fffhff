"""V5 uploads: idea pictures and profile pictures (V6 phase 17: pictures in chats are switched off for good —
purpose=chat is refused with 410 by the security middleware before the request reaches this router).

POST /api/uploads?purpose=idea|avatar   body = the raw file (Content-Length required)
  Checks run BEFORE any byte is read (session, 18+, permission, quotas, size); the body is then streamed
  into the size-capped temp area with a hard cap, and the media worker takes over. The answer is an
  upload id the phone polls (or hears about on the WebSocket) until it is ready / refused.
"""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from app.api.deps import current_user, get_state
from app.api.schemas import _Body
from app.errors import AppError
from app.models import MediaItem
from app.services import media_items
from app.services.messaging import Effects
from app.i18n import N_

router = APIRouter(prefix="/api", tags=["uploads"])


class PrecheckBody(_Body):
    purpose: str = Field(max_length=8)
    conversation_id: str | None = Field(default=None, max_length=40)
    caption: str | None = Field(default=None, max_length=5000)
    size: int | None = None


@router.get("/uploads/config")
def config(request: Request) -> dict:
    """Everything the phone checks before uploading (types, sizes, NSFW thresholds) + the user's quotas."""
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return media_items.upload_config(db, st.settings, user, st.pipeline.unavailable() is None)


@router.post("/uploads/precheck")
def precheck(body: PrecheckBody, request: Request) -> dict:
    """Same rules as the upload itself, without the file: the phone asks before sending megabytes."""
    st = get_state(request)
    reason = st.pipeline.unavailable()
    if reason:
        raise AppError(503, "uploads_unavailable", reason)
    with st.database.session() as db:
        user = current_user(request, db)
        if body.caption:
            media_items.caption_check(st.settings, body.caption)
        if body.purpose == "idea":
            quota = media_items.idea_quota(db, st.settings, user)
            if not quota["enabled"]:
                raise AppError(403, "idea_images_off", "نشر الصور مع الأفكار متوقف حاليًا.")
            if not quota["allowed"]:
                raise media_items.images_not_allowed()
            if quota["remaining"] <= 0:
                raise AppError(429, "idea_image_limit", "يمكنك نشر صورة واحدة كل 24 ساعة.")
        elif body.purpose == "chat":
            raise media_items.chat_images_disabled()
        elif body.purpose == "avatar":
            quota = media_items.avatar_quota(db, st.settings, user)
            if quota["remaining"] <= 0:
                raise media_items._avatar_limit(quota)
        elif body.purpose not in media_items.PURPOSE_KIND:
            raise AppError(400, "invalid_purpose", "طلب غير صالح.")
        db.rollback()
    return {"ok": True}


@router.post("/uploads", status_code=202)
async def upload(request: Request, purpose: str = Query(max_length=8)) -> dict:
    st = get_state(request)
    reason = st.pipeline.unavailable()
    if reason:
        raise AppError(503, "uploads_unavailable", reason)
    raw_length = request.headers.get("content-length") or ""
    length = int(raw_length) if raw_length.isdigit() and len(raw_length) < 12 else None
    if length is not None and st.pipeline.busy(length):
        raise AppError(503, "uploads_busy", "الخادم مشغول بمعالجة ملفات أخرى. حاول بعد دقيقة.", retry_after=60)

    def start() -> tuple[str, dict, int]:
        with st.database.session() as db:
            user = current_user(request, db)
            item, job = media_items.begin_upload(db, st.settings, st.limiter, user, purpose=purpose,
                                                 length=length)
            return item.id, job, int(length or 0)

    item_id, job, cap = await run_in_threadpool(start)
    path = st.pipeline.input_path(item_id)  # name from our random id: no user input reaches the filesystem
    written = 0
    try:
        with open(path, "wb") as fh:
            async for chunk in request.stream():
                written += len(chunk)
                if written > cap:
                    raise AppError(413, "file_too_large", "الملف أكبر من الحجم المعلن.")
                fh.write(chunk)
        if written != cap:
            raise AppError(400, "incomplete_upload", "لم يكتمل رفع الملف. أعد المحاولة.")
    except (AppError, ClientDisconnect, OSError) as exc:
        path.unlink(missing_ok=True)

        def failed() -> None:
            with st.database.session() as db:
                item = db.get(MediaItem, item_id)
                if item is not None:
                    item.state, item.error = "failed", N_("لم يكتمل الرفع.")
        await run_in_threadpool(failed)
        if isinstance(exc, AppError):
            raise
        raise AppError(400, "incomplete_upload", "لم يكتمل رفع الملف. أعد المحاولة.") from None
    st.pipeline.submit(item_id, job)
    return {"upload": {"id": item_id, "state": "processing"}}


@router.get("/uploads/{media_id}")
def upload_status(media_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"upload": media_items.status_of(db, current_user(request, db), media_id)}


@router.post("/messages/{message_id}/open")
def open_chat_image(message_id: str, request: Request) -> dict:
    """An old chat picture (V6 phase 17: always 410 — the pictures are gone)."""
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = media_items.open_chat_image(db, st.settings, user, message_id, effects)
    st.dispatch(effects)
    return result
