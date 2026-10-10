"""V6 phase 5b: «أرباحي» (owner only), invitations and withdrawals. Promotional rewards, separate from membership."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse
from pydantic import Field

from app.api.deps import current_user, get_state
from app.api.schemas import _Body
from app.errors import AppError
from app.services import email_codes, ledger, rewards, withdrawals
from app.services.messaging import Effects

router = APIRouter(tags=["rewards"])


class WithdrawBody(_Body):
    network: str = Field(max_length=16)
    address: str = Field(max_length=128)
    amount: float | str
    password: str | None = Field(default=None, max_length=512)
    code: str = Field(max_length=12)


@router.get("/api/rewards")
def overview(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return rewards.overview(db, st.settings, current_user(request, db))


@router.get("/api/rewards/referrals")
def referrals(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return rewards.referrals_view(db, st.settings, current_user(request, db))


@router.post("/api/rewards/withdraw/code")
def withdraw_code(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        if not st.settings.WITHDRAW_ENABLED:
            raise AppError(403, "withdraw_off", "السحب متوقف حاليًا.")
        if ledger.balances(db, user.id, "rewards")["available"] < ledger.to_minor(st.settings.WITHDRAW_MIN):
            raise AppError(400, "below_min", f"رصيدك المتاح أقل من حد السحب ({st.settings.WITHDRAW_MIN:g} USDT).")
        return email_codes.send(db, st.settings, st.limiter, user, "withdraw")


@router.post("/api/rewards/withdraw", status_code=201)
def withdraw(body: WithdrawBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = withdrawals.request(db, st.settings, current_user(request, db), body.model_dump(), effects)
    st.dispatch(effects)
    return result


@router.get("/r/{code}", include_in_schema=False)
def invitation(code: str, request: Request) -> RedirectResponse:
    """An invitation link: remember the code for 30 days (HttpOnly cookie), then open the app."""
    st = get_state(request)
    response = RedirectResponse("/", status_code=302)
    code = code.upper()[:16]
    if rewards.CODE_RE.match(code) and st.settings.REFERRAL_ENABLED:
        response.set_cookie(rewards.REF_COOKIE, code, max_age=30 * 86400, httponly=True, samesite="lax",
                            secure=bool(st.settings.COOKIE_SECURE), path="/api/auth")
    return response
