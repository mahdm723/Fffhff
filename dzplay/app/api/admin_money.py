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
from app.services import admin_auth, ledger, membership, rewards, withdrawals
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


# ----------------------------------------------------------------- V6 phase 5b: rewards + withdrawals


class GroupBody(_Body):
    amount: float
    active_days: int = 0
    min_posts: int = 0
    min_age_days: int = 0
    note: str | None = Field(default=None, max_length=200)
    code: str | None = Field(default=None, max_length=12)


class RewardBody(_Body):
    kind: str = Field(max_length=12)
    amount: float
    reason: str = Field(default="", max_length=300)


class ReviewBody(_Body):
    action: str = Field(max_length=10)


@router.get("/rewards")
def rewards_list(request: Request, status: str = Query(default="", max_length=10),
                 ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return rewards.admin_referrals(db, status) | withdrawals.admin_list(db)


@router.post("/referrals/{ref_id}/review")
def referral_review(ref_id: str, body: ReviewBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = rewards.review_referral(db, st.settings, ref_id, body.action, f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    return result


@router.post("/rewards/group/preview")
def group_preview(body: GroupBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return rewards.group_preview(db, body.model_dump())


@router.post("/rewards/group")
def group_run(body: GroupBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """Pays everyone matching the criteria: a fresh authenticator code is required."""
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
        result = rewards.group_run(db, body.model_dump(), f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    return result


@router.post("/users/{user_ref}/rewards")
def user_reward(user_ref: str, body: RewardBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None:
            raise not_found()
        result = rewards.individual(db, user, body.model_dump(), f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    return result


@router.get("/withdrawals")
def withdrawals_list(request: Request, status: str = Query(default="", max_length=10),
                     ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return withdrawals.admin_list(db, status)


@router.post("/withdrawals/{wid}/decide")
def withdrawal_decide(wid: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = withdrawals.decide(db, withdrawals.by_id(db, wid), body.action, f"admin:{ac.actor}", txid=body.txid,
                                    note=body.note or "", effects=effects)
    st.dispatch(effects)
    return result

