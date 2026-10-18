"""V6 admin: membership requests and refunds (phase 5); rewards, withdrawals (5b) and the red envelope (5c) join
here. Mounted under ADMIN_PATH like the other admin routers (session, 2FA, roles); every action is audited by the
services."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field

from app.api.admin import SUPER_ADMIN, AdminContext, _record
from app.api.deps import get_state
from app.api.schemas import _Body
from app.errors import not_found
from app.models import MembershipRefund, MembershipRequest, User
from app.services import admin_auth, giveaway, ledger, membership, rewards, withdrawals
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


@router.post("/membership/requests/{request_id}/check")
def membership_check(request_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """V6 phase 13: check the request's TXIDs on the chain again now (information only — nothing is decided)."""
    from app.services import payment_check

    st = get_state(request)
    with st.database.session() as db:
        membership.by_id(db, MembershipRequest, request_id)
    payment_check.run(st, request_id, force=True)
    with st.database.session() as db:
        _record(db, ac, "membership_check", target_type="membership", target_id=request_id)
        return membership.admin_view(db, membership.by_id(db, MembershipRequest, request_id))


@router.post("/membership/requests/{request_id}/topup")
def membership_topup(request_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """«طلب استكمال الفارق»: the user gets the exact missing amount (in-app + e-mail, in their language)."""
    from app.services import payment_check

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        req = membership.by_id(db, MembershipRequest, request_id)
        payment_check.request_topup(db, st.settings, req, f"admin:{ac.actor}", effects)
        out = membership.admin_view(db, req)
    st.dispatch(effects)
    payment_check.update_owner(st, request_id)
    return out


@router.post("/membership/requests/{request_id}/ban")
def membership_ban(request_id: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """The owner's «حظر»: the account is banned and the request rejected."""
    from app.services import payment_check

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        req = membership.by_id(db, MembershipRequest, request_id)
        payment_check.ban(db, st.settings, req, f"admin:{ac.actor}", effects, body.note or "")
        out = membership.admin_view(db, req)
    st.dispatch(effects)
    return out


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


class OptionalReasonBody(_Body):
    reason: str | None = Field(default=None, max_length=500)


@router.post("/membership/requests/{request_id}/revoke")
def membership_revoke(request_id: str, body: OptionalReasonBody, request: Request,
                      ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """V6 phase 16: «سحب العضوية» of an automatic grant (the services audit it)."""
    from app.services import membership_auto

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        req = membership.by_id(db, MembershipRequest, request_id)
        result = membership_auto.revoke(db, st.settings, req, f"admin:{ac.actor}", effects, reason=body.reason or "")
    st.dispatch(effects)
    return result


class SuspiciousBody(_Body):
    on: bool
    reason: str | None = Field(default=None, max_length=255)


@router.post("/users/{user_ref}/suspicious")
def user_suspicious(user_ref: str, body: SuspiciousBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """V6 phase 16: «تعليم كمشبوه» — no automatic membership grant for this account (audited)."""
    from app.services import suspicion

    with get_state(request).database.session() as db:
        return suspicion.mark_user(db, user_ref, body.on, body.reason or "", f"admin:{ac.actor}")


@router.post("/devices/{device_id}/suspicious")
def device_suspicious(device_id: str, body: SuspiciousBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    from app.services import suspicion

    with get_state(request).database.session() as db:
        return suspicion.mark_device(db, device_id, body.on, body.reason or "", f"admin:{ac.actor}")


class ImagesBody(_Body):
    on: bool


class ImagesBulkBody(_Body):
    ids: list[str] = Field(min_length=1, max_length=200)
    on: bool


@router.post("/users/{user_ref}/images")
def user_images(user_ref: str, body: ImagesBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """V6 phase 17: «نشر الصور» — the per-account permission to post pictures (never given by the membership)."""
    from app.services import image_permission

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None:
            raise not_found()
        image_permission.set_allowed(db, [user.id], body.on, f"admin:{ac.actor}", effects)
        result = image_permission.view(db, user)
    st.dispatch(effects)
    return result


@router.post("/images/bulk")
def images_bulk(body: ImagesBulkBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    from app.services import image_permission

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = image_permission.set_allowed(db, body.ids, body.on, f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    return result


@router.get("/images")
def images_lists(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """«من نشر صورًا سابقًا» + «لديهم الإذن الآن» (e-mails: the access is audited like the users list)."""
    from app.services import image_permission

    with get_state(request).database.session() as db:
        result = image_permission.lists(db)
        _record(db, ac, "view_image_permissions", detail=f"former={len(result['former'])} holders={len(result['holders'])}")
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



# ----------------------------------------------------------------- V6 phase 5c: red envelope


class RoundBody(_Body):
    title: str = Field(max_length=120)
    description: str | None = Field(default=None, max_length=1000)
    winners_count: int
    ends_at: str = Field(max_length=40)
    show_winners: bool = False


class CodeBody(_Body):
    code: str | None = Field(default=None, max_length=12)


class PrizeCodesBody(_Body):
    codes: list[str] = Field(max_length=1000)


@router.get("/giveaway")
def giveaway_list(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return giveaway.admin_list(db)


@router.post("/giveaway")
def giveaway_create(body: RoundBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return giveaway.create_round(db, body.model_dump(), f"admin:{ac.actor}")


@router.get("/giveaway/{round_id}")
def giveaway_detail(round_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return giveaway.admin_view(db, giveaway._round(db, round_id), st.settings, details=True)


@router.post("/giveaway/{round_id}/draw")
def giveaway_draw(round_id: str, body: CodeBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """Random draw (fresh authenticator code required); logged with a fingerprint of the entrants."""
    st = get_state(request)
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
        giveaway.draw(db, giveaway._round(db, round_id), f"admin:{ac.actor}")
        return giveaway.admin_view(db, giveaway._round(db, round_id), st.settings, details=True)


@router.post("/giveaway/{round_id}/codes")
def giveaway_codes(round_id: str, body: PrizeCodesBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        giveaway.set_codes(db, st.settings, giveaway._round(db, round_id), body.codes, f"admin:{ac.actor}")
        return giveaway.admin_view(db, giveaway._round(db, round_id), st.settings, details=True)


@router.post("/giveaway/{round_id}/send")
def giveaway_send(round_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = giveaway.send(db, st.settings, giveaway._round(db, round_id), f"admin:{ac.actor}", effects)
    st.dispatch(effects)
    return result


@router.post("/giveaway/{round_id}/cancel")
def giveaway_cancel(round_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return giveaway.cancel(db, giveaway._round(db, round_id), f"admin:{ac.actor}")
