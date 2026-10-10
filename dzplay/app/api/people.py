"""V4: my name / gender / privacy, people search, direct messages and message requests,
"reveal my identity" and mute in a conversation. Every rule is enforced here, server-side."""

from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import Field

from app import clock
from app.api.deps import current_user, get_state
from app.api.schemas import ReportBody, SendBody, _Body
from app.services import ideas, media_items, messaging, names, people
from app.services.messaging import Effects

router = APIRouter(prefix="/api", tags=["people"])


class ProfileBody(_Body):
    display_name: str | None = Field(default=None, max_length=200)  # "" or "dzplay" = back to the default
    gender: str | None = Field(default=None, max_length=12)


class PrivacyBody(_Body):
    accept_direct: str | None = Field(default=None, max_length=12)
    searchable_by_name: bool | None = None


class ConfirmAgeBody(_Body):
    confirm: bool


class RequestBody(_Body):
    action: str = Field(max_length=8)  # accept | ignore


class MuteBody(_Body):
    muted: bool


def _me(st, db, request: Request):
    user = current_user(request, db)
    return user


@router.patch("/me/profile")
def update_profile(body: ProfileBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        if body.display_name is not None:
            names.change_name(db, st.settings, user, body.display_name)
        if body.gender is not None:
            names.set_gender(user, body.gender)
        db.flush()
        return ideas.own_profile(db, user, st.settings)


@router.post("/me/gender-later")
def gender_later(request: Request) -> dict:
    """Existing accounts: "later" on the one-time gender prompt (it is not shown again)."""
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        user.gender_asked_at = user.gender_asked_at or clock.utcnow()
        return {"ok": True}


@router.patch("/me/privacy")
def update_privacy(body: PrivacyBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        return messaging.set_privacy(user, body.model_dump())


@router.post("/me/confirm-age")
def confirm_age(body: ConfirmAgeBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        if body.confirm is not True:
            from app.errors import AppError

            raise AppError(400, "age_required", "يجب التأكيد للمتابعة.")
        user.age_confirmed_at = user.age_confirmed_at or clock.utcnow()
        return {"age_confirmed": True}


class OnboardingBody(_Body):
    gender: str = Field(max_length=12)
    age_confirmed: bool
    display_name: str | None = Field(default=None, max_length=200)  # V6: required when the account has none


@router.post("/me/onboarding")
def complete_onboarding(body: OnboardingBody, request: Request) -> dict:
    """New Google accounts: the same two answers e-mail registration asks for."""
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        if body.age_confirmed is not True:
            from app.errors import AppError

            raise AppError(400, "age_required", f"يجب أن يكون عمرك 18 سنة أو أكثر لاستخدام {st.settings.APP_NAME}.")
        if user.display_name is None or body.display_name is not None:
            names.change_name(db, st.settings, user, body.display_name)
        names.set_gender(user, body.gender)
        now = clock.utcnow()
        user.gender_asked_at = user.gender_asked_at or now
        user.age_confirmed_at = user.age_confirmed_at or now
        user.onboarding_required = None
        db.flush()
        return ideas.own_profile(db, user, st.settings)


class AvatarBody(_Body):
    media_id: str = Field(max_length=40)


@router.put("/me/avatar")
def set_avatar(body: AvatarBody, request: Request) -> dict:
    """V6 phase 3: use a finished upload (purpose "avatar") as my profile picture."""
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = _me(st, db, request)
        result = media_items.set_avatar(db, st.settings, user, body.media_id, effects)
    st.dispatch(effects)
    return result


@router.delete("/me/avatar")
def remove_avatar(request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = media_items.remove_avatar(db, _me(st, db, request), effects)
    st.dispatch(effects)
    return result


@router.get("/people")
def list_people(request: Request, q: str | None = Query(default=None, max_length=60),
                cursor: str | None = Query(default=None, max_length=120)) -> dict:
    """V6 phase 3: «المستخدمون» — everyone listed, most recently active first (or a search with q)."""
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        return people.listing(db, st.settings, st.limiter, user, q, cursor)


@router.post("/people/{public_id}/report-avatar", status_code=201)
def report_avatar(public_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = _me(st, db, request)
        result = people.report_avatar(db, st.settings, st.limiter, user, public_id[:16], body.reason, body.details, effects)
    st.dispatch(effects)
    return result


@router.get("/people/search")
def search_people(request: Request, q: str = Query(max_length=60), page: int = Query(default=0, ge=0, le=50)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        return people.search(db, st.settings, st.limiter, user, q, page)


@router.get("/people/{public_id}")
def person(public_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        people.check_lookup(st.settings, st.limiter, user)  # no fast walking of the DZ-ID space
        return messaging.person_card(db, user, public_id[:16])


@router.post("/people/{public_id}/messages", status_code=201)
def message_person(public_id: str, body: SendBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = _me(st, db, request)
        if messaging.existing_direct_by_public_id(db, user, public_id[:16]) is None:
            people.check_lookup(st.settings, st.limiter, user)  # a first message by DZ-ID is a lookup too
        result = messaging.send_direct(db, st.settings, st.limiter, user, public_id[:16], content=body.content,
                                       client_id=body.client_id, effects=effects)
    st.dispatch(effects)
    return result


@router.post("/conversations/{conversation_id}/request")
def answer_request(conversation_id: str, body: RequestBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = _me(st, db, request)
        result = messaging.answer_request(db, user, conversation_id, body.action, effects)
    st.dispatch(effects)
    return result


@router.post("/conversations/{conversation_id}/mute")
def mute(conversation_id: str, body: MuteBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = _me(st, db, request)
        return messaging.set_muted(db, user, conversation_id, body.muted)
