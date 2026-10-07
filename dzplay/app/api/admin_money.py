"""V6 admin: membership requests and refunds (phase 5); rewards, withdrawals (5b) and the red envelope (5c) join
here. Mounted under ADMIN_PATH like the other admin routers (session, 2FA, roles); every action is audited by the
services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.api.admin import SUPER_ADMIN, AdminContext
from app.api.deps import get_state
from app.api.schemas import _Body
from app.errors import not_found
from app.models import MembershipRefund, MembershipRequest, User
from app.services import ledger, membership
from app.services.messaging import Effects

router = APIRouter(prefix="/api/admin", tags=["admin-money"])


class DecideBody(_Body):
    action: str = Field(max_length=10)
    note: str | None = Field(default=None, max_length=1000)
    txid: str | None = Field(default=None, max_length=200)


class ReasonBody(_Body):
    reason: str = Field(min_length=3, max_length=500)


@router.get("/membership")
def membership_list(request: Request, status: str = Query(default="", max_length=10),
                    ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return membership.admin_list(db, status)


@router.post("/membership/requests/{request_id}/decide")
def membership_decide(request_id: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        req = membership.by_id(db, MembershipRequest, request_id)
        result = membership.decide(db, st.settings, req, body.action, f"admin:{ac.actor}", body.note or "", effects)
    st.dispatch(effects)
    return result


@router.post("/membership/refunds/{refund_id}/decide")
def refund_decide(refund_id: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        r = membership.by_id(db, MembershipRefund, refund_id)
        result = membership.decide_refund(db, st.settings, r, body.action, f"admin:{ac.actor}", txid=body.txid,
                                          note=body.note or "", effects=effects)
    st.dispatch(effects)
    return result


@router.post("/users/{user_ref}/membership/end")
def membership_end(user_ref: str, body: ReasonBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None or not membership.is_member(user):
            raise not_found()
        membership.end(db, user, f"admin:{ac.actor}", effects, reason=body.reason)
    st.dispatch(effects)
    return {"ok": True}


@router.get("/users/{user_ref}/balances")
def user_ledger(user_ref: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None:
            raise not_found()
        return {acc: {"balances": {k: ledger.from_minor(v) for k, v in ledger.balances(db, user.id, acc).items()},
                      "entries": ledger.entries(db, user.id, acc)} for acc in ledger.ACCOUNTS} | {
            "member": membership.is_member(user), "member_since": user.member_since.isoformat() + "Z" if user.member_since else None}
