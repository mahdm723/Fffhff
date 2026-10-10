"""V5 user endpoints: support tickets («تذاكري») and the blue-star request. V6 phase 7: «تواصل معنا»."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import Field

from app.api.deps import current_user, get_state
from app.api.schemas import _Body
from app.services import support, verification
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


@router.get("/contact")
def contact(request: Request) -> dict:
    """«تواصل معنا»: the official support address (set by the admin), public like the policies."""
    from app.services import mail

    st = get_state(request)
    with st.database.session() as db:
        return {"app_name": st.settings.APP_NAME, "support_email": mail.support_address(db, st.settings) or None}


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
