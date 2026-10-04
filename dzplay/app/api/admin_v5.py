"""V5 admin panel API: live settings (tunables), support, verification + payment settings, media moderation.

Mounted under ADMIN_PATH like app.api.admin (same session, 2FA, roles, audit log). Telegram buttons and
these endpoints call the same services, so both stay in sync.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from pydantic import Field
from sqlalchemy import select

from app.api.admin import SUPER_ADMIN, AdminContext, _record
from app.api.deps import get_state
from app.api.schemas import _Body
from app.errors import AppError, not_found
from app.models import MediaItem, Post, User, VerificationRequest
from app.services import media_moderation, monetization, support, tunables, verification
from app.services.messaging import Effects, iso

router = APIRouter(prefix="/api/admin", tags=["admin-v5"])


class SettingsBody(_Body):
    changes: dict[str, Any]


class ReplyBody(_Body):
    body: str = Field(max_length=20_000)


class StatusBody(_Body):
    status: str = Field(max_length=16)


class DecideBody(_Body):
    action: str = Field(max_length=10)
    note: str | None = Field(default=None, max_length=1000)


class VerifiedBody(_Body):
    verified: bool
    reason: str | None = Field(default=None, max_length=255)


class PaymentBody(_Body):
    currency: str = Field(default="", max_length=20)
    network: str = Field(default="", max_length=20)
    wallet: str = Field(default="", max_length=200)
    explorer: str = Field(default="", max_length=300)
    note: str = Field(default="", max_length=500)


class MediaActionBody(_Body):
    action: str = Field(max_length=8)


# ----------------------------------------------------------------- live settings


@router.get("/settings")
def get_settings(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"settings": tunables.listing(db, st.settings), "groups": tunables.GROUPS}


@router.put("/settings")
def put_settings(body: SettingsBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        keys = tunables.save(db, st.settings, body.changes, ac.actor)
        _record(db, ac, "settings_change", detail=", ".join(f"{k}={body.changes[k]!r}" for k in keys)[:2000])
    st.hub.notify_system({"type": "tunables"})  # every instance reloads (this one included)
    st.reload_tunables()
    with st.database.session() as db:
        return {"settings": tunables.listing(db, st.settings), "groups": tunables.GROUPS}


# ----------------------------------------------------------------- support


@router.get("/support")
def support_list(request: Request, status: str = Query(default="", max_length=10), q: str = Query(default="", max_length=40),
                 ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return support.admin_list(db, status, q.strip())


@router.get("/support/{ticket_id}")
def support_get(ticket_id: int, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return support.admin_get(db, ticket_id)


@router.post("/support/{ticket_id}/reply")
def support_reply(ticket_id: int, body: ReplyBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = support.admin_reply(db, st.settings, ticket_id, body.body, ac.actor, effects)
        _record(db, ac, "support_reply", target_type="ticket", target_id=str(support.NUMBER_BASE + ticket_id))
    st.dispatch(effects)
    return result


@router.post("/support/{ticket_id}/status")
def support_status(ticket_id: int, body: StatusBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = support.admin_set_status(db, ticket_id, body.status)
        _record(db, ac, f"support_{body.status}", target_type="ticket", target_id=str(support.NUMBER_BASE + ticket_id))
        return result


# ----------------------------------------------------------------- verification + payments


@router.get("/verification")
def verification_list(request: Request, status: str = Query(default="", max_length=10),
                      ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return verification.admin_list(db, status)


@router.post("/verification/{request_id}/decide")
def verification_decide(request_id: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        req = db.get(VerificationRequest, request_id) if len(request_id) <= 32 else None
        if req is None:
            raise not_found()
        result = verification.decide(db, req, body.action, ac.actor, body.note or "", effects)
        if req.tg_message_id and st.bot is not None:
            label = {"accept": "✅ قُبل", "reject": "❌ رُفض", "fix": "✏️ طُلب تصحيح"}.get(body.action, body.action)
            effects.later(media_moderation.mark_done, st.bot.admin_id, req.tg_message_id, f"{label} — {ac.actor}")
    st.dispatch(effects)
    return result


@router.post("/users/{user_ref}/verified")
def set_verified(user_ref: str, body: VerifiedBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """Manual grant / revoke of the blue star."""
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None:
            raise not_found()
        if body.verified:
            verification.grant(db, user, ac.actor, effects)
        else:
            verification.revoke(db, user, ac.actor, body.reason or "", effects)
        result = {"user_ref": user.id, "verified": user.verified_at is not None, "verified_at": iso(user.verified_at)}
    st.dispatch(effects)
    return result


@router.get("/payment-settings")
def payment_get(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"payment": verification.payment_settings(db),
                "networks": {k: {"label": v["label"], "explorer": v["explorer"]} for k, v in verification.NETWORKS.items()}}


@router.put("/payment-settings")
def payment_put(body: PaymentBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"payment": verification.save_payment_settings(db, body.model_dump(), ac.actor)}


# ----------------------------------------------------------------- media moderation queue


def _media_row(db, item: MediaItem) -> dict:
    caption = None
    if item.attached_type == "post" and item.attached_id:
        post = db.get(Post, item.attached_id)
        caption = post.content if post else None
    return {"id": item.id, "purpose": item.purpose, "kind": item.kind, "state": item.state, "review": item.review,
            "hidden": bool(item.hidden), "legal_hold": bool(item.legal_hold), "reports": item.reports_count or 0,
            "nsfw": item.nsfw_score, "owner_public_id": item.owner_public_id, "owner_ref": item.owner_id,
            "caption": caption, "error": item.error, "created_at": iso(item.created_at),
            "removed_by": item.removed_by, "attached_type": item.attached_type, "attached_id": item.attached_id,
            "previewable": bool(item.state == "attached" and item.tg_file_id or item.legal_hold and item.tg_file_id)}


@router.get("/media")
def media_queue(request: Request, filter: str = Query(default="published", max_length=12),
                limit: int = Query(default=60, le=200), ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """published | pending | rejected | reported | removed"""
    with get_state(request).database.session() as db:
        q = select(MediaItem).order_by(MediaItem.created_at.desc())
        if filter == "pending":
            q = q.where(MediaItem.review == "pending")
        elif filter == "rejected":
            q = q.where(MediaItem.state == "rejected")
        elif filter == "reported":
            q = q.where(MediaItem.reports_count > 0)
        elif filter == "removed":
            q = q.where(MediaItem.state == "removed")
        else:
            q = q.where(MediaItem.state == "attached", MediaItem.purpose != "chat")
        return {"items": [_media_row(db, i) for i in db.execute(q.limit(limit)).scalars()]}


@router.post("/media/{item_id}/action")
def media_action(item_id: str, body: MediaActionBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    if body.action not in media_moderation.ACTIONS:
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    effects = Effects()
    with st.database.session() as db:
        item = db.get(MediaItem, item_id) if len(item_id) <= 32 else None
        if item is None:
            raise not_found()
        label = media_moderation.act_from_panel(st, db, item, body.action, f"admin:{ac.actor}", effects)
        row = _media_row(db, item)
    st.dispatch(effects)
    return {"result": label, "item": row}


# ----------------------------------------------------------------- monetization + ledger


class LedgerBody(_Body):
    kind: str = Field(max_length=12)  # earning | adjustment | payout
    amount: Any = None
    currency: str | None = Field(default=None, max_length=12)
    note: str | None = Field(default=None, max_length=500)
    paid_on: str | None = Field(default=None, max_length=10)  # payouts: date the Red Packet was sent


class ReverseBody(_Body):
    note: str | None = Field(default=None, max_length=500)


@router.get("/monetization")
def monetization_list(request: Request, status: str = Query(default="", max_length=10),
                      ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return monetization.admin_list(db, status)


@router.post("/monetization/{app_id}/decide")
def monetization_decide(app_id: str, body: DecideBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    from app.models import MonetizationApplication

    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        a = db.get(MonetizationApplication, app_id) if len(app_id) <= 32 else None
        if a is None:
            raise not_found()
        result = monetization.decide(db, a, body.action, ac.actor, body.note or "", effects)
        if a.tg_message_id and st.bot is not None:
            effects.later(media_moderation.mark_done, st.bot.admin_id, a.tg_message_id, f"{a.status} — {ac.actor}")
    st.dispatch(effects)
    return result


@router.get("/users/{user_ref}/ledger")
def user_ledger(user_ref: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        user = db.get(User, user_ref) if len(user_ref) <= 32 else None
        if user is None:
            raise not_found()
        a = monetization.latest(db, user.id)
        return {"balances": monetization.balance(db, user.id), "history": monetization.history(db, user.id, 500),
                "application": monetization.admin_view(db, a) if a else None}


@router.post("/users/{user_ref}/ledger", status_code=201)
def ledger_add(user_ref: str, body: LedgerBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = monetization.record(db, st.settings, user_ref, kind=body.kind, amount=body.amount, currency=body.currency,
                                     note=body.note, actor=ac.actor, paid_on=body.paid_on, effects=effects)
    st.dispatch(effects)
    return result


@router.post("/ledger/{entry_id}/reverse", status_code=201)
def ledger_reverse(entry_id: int, body: ReverseBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = monetization.reverse(db, entry_id, ac.actor, body.note, effects)
    st.dispatch(effects)
    return result
