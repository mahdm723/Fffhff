"""V6 phase 16: membership granted automatically after the chain check (AUTO_APPROVE_MEMBERSHIP, off by default).

A request is granted without the owner only when ALL of these hold:
* the setting is on and the automatic check runs (TRC20 / BEP20 — any other network stays manual);
* every TXID of the request is ✅ (enough confirmations, our wallet, the official USDT contract, after the account was
  created, never used) — none still waiting, none failed;
* the sum covers the price (match, or over: then granted and marked for the owner's review);
* the account is active and not suspicious (services/suspicion.py — never because of a shared network);
* fewer than AUTO_APPROVE_DAILY_MAX automatic grants today (UTC).
Anything else stays for the owner, with the reason recorded (auto_note).

No double grant: the request is claimed by an atomic UPDATE (auto_state NULL → granting, status pending), the daily
counter row is locked, and membership.decide keeps its own user lock, pending check and idempotent ledger entry.
Every automatic grant: the audit log, the owner's Telegram message «تم منح العضوية تلقائيًا» with «سحب العضوية», the
user's notification and e-mail (from decide).
"""

from __future__ import annotations

import json
import logging

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.i18n import N_
from app.models import AppSetting, MembershipRequest, MembershipTx, User
from app.services import audit, chain, notify, suspicion

log = logging.getLogger("dzplay.membership_auto")
ACTOR = "system:auto"
COUNTER_KEY = "auto_approve.day"


def _note(req: MembershipRequest, **data) -> None:
    req.auto_note = json.dumps(data, ensure_ascii=False)


def note_of(req: MembershipRequest) -> dict:
    try:
        return json.loads(req.auto_note or "{}")
    except ValueError:
        return {}


def _take_slot(db: Session, settings: Settings) -> bool:
    """One more automatic grant today? (the counter row is locked: concurrent checks count one by one)"""
    today = clock.utcnow().date().isoformat()
    row = db.get(AppSetting, COUNTER_KEY, with_for_update=True)
    if row is None:
        row = AppSetting(key=COUNTER_KEY, value=json.dumps({"day": today, "n": 0}), updated_at=clock.utcnow(),
                         updated_by="system")
        db.add(row)
        db.flush()
        row = db.get(AppSetting, COUNTER_KEY, with_for_update=True)
    try:
        data = json.loads(row.value)
    except ValueError:
        data = {}
    n = int(data.get("n") or 0) if data.get("day") == today else 0
    if n >= max(0, int(settings.AUTO_APPROVE_DAILY_MAX)):
        return False
    row.value, row.updated_at = json.dumps({"day": today, "n": n + 1}), clock.utcnow()
    return True


def try_approve(db: Session, settings: Settings, req: MembershipRequest, status: str, effects) -> bool:
    """Called by payment_check.run right after the request's result is computed (same transaction)."""
    if not settings.AUTO_APPROVE_MEMBERSHIP or not settings.CHAIN_CHECK_ENABLED:
        return False
    if req.status != "pending" or req.auto_state is not None:
        return False
    if status not in ("match", "over"):
        return False  # under / mismatch / waiting / unsupported network: the owner decides (nothing to record)
    rows = list(db.scalars(select(MembershipTx).where(MembershipTx.request_id == req.id)))
    if not rows or any(t.status != chain.OK for t in rows):
        _note(req, manual=["عملية ليست مؤكدة كلها بعد أو إحداها غير صالحة"])
        return False
    user = db.get(User, req.user_id) if req.user_id else None
    if user is None or user.status != "active":
        _note(req, manual=["الحساب غير نشط"])
        return False
    why = suspicion.reasons(db, user)
    if why:
        _note(req, manual=why)
        return False
    db.flush()  # the check result first: the refresh below (and decide's) must not drop it
    claimed = db.execute(update(MembershipRequest)
                         .where(MembershipRequest.id == req.id, MembershipRequest.auto_state.is_(None),
                                MembershipRequest.status == "pending")
                         .values(auto_state="granting")).rowcount
    if claimed != 1:
        return False  # another check got there first
    db.refresh(req)
    if not _take_slot(db, settings):
        req.auto_state = None
        _note(req, manual=[f"وصل المنح التلقائي إلى حده اليومي ({settings.AUTO_APPROVE_DAILY_MAX})"])
        return False
    from app.services import membership

    try:
        membership.decide(db, settings, req, "accept", ACTOR, "", effects)
    except AppError:
        req.auto_state = None  # decided meanwhile (by the owner): nothing to do
        return False
    req.auto_state = "granted"
    review = status == "over"
    _note(req, granted=True, review="over" if review else None)
    audit.record(db, ACTOR, "membership_auto_granted", target_type="membership", target_id=req.id,
                 detail=f"{req.network} {req.check_status}" + (" — مبلغ زائد: للمراجعة" if review else ""))
    return True


def revoke(db: Session, settings: Settings, req: MembershipRequest, actor: str, effects, reason: str = "") -> dict:
    """«سحب العضوية» of an automatic grant (Telegram or panel): the membership stops, the invitation reward that this
    acceptance created is reversed, the user is told. No automatic refund (the payment stays in the ledger)."""
    from app.services import ledger, membership, rewards

    if req.status != "accepted" or req.auto_state != "granted":
        raise AppError(409, "not_auto_granted", "هذا الطلب لم يُمنح تلقائيًا أو سُحب من قبل.")
    user = db.get(User, req.user_id) if req.user_id else None
    if user is not None:
        ledger.lock_user(db, user.id)
        membership.end(db, user, actor, effects, reason=reason or "auto_revoked")
        rewards.on_membership_refunded(db, settings, user, None, effects)
        notify.create(db, user.id, "membership_revoked", effects, data=notify.msg(N_("أُوقفت عضويتك بعد المراجعة.")))
        effects.later(membership.mail_user, user.id, "revoked", notify.msg(N_("أُوقفت عضويتك بعد مراجعة الدفع. للاستفسار تواصل مع الدعم.")))
    req.auto_state = "revoked"
    audit.record(db, actor, "membership_auto_revoked", target_type="membership", target_id=req.id,
                 reason=(reason or "")[:255] or None)
    from app.services import payment_check

    effects.later(payment_check.update_owner, req.id)
    db.flush()
    return membership.admin_view(db, req)
