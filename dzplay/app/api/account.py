"""V5 user endpoints: support tickets («تذاكري») and the blue-star request."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from app.api.deps import current_user, get_state, session_token
from app.api.schemas import ReportBody, _Body
from app.services import creator_reels, monetization, support, verification
from app.services.messaging import Effects

router = APIRouter(prefix="/api", tags=["account"])


class TicketBody(_Body):
    category: str = Field(max_length=16)
    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(max_length=20_000)


class TicketMessageBody(_Body):
    body: str = Field(max_length=20_000)


class VerifyBody(_Body):
    # Only these fields are read: anything else (verified, role, balance…) is ignored.
    account_type: str = Field(max_length=16)
    description: str = Field(max_length=5000)
    reason: str = Field(max_length=5000)
    amount: Any = None
    txid: str = Field(max_length=300)


@router.get("/support/tickets")
def my_tickets(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return support.list_mine(db, current_user(request, db))


@router.post("/support/tickets", status_code=201)
def new_ticket(body: TicketBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = support.create(db, st.settings, st.limiter, user, category=body.category, subject=body.subject,
                                body=body.body, effects=effects)
    st.dispatch(effects)
    return result


@router.get("/support/tickets/{ticket_id}")
def ticket(ticket_id: int, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return support.get_mine(db, current_user(request, db), ticket_id)


@router.post("/support/tickets/{ticket_id}/messages", status_code=201)
def ticket_message(ticket_id: int, body: TicketMessageBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = support.add_message(db, st.settings, st.limiter, user, ticket_id, body.body, effects)
    st.dispatch(effects)
    return result


@router.post("/support/tickets/{ticket_id}/close")
def close_ticket(ticket_id: int, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return support.close(db, current_user(request, db), ticket_id)


@router.get("/verification")
def verification_overview(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return verification.overview(db, st.settings, current_user(request, db))


@router.post("/verification", status_code=201)
def verification_submit(body: VerifyBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = verification.submit(db, st.settings, st.limiter, user, body.model_dump(), effects)
    st.dispatch(effects)
    return result


@router.put("/verification/{request_id}")
def verification_fix(request_id: str, body: VerifyBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = verification.resubmit(db, st.settings, user, request_id, body.model_dump(), effects)
    st.dispatch(effects)
    return result


# ----------------------------------------------------------------- studio (verified creators)


class StudioBody(_Body):
    media_id: str = Field(max_length=40)
    caption: str | None = Field(default=None, max_length=5000)
    show_author: bool = False
    show_on_profile: bool = False


class StudioUpdateBody(_Body):
    show_author: bool | None = None
    show_on_profile: bool | None = None


def _skey(request: Request) -> str:
    from app.services.reels import session_key

    return session_key(session_token(request))


@router.get("/studio")
def studio(request: Request, all: bool = False) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        creator_reels.require_creator(st.settings, user)
        return creator_reels.my_reels(db, st.settings, user, _skey(request), studio_only=not all)


@router.post("/studio/reels", status_code=201)
def studio_submit(body: StudioBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = creator_reels.submit(db, st.settings, user, media_id=body.media_id, caption=body.caption,
                                      show_author=body.show_author, show_on_profile=body.show_on_profile,
                                      effects=effects, skey=_skey(request))
    st.dispatch(effects)
    return result


@router.patch("/studio/reels/{reel_id}")
def studio_update(reel_id: str, body: StudioUpdateBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return creator_reels.update(db, st.settings, user, reel_id, body.model_dump(exclude_none=True), _skey(request))


@router.delete("/studio/reels/{reel_id}")
def studio_delete(reel_id: str, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        creator_reels.delete(db, st.media, current_user(request, db), reel_id, effects)
    st.dispatch(effects)
    return {"ok": True}


@router.post("/reels/{reel_id}/report", status_code=201)
def report_reel(reel_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = creator_reels.report(db, st.settings, st.limiter, user, reel_id, reason=body.reason,
                                      details=body.details, effects=effects)
    st.dispatch(effects)
    return result


@router.get("/profiles/{ref}/reels")
def profile_reels(ref: str, request: Request) -> dict:
    """Named creator reels the owner chose to list on their profile (never the ones without the name)."""
    from app.services.ideas import _user_by_ref

    st = get_state(request)
    with st.database.session() as db:
        current_user(request, db)
        owner = _user_by_ref(db, ref)
        return {"reels": creator_reels.profile_reels(db, st.settings, owner.id, _skey(request))}


# ----------------------------------------------------------------- monetization + «أموالي»


class EmailCodeBody(_Body):
    email: str = Field(max_length=320)


class MonetizeBody(_Body):
    content_type: str = Field(max_length=1000)
    payout_email: str | None = Field(default=None, max_length=320)
    code: str | None = Field(default=None, max_length=12)
    terms: bool = False


@router.get("/monetization")
def monetization_overview(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return monetization.overview(db, st.settings, current_user(request, db))


@router.post("/monetization/email-code")
def monetization_code(body: EmailCodeBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = monetization.send_email_code(db, st.settings, st.limiter, current_user(request, db), body.email, effects)
    st.dispatch(effects)
    return result


@router.post("/monetization", status_code=201)
def monetization_apply(body: MonetizeBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = monetization.apply(db, st.settings, current_user(request, db), body.model_dump(), effects)
    st.dispatch(effects)
    return result


@router.get("/money")
def my_money(request: Request) -> dict:
    """Read-only: no endpoint lets a user write to the ledger."""
    st = get_state(request)
    with st.database.session() as db:
        return monetization.my_money(db, st.settings, current_user(request, db))


# ----------------------------------------------------------------- «حذف حسابي»


class DeleteAccountBody(_Body):
    password: str | None = Field(default=None, max_length=512)
    confirm: str | None = Field(default=None, max_length=40)


@router.post("/me/delete")
def delete_my_account(body: DeleteAccountBody, request: Request):
    from fastapi.responses import JSONResponse

    from app.api.deps import clear_session_cookie
    from app.services import account_deletion

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        account_deletion.delete_own(db, user, password=body.password, confirm=body.confirm, effects=effects, store=st.media)
    st.dispatch(effects)
    res = JSONResponse({"ok": True})
    clear_session_cookie(res, request)
    return res
