"""V6 phase 5: «عضويتي» — membership status, payment by TXID, refund within the window (password + e-mail code)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import Field

from app.api.deps import current_user, get_state
from app.api.schemas import _Body
from app.services import email_codes, membership
from app.services.messaging import Effects

router = APIRouter(prefix="/api", tags=["membership"])


class TxBody(_Body):
    txid: str = Field(max_length=200)


class RefundBody(_Body):
    network: str = Field(max_length=16)
    address: str = Field(max_length=128)
    password: str | None = Field(default=None, max_length=512)
    code: str = Field(max_length=12)


@router.get("/membership")
def overview(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return membership.overview(db, st.settings, current_user(request, db))


@router.post("/membership/requests", status_code=201)
def submit(body: TxBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = membership.submit(db, st.settings, st.limiter, current_user(request, db), body.txid, effects)
    st.dispatch(effects)
    return result


@router.post("/membership/refund/code")
def refund_code(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        if not membership.refund_state(db, st.settings, user)["eligible"]:
            from app.errors import AppError

            raise AppError(403, "refund_not_allowed", "لا يمكن طلب الاسترجاع الآن.")
        return email_codes.send(db, st.settings, st.limiter, user, "refund")


@router.post("/membership/refund", status_code=201)
def refund(body: RefundBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = membership.request_refund(db, st.settings, current_user(request, db), body.model_dump(), effects)
    st.dispatch(effects)
    return result
