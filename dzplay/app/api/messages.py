from __future__ import annotations

import re

from fastapi import APIRouter, Query, Request
from pydantic import Field
from sqlalchemy import delete, select

from app import clock
from app.api.deps import current_user, get_state
from app.api.schemas import PushSubscribeBody, PushUnsubscribeBody, ReportBody, SendBody, _Body
from app.errors import AppError
from app.models import FcmToken, PushSubscription
from app.services import ideas, messaging
from app.services.messaging import Effects
from app.services.push import push_endpoint_allowed

router = APIRouter(prefix="/api", tags=["messaging"])


@router.get("/conversations")
def list_conversations(request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = {"conversations": messaging.list_conversations(db, user, effects, st.settings)}
    st.dispatch(effects)
    return result


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: str, request: Request, before: str | None = Query(default=None, max_length=40),
                     limit: int = Query(default=50, ge=1, le=100)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = messaging.get_conversation(db, user, conversation_id, before=before, limit=limit, effects=effects,
                                            settings=st.settings)
    st.dispatch(effects)
    return result


@router.post("/conversations/{conversation_id}/messages", status_code=201)
def reply(conversation_id: str, body: SendBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = messaging.reply(db, st.settings, st.limiter, user, conversation_id, content=body.content,
                                 client_id=body.client_id, effects=effects)
    st.dispatch(effects)
    return result


@router.post("/conversations/{conversation_id}/read")
def mark_read(conversation_id: str, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = messaging.mark_read(db, st.settings, user, conversation_id, effects)
    st.dispatch(effects)
    return result


@router.delete("/conversations/{conversation_id}")
def hide_conversation(conversation_id: str, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        messaging.hide_conversation(db, user, conversation_id, effects)
    st.dispatch(effects)
    return {"ok": True}


@router.post("/conversations/{conversation_id}/block")
def block(conversation_id: str, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        messaging.block_conversation(db, user, conversation_id, effects, st.settings)
    st.dispatch(effects)
    return {"ok": True}


@router.post("/conversations/{conversation_id}/report", status_code=201)
def report_conversation(conversation_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        return messaging.report(db, st.settings, st.limiter, user, conversation_id=conversation_id, message_id=None,
                                reason=body.reason, details=body.details)


@router.post("/messages/{message_id}/report", status_code=201)
def report_message(message_id: str, body: ReportBody, request: Request) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = messaging.report(db, st.settings, st.limiter, user, conversation_id=None, message_id=message_id,
                                  reason=body.reason, details=body.details, effects=effects)
    st.dispatch(effects)
    return result


@router.get("/sync")
def sync(request: Request, since: str | None = Query(default=None, max_length=40)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = current_user(request, db)
        result = messaging.sync(db, user, since=since, effects=effects, settings=st.settings)
    st.dispatch(effects)
    return result


@router.get("/blocks")
def list_blocks(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"blocks": messaging.list_blocks(db, current_user(request, db))}


@router.delete("/blocks/{block_id}")
def unblock(block_id: str, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        messaging.unblock(db, current_user(request, db), block_id)
    return {"ok": True}


@router.get("/profile")
def get_profile(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return ideas.own_profile(db, current_user(request, db), st.settings)


@router.post("/push/subscribe")
def push_subscribe(body: PushSubscribeBody, request: Request) -> dict:
    st = get_state(request)
    if not st.settings.push_enabled:
        raise AppError(404, "push_disabled", "الإشعارات غير مفعّلة.")
    if not push_endpoint_allowed(st.settings, body.endpoint):
        raise AppError(400, "invalid_subscription", "اشتراك غير صالح.")
    with st.database.session() as db:
        user = current_user(request, db)
        sub = db.query(PushSubscription).filter(PushSubscription.endpoint == body.endpoint).one_or_none()
        if sub is None:
            db.add(PushSubscription(user_id=user.id, endpoint=body.endpoint, p256dh=body.keys.p256dh, auth=body.keys.auth))
        else:  # same browser, maybe a different account now
            sub.user_id, sub.p256dh, sub.auth = user.id, body.keys.p256dh, body.keys.auth
    return {"ok": True}


@router.post("/push/unsubscribe")
def push_unsubscribe(body: PushUnsubscribeBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        db.query(PushSubscription).filter(PushSubscription.endpoint == body.endpoint,
                                          PushSubscription.user_id == user.id).delete()
    return {"ok": True}


# ----------------------------------------------------------------- Android app (Firebase Cloud Messaging)

_FCM_TOKEN_RE = re.compile(r"^[A-Za-z0-9_:\-]{20,512}$")
_FCM_PER_USER = 10


class FcmBody(_Body):
    token: str = Field(max_length=512)


@router.post("/push/fcm")
def fcm_register(body: FcmBody, request: Request) -> dict:
    """The Android app registers its Firebase token so a call can ring it while it is closed."""
    st = get_state(request)
    if not _FCM_TOKEN_RE.match(body.token):
        raise AppError(400, "invalid_token", "رمز غير صالح.")
    with st.database.session() as db:
        user = current_user(request, db)
        row = db.scalar(select(FcmToken).where(FcmToken.token == body.token))
        if row is None:
            db.add(FcmToken(user_id=user.id, token=body.token, created_at=clock.utcnow()))
        else:  # same phone, maybe another account now
            row.user_id, row.created_at = user.id, clock.utcnow()
        db.flush()
        ids = list(db.execute(select(FcmToken.id).where(FcmToken.user_id == user.id)
                              .order_by(FcmToken.created_at.desc()).offset(_FCM_PER_USER)).scalars())
        if ids:
            db.execute(delete(FcmToken).where(FcmToken.id.in_(ids)))
    return {"ok": True, "enabled": st.fcm is not None}


@router.post("/push/fcm/remove")
def fcm_remove(body: FcmBody, request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        db.execute(delete(FcmToken).where(FcmToken.token == body.token, FcmToken.user_id == user.id))
    return {"ok": True}
