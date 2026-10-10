"""V6 phase 5b: withdrawing rewards (USDT on TRC20 / BEP20).

The amount (≥ WITHDRAW_MIN, ≤ available) leaves the balance at once ("withdraw" entry, in one locked
transaction), the fee WITHDRAW_FEE is shown before confirming and deducted from what is sent. Password + e-mail
code, one pending request, WITHDRAW_REQUESTS_PER_DAY. The admin sends the transfer and records its unique TXID,
or rejects with a reason (the amount comes back: "withdraw_rev"). Both are idempotent.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import User, WithdrawalRequest
from app.services import audit, ledger, notify
from app.services.membership import REFUND_NETWORKS, check_password
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.withdrawals")
STATUS_AR = {"pending": "قيد التنفيذ", "done": "تم الإرسال", "rejected": "مرفوض"}


def networks(settings: Settings) -> dict:
    wanted = [n.strip().upper() for n in settings.WITHDRAW_NETWORKS.split(",") if n.strip()]
    return {k: REFUND_NETWORKS[k]["label"] for k in wanted if k in REFUND_NETWORKS}


def _pending(db: Session, user_id: str) -> WithdrawalRequest | None:
    return db.scalar(select(WithdrawalRequest).where(WithdrawalRequest.user_id == user_id,
                                                     WithdrawalRequest.status == "pending"))


def view(r: WithdrawalRequest) -> dict:
    rule = REFUND_NETWORKS.get(r.network, {})
    return {"id": r.id, "status": r.status, "status_label": STATUS_AR.get(r.status), "network": r.network,
            "network_label": rule.get("label", r.network), "address": r.address, "amount": ledger.from_minor(r.amount_minor),
            "fee": ledger.from_minor(r.fee_minor), "net": ledger.from_minor(r.amount_minor - r.fee_minor),
            "txid": r.txid, "reason": r.reason, "created_at": iso(r.created_at), "decided_at": iso(r.decided_at)}


def settings_view(db: Session, settings: Settings, user: User) -> dict:
    p = _pending(db, user.id)
    recent = db.execute(select(WithdrawalRequest).where(WithdrawalRequest.user_id == user.id)
                        .order_by(WithdrawalRequest.created_at.desc()).limit(10)).scalars().all()
    return {"enabled": settings.WITHDRAW_ENABLED, "min": ledger.from_minor(ledger.to_minor(settings.WITHDRAW_MIN)),
            "fee": ledger.from_minor(ledger.to_minor(settings.WITHDRAW_FEE)), "networks": networks(settings),
            "pending": view(p) if p else None, "history": [view(r) for r in recent]}


def request(db: Session, settings: Settings, user: User, body: dict, effects: Effects) -> dict:
    from app.services import email_codes
    from app.services.messaging import _require_can_send

    _require_can_send(user)
    if not settings.WITHDRAW_ENABLED:
        raise AppError(403, "withdraw_off", "السحب متوقف حاليًا.")
    network = body.get("network")
    nets = networks(settings)
    if network not in nets:
        raise AppError(400, "invalid_network", "اختر الشبكة من القائمة.")
    address = body.get("address").strip() if isinstance(body.get("address"), str) else ""
    if not re.match(REFUND_NETWORKS[network]["address"], address):
        raise AppError(400, "invalid_address", f"العنوان لا يطابق شكل عناوين {nets[network]}.")
    try:
        amount = ledger.to_minor(body.get("amount"))
    except Exception:  # noqa: BLE001
        raise AppError(400, "invalid_amount", "أدخل المبلغ.") from None
    fee = ledger.to_minor(settings.WITHDRAW_FEE)
    if amount < ledger.to_minor(settings.WITHDRAW_MIN):
        raise AppError(400, "below_min", f"أقل مبلغ للسحب {settings.WITHDRAW_MIN:g} USDT.")
    if amount <= fee:
        raise AppError(400, "below_fee", "المبلغ يجب أن يكون أكبر من الرسوم.")
    ledger.lock_user(db, user.id)  # from here: one withdrawal at a time for this user (PostgreSQL row lock)
    if _pending(db, user.id) is not None:
        raise AppError(409, "withdraw_open", "لديك طلب سحب قيد التنفيذ.")
    since = clock.utcnow() - timedelta(days=1)
    today = db.scalar(select(func.count()).select_from(WithdrawalRequest).where(
        WithdrawalRequest.user_id == user.id, WithdrawalRequest.created_at > since)) or 0
    if today >= settings.WITHDRAW_REQUESTS_PER_DAY:
        raise AppError(429, "withdraw_limit", "وصلت إلى حد طلبات السحب اليوم.")
    if amount > ledger.balances(db, user.id, "rewards")["available"]:
        raise AppError(400, "over_balance", "المبلغ أكبر من رصيدك المتاح.")
    check_password(user, body.get("password"))
    email_codes.verify(db, settings, user, "withdraw", body.get("code"))
    now = clock.utcnow()
    w = WithdrawalRequest(user_id=user.id, user_public_id=user.public_id, network=network, address=address,
                          amount_minor=amount, fee_minor=fee, status="pending", created_at=now)
    db.add(w)
    db.flush()
    ledger.post(db, user_id=user.id, account="rewards", kind="withdraw", amount_minor=-amount, source_type="withdraw",
                source_id=w.id, actor=f"user:{user.public_id}", note=f"{network} {address[:6]}…{address[-4:]}")
    audit.record(db, f"user:{user.public_id}", "withdraw_request", target_type="withdrawal", target_id=w.id,
                 detail=f"{ledger.from_minor(amount)} USDT {network}")
    effects.later(notify_admin, w.id)
    return view(w)


def decide(db: Session, w: WithdrawalRequest, action: str, actor: str, *, txid: object = None, note: str = "",
           effects: Effects) -> dict:
    if action not in ("done", "reject"):
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    if w.user_id:
        ledger.lock_user(db, w.user_id)
    db.refresh(w)
    if w.status != "pending":
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    note = (note or "").strip()[:500]
    if action == "done":
        tx = txid.strip() if isinstance(txid, str) else ""
        if not re.match(REFUND_NETWORKS[w.network]["tx"], tx):
            raise AppError(400, "invalid_txid", "أدخل رقم عملية التحويل (TXID) الصحيح.")
        if db.scalar(select(WithdrawalRequest.id).where(func.lower(WithdrawalRequest.txid) == tx.lower())):
            raise AppError(409, "txid_used", "رقم العملية هذا مسجّل لسحب آخر.")
        w.txid = tx
    else:
        if not note:
            raise AppError(400, "reason_required", "اكتب سبب الرفض.")
        if w.user_id:
            ledger.post(db, user_id=w.user_id, account="rewards", kind="withdraw_rev", amount_minor=w.amount_minor,
                        source_type="withdraw", source_id=w.id, actor=actor, note=note)
    w.status = "done" if action == "done" else "rejected"
    w.reason, w.decided_at, w.decided_by = note or None, clock.utcnow(), actor[:80]
    audit.record(db, actor, f"withdraw_{action}", target_type="withdrawal", target_id=w.id,
                 detail=f"{ledger.from_minor(w.amount_minor)} USDT {w.network}", reason=note or None)
    if w.user_id:
        text = (f"أُرسل سحبك: {ledger.from_minor(w.amount_minor - w.fee_minor)} USDT على {w.network}." if action == "done"
                else f"رُفض طلب السحب وأُعيد المبلغ إلى رصيدك. السبب: {note}")
        notify.create(db, w.user_id, f"withdraw_{action}", effects, data={"text": text})
        effects.signal(w.user_id, "account")
        from app.services.membership import mail_user

        effects.later(mail_user, w.user_id, f"withdraw_{action}", text)
    db.flush()
    return admin_view(db, w)


def admin_view(db: Session, w: WithdrawalRequest) -> dict:
    from app.services.verification import explorer_link

    user = db.get(User, w.user_id) if w.user_id else None
    return {**view(w), "user_ref": w.user_id, "public_id": w.user_public_id, "name": user.display_name if user else None,
            "explorer_url": explorer_link(REFUND_NETWORKS.get(w.network, {}).get("explorer", ""), w.txid) if w.txid else None,
            "decided_by": w.decided_by}


def admin_list(db: Session, status: str = "") -> dict:
    q = select(WithdrawalRequest).order_by(WithdrawalRequest.created_at.desc())
    if status in STATUS_AR:
        q = q.where(WithdrawalRequest.status == status)
    return {"withdrawals": [admin_view(db, w) for w in db.execute(q.limit(200)).scalars()],
            "pending": db.scalar(select(func.count()).select_from(WithdrawalRequest)
                                 .where(WithdrawalRequest.status == "pending")) or 0}


def by_id(db: Session, wid: object) -> WithdrawalRequest:
    w = db.get(WithdrawalRequest, wid) if isinstance(wid, str) and len(wid) <= 32 else None
    if w is None:
        raise not_found()
    return w


def notify_admin(state, wid: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        w = db.get(WithdrawalRequest, wid)
        if w is None:
            return
        v = view(w)
        pid = w.user_public_id
    text = (f"💸 طلب سحب — {pid}\nالمبلغ: {v['amount']} USDT (يُرسل {v['net']} بعد رسوم {v['fee']})\n"
            f"الشبكة: {v['network_label']}\nالعنوان: {v['address']}\n\nأرسل التحويل ثم سجّل رقم العملية من اللوحة ← المكافآت والسحب.")
    try:
        bot.tg.send_message(bot.admin_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("withdrawal notice not sent: %s", type(exc).__name__)
