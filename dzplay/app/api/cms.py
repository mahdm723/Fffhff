"""V6 phase 8: the editable texts the app shows (membership intro, market disclaimer, welcome, announcement,
profile footer, about / FAQ / contact). Public like the policy pages; e-mail texts are never served here."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import get_state
from app.services import cms

router = APIRouter(prefix="/api", tags=["content"])


@router.get("/content/{key}")
def content(key: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return cms.public(db, st.settings, key[:64])
