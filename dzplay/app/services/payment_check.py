"""V6 phase 13: the automatic check of membership payments on the chain.

THE RULE: the system checks and SHOWS the result; it never accepts or rejects a request. Only membership.decide
changes a request's status — from the panel or the owner's Telegram buttons (accept / reject / ban). V6 phase 16: the
one exception is AUTO_APPROVE_MEMBERSHIP (off by default) — services/membership_auto.py grants a ✅ request whose every
condition holds; nothing is ever rejected automatically.

For every TXID of a request (the first payment, then top-ups): success and confirmations, the recipient is the
platform wallet, the token is the official USDT contract, the time is after the account was created, the TXID was not
used elsewhere. The request then gets, from the sum of the valid transfers against the price:

    ✅ match · ⚠️ under (missing amount) · ⚠️ over (excess) · ❌ mismatch (reasons) · ⏳ waiting (checked again)

A TXID not found yet is checked again every MEMBERSHIP_CHECK_INTERVAL_MIN for MEMBERSHIP_CHECK_MAX_HOURS by the chain
loop (main._chain_loop), which claims due rows with an atomic UPDATE, so several instances never check one twice.
Network calls are made outside any database transaction. The owner's Telegram message is updated with each result.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app import clock, i18n
from app.config import Settings
from app.errors import AppError
from app.i18n import N_
from app.models import MembershipRefund, MembershipRequest, MembershipTx, User, VerificationRequest, WithdrawalRequest
from app.services import audit, chain, notify

log = logging.getLogger("dzplay.payment_check")

RETRY = (chain.PENDING, chain.NOT_FOUND, chain.ERROR)
EXPIRED = "expired"
CLAIM_SECONDS = 300  # a claimed row is not picked by another instance for this long
MICRO_PER_CENT = 10_000
SKEW = timedelta(minutes=2)

# the panel and Telegram are the owner's: Arabic
REASON = {
    chain.FAILED: "العملية فاشلة على البلوكشين",
    chain.WRONG_RECIPIENT: "المستلم ليس محفظة المنصة",
    chain.FAKE_TOKEN: "عملة مزيفة: ليست عقد USDT الرسمي",
    chain.NOT_TOKEN: "ليست تحويل USDT",
    chain.BEFORE_ACCOUNT: "العملية أقدم من إنشاء الحساب",
    chain.USED: "رقم العملية مستخدم من قبل",
    EXPIRED: "لم يُعثر على العملية على البلوكشين",
    chain.UNSUPPORTED: "لا فحص آلي لهذه الشبكة",
}
TX_LABEL = {
    chain.OK: "✅ مؤكدة",
    chain.PENDING: "⏳ بانتظار التأكيدات",
    chain.NOT_FOUND: "⏳ لم يُعثر عليها بعد",
    chain.ERROR: "⏳ تعذّر الاتصال بالشبكة، ستُعاد المحاولة",
    **{k: f"❌ {v}" for k, v in REASON.items()},
    chain.UNSUPPORTED: "➖ لا فحص آلي لهذه الشبكة",
}
ICON = {"match": "✅", "under": "⚠️", "over": "⚠️", "mismatch": "❌", "waiting": "⏳", "unsupported": "➖", "off": "➖"}


def usdt(micro: int | None) -> str:
    """Micro-USDT → «12.50» (cents, rounded down)."""
    micro = int(micro or 0)
    sign = "-" if micro < 0 else ""
    m = abs(micro)
    return f"{sign}{m // 1_000_000}.{(m % 1_000_000) // 10_000:02d}"


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.rstrip("Z"))
    except ValueError:
        return None


def txs(db: Session, req: MembershipRequest) -> list[MembershipTx]:
    return list(db.execute(select(MembershipTx).where(MembershipTx.request_id == req.id).order_by(MembershipTx.id)).scalars())


def add_tx(db: Session, req: MembershipRequest, txid: str, kind: str) -> MembershipTx:
    now = clock.utcnow()
    tx = MembershipTx(request_id=req.id, txid=txid, network=req.network, kind=kind, status=chain.NOT_FOUND, attempts=0,
                      next_check_at=now, created_at=now)
    db.add(tx)
    db.flush()
    return tx


def txid_taken(db: Session, txid: str) -> bool:
    """A TXID already given for a membership request (first payment or top-up) or the old star requests."""
    low = txid.lower()
    return bool(db.scalar(select(MembershipRequest.id).where(func.lower(MembershipRequest.txid) == low).limit(1))
                or db.scalar(select(MembershipTx.id).where(func.lower(MembershipTx.txid) == low).limit(1))
                or db.scalar(select(VerificationRequest.id).where(func.lower(VerificationRequest.txid) == low).limit(1)))


def _used_elsewhere(db: Session, tx: MembershipTx) -> bool:
    low = tx.txid.lower()
    queries = (
        select(VerificationRequest.id).where(func.lower(VerificationRequest.txid) == low),
        select(MembershipRefund.id).where(func.lower(MembershipRefund.refund_txid) == low),
        select(WithdrawalRequest.id).where(func.lower(WithdrawalRequest.txid) == low),
        select(MembershipTx.id).where(func.lower(MembershipTx.txid) == low, MembershipTx.id != tx.id),
        select(MembershipRequest.id).where(func.lower(MembershipRequest.txid) == low, MembershipRequest.id != tx.request_id),
    )
    return any(db.scalar(q.limit(1)) for q in queries)


# ---------------------------------------------------------------------------------------------- one TXID


def _apply(settings: Settings, tx: MembershipTx, result: chain.TxResult, user_created: datetime | None) -> None:
    now = clock.utcnow()
    when = _parse(result.time)
    # a payment cannot come before the account (2 minutes allowed for the chain's and the server's clocks)
    if result.status in (chain.OK, chain.PENDING) and when and user_created and when < user_created - SKEW:
        result.status = chain.BEFORE_ACCOUNT
    status = result.status
    if status in RETRY:
        hours = settings.MEMBERSHIP_CHECK_MAX_HOURS * (4 if status == chain.PENDING else 1)  # found: wait longer
        if now >= tx.created_at + timedelta(hours=hours):
            status, tx.next_check_at = EXPIRED, None
        else:
            minutes = 1 if status == chain.PENDING else settings.MEMBERSHIP_CHECK_INTERVAL_MIN
            tx.next_check_at = now + timedelta(minutes=minutes)
    else:
        tx.next_check_at = None
    tx.status = status
    tx.amount_micro = result.amount_micro
    tx.confirmations = result.confirmations
    tx.tx_time = when
    tx.result = json.dumps(result.to_dict(), ensure_ascii=False)
    tx.attempts = (tx.attempts or 0) + 1
    tx.checked_at = now


def aggregate(db: Session, settings: Settings, req: MembershipRequest) -> str:
    """The request's result from its TXIDs (stored on the request for the panel and Telegram)."""
    rows = txs(db, req)
    price = req.amount_minor * MICRO_PER_CENT
    tolerance = round(float(settings.PAYMENT_AMOUNT_TOLERANCE) * 1_000_000)
    ok = [t for t in rows if t.status == chain.OK]
    received = sum(t.amount_micro or 0 for t in ok)
    waiting = [t for t in rows if t.status in RETRY]
    bad = [t for t in rows if t.status in chain.HARD_FAIL or t.status == EXPIRED]
    diff = received - price
    if rows and all(t.status == chain.UNSUPPORTED for t in rows):
        status = "unsupported"
    elif waiting and received < price - tolerance:
        status = "waiting"
    elif not ok:
        status = "mismatch" if bad else "waiting"
    elif abs(diff) <= tolerance:
        status = "match"
    else:
        status = "under" if diff < 0 else "over"
    req.check_status, req.received_micro, req.diff_micro, req.checked_at = status, received, diff, clock.utcnow()
    req.check_summary = json.dumps({"reasons": sorted({REASON[t.status] for t in bad})}, ensure_ascii=False)
    return status


# ---------------------------------------------------------------------------------------------- running a check


def run(state, request_id: str, force: bool = False) -> str | None:
    """Check the waiting TXIDs of a request (all of them with force), store the result, update the owner's Telegram
    message (sent the first time). Background (pipeline / chain loop) or the panel's «إعادة الفحص». Never decides."""
    settings = state.settings
    effects = None
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is None:
            return None
        rows = txs(db, req)
        if not rows and req.txid:  # a request from before phase 13: its first payment is checked now
            rows = [add_tx(db, req, req.txid, "primary")]
        if req.status != "pending":  # decided: nothing more to watch
            for t in rows:
                t.next_check_at = None
            return req.check_status
        user = db.get(User, req.user_id) if req.user_id else None
        user_created = user.created_at if user is not None else None
        enabled = bool(settings.CHAIN_CHECK_ENABLED)
        todo = [(t.id, t.network, t.txid, _used_elsewhere(db, t)) for t in rows if enabled and (force or t.status in RETRY)]
        wallet = req.wallet
    # the chain is read outside any transaction
    results = {}
    if todo:
        reader = state.chain_reader()
        for tx_id, network, txid, used in todo:
            results[tx_id] = chain.TxResult(chain.USED) if used else reader.check(network, txid, wallet)
    from app.services.messaging import Effects

    effects = Effects()
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is None or req.status != "pending":
            return None
        for tx_id, result in results.items():
            tx = db.get(MembershipTx, tx_id)
            if tx is not None:
                _apply(settings, tx, result, user_created)
        before = req.check_status
        status = aggregate(db, settings, req) if enabled else "off"
        if not enabled:
            req.check_status = "off"
        changed = force or status != before or bool(results)
        if (enabled and settings.AUTO_NOTIFY_UNDERPAYMENT and status == "under"
                and req.underpay_notified_micro != -(req.diff_micro or 0)):
            request_topup(db, settings, req, "system:auto", effects)
        if enabled and status in ("match", "over"):  # V6 phase 16 (off by default)
            from app.services import membership_auto

            if membership_auto.try_approve(db, settings, req, status, effects):
                changed = True
        first = req.tg_message_id is None
    state.dispatch(effects)
    if first:
        send_owner(state, request_id)
    elif changed:
        update_owner(state, request_id)
    return status


def due(state, limit: int = 20) -> list[str]:
    """Claim the TXIDs due for a new check (atomic per row, safe across instances); returns their requests."""
    now = clock.utcnow()
    claimed: list[str] = []
    with state.database.session() as db:
        rows = db.execute(select(MembershipTx.id, MembershipTx.request_id, MembershipTx.next_check_at)
                          .where(MembershipTx.next_check_at.is_not(None), MembershipTx.next_check_at <= now,
                                 MembershipTx.status.in_(RETRY))
                          .order_by(MembershipTx.next_check_at).limit(limit)).all()
        for tx_id, request_id, at in rows:
            res = db.execute(update(MembershipTx).where(MembershipTx.id == tx_id, MembershipTx.next_check_at == at)
                             .values(next_check_at=now + timedelta(seconds=CLAIM_SECONDS)))
            if res.rowcount == 1 and request_id not in claimed:
                claimed.append(request_id)
    return claimed


# ---------------------------------------------------------------------------------------------- views


def view(db: Session, req: MembershipRequest) -> dict:
    """The result for the panel (Arabic)."""
    from app.services.verification import NETWORKS, explorer_link, payment_settings

    rows = txs(db, req)
    pay = payment_settings(db)
    template = (pay["explorer"] if pay["network"] == req.network else NETWORKS.get(req.network, {}).get("explorer", "")) \
        or chain.EXPLORER.get(req.network, "")
    status = req.check_status
    summary = json.loads(req.check_summary) if req.check_summary else {}
    price = req.amount_minor * MICRO_PER_CENT
    out = {"status": status, "icon": ICON.get(status or "", "➖"), "text": status_text(req, rows), "price": usdt(price),
           "received": usdt(req.received_micro) if req.received_micro is not None else None,
           "diff": usdt(abs(req.diff_micro)) if req.diff_micro is not None else None,
           "reasons": summary.get("reasons", []), "checked_at": _iso(req.checked_at),
           "underpay_notified_at": _iso(req.underpay_notified_at),
           "can_request_topup": req.status == "pending" and status == "under",
           "txs": [{"id": t.id, "txid": t.txid, "kind": t.kind, "status": t.status, "label": TX_LABEL.get(t.status, t.status),
                    "amount": usdt(t.amount_micro) if t.amount_micro else None, "confirmations": t.confirmations,
                    "time": _iso(t.tx_time), "explorer_url": explorer_link(template, t.txid), "checked_at": _iso(t.checked_at),
                    "attempts": t.attempts} for t in rows]}
    return out


def _iso(when: datetime | None) -> str | None:
    return when.isoformat(timespec="seconds") + "Z" if when else None


def status_text(req: MembershipRequest, rows: list[MembershipTx] | None = None) -> str:
    s = req.check_status
    received, price = usdt(req.received_micro), usdt(req.amount_minor * MICRO_PER_CENT)
    if s is None:
        return "➖ لم يُفحص آليًا بعد"
    if s == "off":
        return "➖ الفحص الآلي متوقف (الإعدادات ← العضوية)"
    if s == "unsupported":
        return f"➖ لا فحص آلي لشبكة {req.network}: تحقق يدويًا من المستكشف"
    if s == "match":
        return f"✅ مطابق — وصل {received} USDT"
    if s == "under":
        return f"⚠️ ناقص {usdt(abs(req.diff_micro or 0))} USDT — وصل {received} من {price}"
    if s == "over":
        return f"⚠️ زائد {usdt(abs(req.diff_micro or 0))} USDT — وصل {received} من {price}"
    reasons = (json.loads(req.check_summary) if req.check_summary else {}).get("reasons", [])
    if s == "mismatch":
        return "❌ غير مطابق: " + ("، ".join(reasons) or "—")
    pending = [t for t in (rows or []) if t.status == chain.PENDING]
    if pending:
        r = json.loads(pending[0].result or "{}")
        return f"⏳ وُجدت العملية، بانتظار التأكيدات ({r.get('detail') or pending[0].confirmations})"
    return "⏳ لم يُعثر عليها بعد — يُعاد الفحص تلقائيًا"


def user_view(db: Session, req: MembershipRequest, settings: Settings) -> dict | None:
    """«عضويتي»: after the owner (or the automatic setting) asked for the missing amount — what is missing, where to
    send it, the TXIDs already given. Nothing else of the check is shown to the user."""
    if req.status != "pending" or req.underpay_notified_at is None:
        return None
    from app.services.membership import REFUND_NETWORKS

    rows = txs(db, req)
    missing = -(req.diff_micro or 0) if (req.diff_micro or 0) < 0 else 0
    return {"missing": usdt(missing or req.underpay_notified_micro), "wallet": req.wallet, "network": req.network,
            "network_label": i18n.tr(REFUND_NETWORKS.get(req.network, {}).get("label", req.network)),
            "txids": [t.txid for t in rows], "can_add": len(rows) < settings.MEMBERSHIP_MAX_TXIDS,
            "checking": any(t.status in RETRY for t in rows)}


# ---------------------------------------------------------------------------------------------- the missing amount


def request_topup(db: Session, settings: Settings, req: MembershipRequest, actor: str, effects) -> None:
    """Ask the user for the missing amount: an in-app notification and an e-mail in their language (exact amount,
    same address, same network). From the panel, the owner's Telegram button, or AUTO_NOTIFY_UNDERPAYMENT."""
    if req.status != "pending":
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    if req.check_status != "under" or (req.diff_micro or 0) >= 0:
        raise AppError(409, "not_underpaid", "المبلغ المدفوع ليس أقل من السعر.")
    missing = -req.diff_micro
    req.underpay_notified_at, req.underpay_notified_micro = clock.utcnow(), missing
    if req.user_id:
        notify.create(db, req.user_id, "membership_topup", effects, data=notify.msg(
            N_("ينقص دفع العضوية {amount} USDT. أرسل الفارق على شبكة {network} إلى العنوان نفسه، ثم أضف رقم العملية في «عضويتي»."),
            amount=usdt(missing), network=req.network))
        effects.signal(req.user_id, "account")
        effects.later(mail_topup, req.id)
    audit.record(db, actor, "membership_topup_request", target_type="membership", target_id=req.id,
                 detail=f"{usdt(missing)} USDT")


def mail_topup(state, request_id: str) -> None:
    from app.services import mail

    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        user = db.get(User, req.user_id) if req and req.user_id else None
        if req is None or user is None or not user.email or not req.underpay_notified_micro:
            return
        out = mail.prepare(db, state.settings, "system", user.email, "email.membership_topup",
                           {"amount": usdt(req.underpay_notified_micro), "network": req.network, "wallet": req.wallet},
                           lang=i18n.user_lang(user))
    if out is None:
        return
    try:
        mail.deliver(out)
    except Exception as exc:  # noqa: BLE001
        log.info("top-up mail not sent: %s", type(exc).__name__)


def add_topup(db: Session, settings: Settings, limiter, user: User, request_id: object, txid: object, effects) -> MembershipRequest:
    """The user adds the TXID of the missing amount to the same request; the amounts are summed when checked."""
    from app.services.membership import _clean_txid
    from app.services.messaging import _check_limits
    from app.services.rate_limit import Limit

    req = db.get(MembershipRequest, request_id) if isinstance(request_id, str) and len(request_id) <= 32 else None
    if req is None or req.user_id != user.id:
        raise AppError(404, "not_found", "غير موجود.")
    if req.status != "pending" or req.underpay_notified_at is None:
        raise AppError(409, "topup_closed", "لا يمكن إضافة عملية إلى هذا الطلب الآن.")
    if len(txs(db, req)) >= settings.MEMBERSHIP_MAX_TXIDS:
        raise AppError(409, "too_many_txids", "وصلت إلى الحد الأقصى لعدد العمليات في الطلب. تواصل مع الدعم.")
    _check_limits(limiter, [Limit(f"membership_topup:{user.id}", 10, 86400)])
    tx = _clean_txid(req.network, txid)
    if txid_taken(db, tx):
        raise AppError(409, "txid_used", "رقم العملية هذا مستخدم في طلب آخر.")
    add_tx(db, req, tx, "topup")
    audit.record(db, f"user:{user.public_id}", "membership_topup", target_type="membership", target_id=req.id)
    effects.later(run, req.id)
    return req


# ---------------------------------------------------------------------------------------------- ban (owner)


def ban(db: Session, settings: Settings, req: MembershipRequest, actor: str, effects, note: str = "") -> None:
    """The owner's «حظر»: the account is banned and the request rejected (both audited)."""
    from app.services import admin as admin_svc
    from app.services.membership import decide

    if req.status != "pending":
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    if req.user_id:
        admin_svc.set_user_status(db, req.user_id, "banned", effects, settings)
    audit.record(db, actor, "membership_ban", target_type="membership", target_id=req.id, reason=note or None)
    decide(db, settings, req, "reject", actor, note or N_("رُفض الطلب وحُظر الحساب."), effects)


# ---------------------------------------------------------------------------------------------- Telegram (owner)


def owner_text(db: Session, req: MembershipRequest) -> str:
    from app.services.membership import admin_view

    v = admin_view(db, req)
    c = v["check"]
    lines = [f"💳 طلب عضوية — {v['public_id']}", f"المبلغ المطلوب: {v['amount']} {v['currency']} ({v['network']})",
             f"الفحص الآلي: {c['text']}"]
    for i, t in enumerate(c["txs"], 1):
        amount = f" · {t['amount']} USDT" if t["amount"] else ""
        conf = f" · {t['confirmations']} تأكيد" if t["confirmations"] else ""
        lines.append(f"{i}) {'الفارق ' if t['kind'] == 'topup' else ''}{t['txid']}\n   {t['label']}{amount}{conf}\n   {t['explorer_url'] or ''}")
    if req.underpay_notified_at:
        lines.append(f"💬 طُلب الفارق من المستخدم ({usdt(req.underpay_notified_micro)} USDT)")
    from app.services.membership_auto import note_of

    note = note_of(req)
    if req.auto_state == "granted":
        lines.append("✅ تم منح العضوية تلقائيًا بعد الفحص" + (" — ⚠️ مبلغ زائد: للمراجعة" if note.get("review") == "over" else ""))
    elif req.auto_state == "revoked":
        lines.append("🚫 سُحبت العضوية الممنوحة تلقائيًا")
    elif req.status == "pending" and note.get("manual"):
        lines.append("↩️ للمراجعة اليدوية: " + "، ".join(note["manual"]))
    if req.status != "pending":
        lines.append(f"القرار: {v['status_label']}")
    return "\n".join(lines)


def owner_keyboard(req: MembershipRequest, confirm_ban: bool = False, confirm: str | None = None) -> dict | None:
    rid = req.id
    if req.status == "accepted" and req.auto_state == "granted":  # V6 phase 16: the owner can take it back
        if confirm == "revoke":
            return {"inline_keyboard": [[{"text": "🚫 تأكيد سحب العضوية", "callback_data": f"ms:revokeyes:{rid}"},
                                         {"text": "↩️ تراجع", "callback_data": f"ms:menu:{rid}"}]]}
        return {"inline_keyboard": [[{"text": "🚫 سحب العضوية", "callback_data": f"ms:revoke:{rid}"}]]}
    if req.status != "pending":
        return None
    if confirm_ban:
        return {"inline_keyboard": [[{"text": "⛔ تأكيد الحظر والرفض", "callback_data": f"ms:banyes:{rid}"},
                                     {"text": "↩️ تراجع", "callback_data": f"ms:menu:{rid}"}]]}
    second = [{"text": "⛔ حظر", "callback_data": f"ms:ban:{rid}"}]
    if req.check_status == "under":
        second.append({"text": "💬 طلب الفارق", "callback_data": f"ms:topup:{rid}"})
    second.append({"text": "🔄 إعادة الفحص", "callback_data": f"ms:recheck:{rid}"})
    return {"inline_keyboard": [[{"text": "✅ قبول", "callback_data": f"ms:accept:{rid}"},
                                 {"text": "❌ رفض", "callback_data": f"ms:reject:{rid}"}], second]}


def send_owner(state, request_id: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is None or req.tg_message_id is not None:
            return
        text, kb = owner_text(db, req), owner_keyboard(req)
    try:
        res = bot.tg.send_message(bot.admin_id, text, reply_markup=kb)
    except Exception as exc:  # noqa: BLE001
        log.warning("membership notice not sent: %s", type(exc).__name__)
        return
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is not None and isinstance(res, dict):
            req.tg_message_id = res.get("message_id")


def update_owner(state, request_id: str, confirm_ban: bool = False, confirm: str | None = None) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is None or req.tg_message_id is None:
            return
        text, kb, msg_id = owner_text(db, req), owner_keyboard(req, confirm_ban, confirm), req.tg_message_id
    try:
        bot.tg.edit_message_text(bot.admin_id, msg_id, text, reply_markup=kb)
    except Exception as exc:  # noqa: BLE001 - e.g. «message is not modified»
        log.info("membership message not updated: %s", type(exc).__name__)
