"""V6 phase 5c: «الظرف الأحمر» — enter a round with a confirmed e-mail (prize codes are never sent to the app)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import Field

from app.api.deps import client_context, current_user, get_state
from app.api.schemas import _Body
from app.services import giveaway

router = APIRouter(prefix="/api", tags=["giveaway"])


class EnterBody(_Body):
    email: str | None = Field(default=None, max_length=320)


class ConfirmBody(_Body):
    code: str = Field(max_length=12)


@router.get("/giveaway")
def view(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return giveaway.view(db, current_user(request, db))


@router.post("/giveaway/{round_id}/enter")
def enter(round_id: str, body: EnterBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        result = giveaway.enter(db, st.settings, st.limiter, user, round_id[:32], body.email)
        giveaway.set_network(db, user, round_id[:32], client_context(request).ip_hash)
        return result


@router.post("/giveaway/{round_id}/confirm")
def confirm(round_id: str, body: ConfirmBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return giveaway.confirm(db, st.settings, current_user(request, db), round_id[:32], body.code)
