"""V4: 1:1 calls. Start / answer / end over REST (also usable from a notification tap);
SDP / ICE signaling goes over the WebSocket (app/api/ws.py → calls.handle_signal)."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import Field

from app.api.deps import current_user, get_state
from app.api.schemas import ReportBody, _Body
from app.errors import rate_limited
from app.services import calls
from app.services.messaging import Effects
from app.services.rate_limit import Limit

router = APIRouter(prefix="/api/calls", tags=["calls"])


class StartBody(_Body):
    conversation_id: str = Field(max_length=32)
    kind: str = Field(max_length=8)


class AcceptBody(_Body):
    device: str | None = Field(default=None, max_length=40)


class HangupBody(_Body):
    reason: str | None = Field(default=None, max_length=24)


@router.get("/ice")
def ice(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        decision = st.limiter.check_and_hit([Limit(f"ice:{user.id}", 30, 60)])
        if not decision.allowed:
            raise rate_limited(decision.retry_after)
        return calls.ice_config(st.settings, user)


@router.post("", status_code=201)
def start(body: StartBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = calls.start_call(db, st.settings, user, body.conversation_id, body.kind, effects)
    st.dispatch(effects)
    return result


@router.get("/active")
def active(request: Request) -> dict:
    """The caller's / callee's live call, if any (e.g. the app was opened from the ring notification)."""
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        call = calls.active_call(db, user.id)
        return {"call": calls.call_view(db, call, user.id) if call else None}


def _act(request: Request, call_id: str, fn) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        call = calls._get_call(db, user, call_id)
        fn(db, st.settings, user, call, effects)
        db.flush()
        result = {"call": calls.call_view(db, call, user.id)}
    st.dispatch(effects)
    return result


@router.post("/{call_id}/accept")
def accept(call_id: str, body: AcceptBody, request: Request) -> dict:
    return _act(request, call_id, lambda db, s, u, c, e: calls.accept(db, s, u, c, body.device, e))


@router.post("/{call_id}/decline")
def decline(call_id: str, request: Request) -> dict:
    return _act(request, call_id, calls.decline)


@router.post("/{call_id}/hangup")
def hangup(call_id: str, body: HangupBody, request: Request) -> dict:
    return _act(request, call_id, lambda db, s, u, c, e: calls.hangup(db, s, u, c, body.reason, e))


@router.post("/{call_id}/report", status_code=201)
def report(call_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return calls.report_call(db, st.settings, st.limiter, user, call_id, reason=body.reason, details=body.details)
