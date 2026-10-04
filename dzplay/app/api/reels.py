"""Reels API (feed, reactions, views, PUBLIC comments) and session-bound media files.

Reel comments use their own table and endpoints; nothing here touches the
owner-only Ideas comments (/api/posts/{id}/comments).
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import current_user, get_state, session_token
from app.api.posts import ReactionBody
from app.api.schemas import ReportBody, SendBody
from app.errors import AppError, not_found
from app.models import MediaItem, Reel, ReelAsset
from app.services import media_items, reels
from app.services.media import ASSET_ID, CONTENT_TYPES, VARIANTS, MediaError
from app.services.telegram import TelegramError

router = APIRouter(tags=["reels"])
_SIG = re.compile(r"^[a-f0-9]{40}$")


@router.get("/api/reels/feed")
def feed(request: Request, cursor: str | None = Query(default=None, max_length=300),
         limit: int | None = Query(default=None, ge=1, le=50)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return reels.feed(db, st.settings, user, reels.session_key(session_token(request)), cursor, limit)


@router.put("/api/reels/{reel_id}/reaction")
def react(reel_id: str, body: ReactionBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return reels.set_reaction(db, st.settings, st.limiter, current_user(request, db), reel_id, body.reaction)


@router.post("/api/reels/{reel_id}/view")
def view(reel_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return reels.mark_seen(db, st.settings, st.limiter, current_user(request, db), reel_id)


@router.get("/api/reels/{reel_id}/comments")
def comments(reel_id: str, request: Request, cursor: str | None = Query(default=None, max_length=300)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return reels.list_comments(db, st.settings, current_user(request, db), reel_id, cursor)


@router.post("/api/reels/{reel_id}/comments", status_code=201)
def add_comment(reel_id: str, body: SendBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return reels.add_comment(db, st.settings, st.limiter, current_user(request, db), reel_id, content=body.content)


@router.delete("/api/reel-comments/{comment_id}")
def delete_comment(comment_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        reels.delete_comment(db, current_user(request, db), comment_id)
    return {"ok": True}


@router.post("/api/reel-comments/{comment_id}/report", status_code=201)
def report_comment(comment_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return reels.report_comment(db, st.settings, st.limiter, current_user(request, db), comment_id,
                                    reason=body.reason, details=body.details)


@router.get("/media/{asset_id}/{variant}", include_in_schema=False)
async def media(asset_id: str, variant: str, request: Request, e: int = Query(default=0), s: str = Query(default="", max_length=64)):
    """Prepared media for a signed-in viewer. The URL is signed, expires, and is bound to the
    viewer's session (copying it elsewhere does not work). Supports HTTP Range."""
    st = get_state(request)
    if not ASSET_ID.match(asset_id) or variant not in CONTENT_TYPES or not _SIG.match(s or ""):
        raise not_found()
    token = session_token(request)
    if not token or not reels.verify_media_sig(st.settings, asset_id, variant, e, s, reels.session_key(token)):
        raise AppError(403, "forbidden", "رابط غير صالح أو منتهي.")

    def locate():
        with st.database.session() as db:
            user = current_user(request, db)  # the session must still be valid (not logged out / banned)
            asset = db.get(ReelAsset, asset_id)
            cache = "private, max-age=3600"
            if asset is not None:
                reel = db.get(Reel, asset.reel_id)
                if reel is None or reel.status != "visible":
                    raise not_found()
            else:  # V5: a user's picture/video: allowed viewers only, and only while it may be seen
                asset = db.get(MediaItem, asset_id)
                cache = media_items.can_view(db, user, asset) if asset is not None else None
                if cache is None:
                    raise not_found()
            if variant not in VARIANTS.get(asset.kind, ()):
                raise not_found()
            try:
                return st.media.ensure(db, asset, variant), cache
            except (MediaError, TelegramError):
                raise AppError(503, "media_unavailable", "تعذّر تحميل الملف الآن. حاول بعد قليل.") from None

    path, cache = await run_in_threadpool(locate)
    return FileResponse(path, media_type=CONTENT_TYPES[variant],
                        headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"})
