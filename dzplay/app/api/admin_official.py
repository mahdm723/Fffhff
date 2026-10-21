"""V6 phase 19 admin: «الحساب الرسمي» — post (text + optional picture) as the official account with the admin's 2FA
code, its posts with «تثبيت» / «إلغاء التثبيت» / delete. Mounted under ADMIN_PATH (session, 2FA, roles); every
action is audited by services/official.py."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from app.api.admin import SUPER_ADMIN, AdminContext
from app.api.deps import get_state
from app.api.schemas import _Body
from app.errors import AppError
from app.services import admin_auth, official
from app.services.messaging import Effects

router = APIRouter(prefix="/api/admin/official", tags=["admin-official"])


class OfficialPostBody(_Body):
    content: str = Field(default="", max_length=10000)
    media_id: str | None = Field(default=None, max_length=40)
    code: str = Field(max_length=12)  # the admin's authenticator code (step-up)


class PinBody(_Body):
    hours: int = Field(ge=0, le=official.MAX_PIN_HOURS)


@router.get("")
def official_home(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return official.panel_view(db)


@router.post("/uploads", status_code=202)
async def official_upload(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """The picture (raw body, size-capped before reading); then the same checks as every picture."""
    st = get_state(request)
    raw = request.headers.get("content-length") or ""
    cap = int(st.settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024)
    if not raw.isdigit() or not 0 < int(raw) <= cap:
        raise AppError(413, "file_too_large", "الملف أكبر من الحد المسموح ({mb} MB).", params={"mb": cap // (1024 * 1024)})
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > cap:
            raise AppError(413, "file_too_large", "الملف أكبر من الحد المسموح ({mb} MB).", params={"mb": cap // (1024 * 1024)})
    item_id = await run_in_threadpool(official.start_picture, st, bytes(data))
    return {"upload": {"id": item_id, "state": "processing"}}


@router.get("/uploads/{item_id}")
def official_upload_state(item_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"upload": official.picture_state(db, item_id)}


@router.post("/posts", status_code=201)
def official_post(body: OfficialPostBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
        result = official.create_post(db, st.settings, content=body.content, media_id=body.media_id,
                                      actor=f"admin:{ac.actor}", effects=effects)
    st.dispatch(effects)
    return result


@router.post("/posts/{post_id}/pin")
def official_pin(post_id: str, body: PinBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        out = official.pin(db, post_id, body.hours, f"admin:{ac.actor}")
    official.changed_pin(st)  # V6 phase 21: the feed shows the new pin at once, in every worker
    return out


@router.delete("/posts/{post_id}")
def official_delete(post_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        official.delete(db, post_id, f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    official.changed_pin(st)
    return {"ok": True}
