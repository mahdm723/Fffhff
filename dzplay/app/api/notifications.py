"""V6 phase 4: my notifications (owner only)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from app.api.deps import current_user, get_state
from app.services import notify

router = APIRouter(prefix="/api", tags=["notifications"])


@router.get("/notifications")
def notifications(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return notify.listing(db, current_user(request, db))


@router.post("/notifications/read")
def read_all(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return notify.mark_read(db, current_user(request, db))
