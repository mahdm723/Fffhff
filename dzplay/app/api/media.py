"""Session-bound user pictures (idea pictures, chat pictures). Files stay in Telegram; this route serves the
prepared copy from the disk cache to allowed viewers only."""

from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.api.deps import current_user, get_state, session_token
from app.errors import AppError, not_found
from app.models import MediaItem
from app.services import media_items, media_urls
from app.services.media import ASSET_ID, CONTENT_TYPES, VARIANTS, MediaError
from app.services.telegram import TelegramError

router = APIRouter(tags=["media"])
_SIG = re.compile(r"^[a-f0-9]{40}$")


@router.get("/media/{asset_id}/{variant}", include_in_schema=False)
async def media(asset_id: str, variant: str, request: Request, e: int = Query(default=0), s: str = Query(default="", max_length=64)):
    """Prepared media for a signed-in viewer. The URL is signed, expires, and is bound to the
    viewer's session. Rights are checked per purpose (media_items.can_view). Supports HTTP Range."""
    st = get_state(request)
    if not ASSET_ID.match(asset_id) or variant not in CONTENT_TYPES or not _SIG.match(s or ""):
        raise not_found()
    token = session_token(request)
    if not token or not media_urls.verify_media_sig(st.settings, asset_id, variant, e, s, media_urls.session_key(token)):
        raise AppError(403, "forbidden", "رابط غير صالح أو منتهي.")

    def locate():
        with st.database.session() as db:
            user = current_user(request, db)  # the session must still be valid (not logged out / banned)
            asset = db.get(MediaItem, asset_id)
            cache = media_items.can_view(db, user, asset) if asset is not None else None
            if cache is None or variant not in VARIANTS.get(asset.kind, ()):
                raise not_found()
            try:
                return st.media.ensure(db, asset, variant), cache
            except (MediaError, TelegramError):
                raise AppError(503, "media_unavailable", "تعذّر تحميل الملف الآن. حاول بعد قليل.") from None

    path, cache = await run_in_threadpool(locate)
    return FileResponse(path, media_type=CONTENT_TYPES[variant],
                        headers={"Cache-Control": cache, "X-Content-Type-Options": "nosniff"})
