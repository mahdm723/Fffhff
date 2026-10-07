"""V6 phase 5: membership — a one-time payment (MEMBERSHIP_PRICE, 50 USDT by default) for in-app FEATURES only:
the blue star, picture posts, chat pictures. It is never linked to trading, earnings or any return, and paying
never counts in any reward rule.

Flow: the member pays to the wallet of the panel's payment settings (pay.*), sends the TXID → the admin accepts
or rejects (panel or Telegram ms: buttons) → on acceptance, in one locked transaction: ledger "mem_pay", the
membership starts, the blue star is granted if the account had none (verified_by="membership"), notification +
e-mail. Optional refund (MEMBERSHIP_REFUNDABLE) within MEMBERSHIP_REFUND_WINDOW_DAYS of the acceptance, minus
MEMBERSHIP_REFUND_FEE: password + e-mail code, the admin sends the transfer and records its TXID → ledger
"mem_refund" + "mem_fee", the membership ends, the star goes if it came from the membership.
"""

from __future__ import annotations

import logging
import re
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import MembershipRefund, MembershipRequest, User, VerificationRequest
from app.services import audit, ledger, notify
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.membership")

FEATURES = ["النجمة الزرقاء بجانب اسمك", "نشر الأفكار مع صورة", "إرسال صور مؤقتة في المحادثات"]
REFUND_NETWORKS = {
    "TRC20": {"label": "USDT على TRON (TRC20)", "address": r"^T[1-9A-HJ-NP-Za-km-z]{33}$",
              "tx": r"^[0-9a-fA-F]{64}$", "explorer": "https://tronscan.org/#/transaction/{txid}"},
    "BEP20": {"label": "USDT على BNB Smart Chain (BEP20)", "address": r"^0x[0-9a-fA-F]{40}$",
              "tx": r"^0x[0-9a-fA-F]{64}$", "explorer": "https://bscscan.com/tx/{txid}"},
}
STATUS_AR = {"pending": "قيد المراجعة", "accepted": "مقبول", "rejected": "مرفوض"}
REFUND_AR = {"requested": "قيد التنفيذ", "done": "تم الإرجاع", "rejected": "مرفوض"}


def is_member(user: User | None) -> bool:
    return bool(user is not None and user.member_since is not None and user.member_ended_at is None)


def _price_minor(settings: Settings) -> int:
    return ledger.to_minor(settings.MEMBERSHIP_PRICE)


def _fee_minor(settings: Settings) -> int:
    return ledger.to_minor(settings.MEMBERSHIP_REFUND_FEE)


def _accepted(db: Session, user_id: str) -> MembershipRequest | None:
    return db.scalar(select(MembershipRequest).where(MembershipRequest.user_id == user_id,
                                                     MembershipRequest.status == "accepted")
                     .order_by(MembershipRequest.decided_at.desc()))


def _latest(db: Session, user_id: str) -> MembershipRequest | None:
    return db.scalar(select(MembershipRequest).where(MembershipRequest.user_id == user_id)
                     .order_by(MembershipRequest.created_at.desc()))


def _open_refund(db: Session, user_id: str) -> MembershipRefund | None:
    return db.scalar(select(MembershipRefund).where(MembershipRefund.user_id == user_id)
                     .order_by(MembershipRefund.created_at.desc()))


def refund_state(db: Session, settings: Settings, user: User) -> dict:
    req = _accepted(db, user.id) if is_member(user) else None
    last = _open_refund(db, user.id)
    deadline = req.decided_at + timedelta(days=settings.MEMBERSHIP_REFUND_WINDOW_DAYS) if req and req.decided_at else None
    paid = req.amount_minor if req else 0
    fee = min(_fee_minor(settings), paid)
    open_ = last is not None and last.status == "requested"
    eligible = bool(settings.MEMBERSHIP_REFUNDABLE and req and deadline and clock.utcnow() <= deadline and not open_
                    and paid > fee)
    return {"enabled": settings.MEMBERSHIP_REFUNDABLE, "window_days": settings.MEMBERSHIP_REFUND_WINDOW_DAYS,
            "fee": ledger.from_minor(_fee_minor(settings)), "amount": ledger.from_minor(max(0, paid - fee)),
            "deadline": iso(deadline), "eligible": eligible,
            "networks": {k: v["label"] for k, v in REFUND_NETWORKS.items()},
            "request": None if last is None else {"id": last.id, "status": last.status, "status_label": REFUND_AR.get(last.status),
                                                  "amount": ledger.from_minor(last.amount_minor), "network": last.network,
                                                  "reason": last.reason, "created_at": iso(last.created_at),
                                                  "decided_at": iso(last.decided_at)}}


def overview(db: Session, settings: Settings, user: User) -> dict:
    """«عضويتي». Features and status only — never any earning or return."""
    from app.services.verification import payment_settings, qr_svg

    pay = payment_settings(db)
    last = _latest(db, user.id)
    payment = None
    if pay["available"]:
        payment = {"currency": pay["currency"], "network": pay["network"], "network_label": pay["network_label"],
                   "wallet": pay["wallet"], "note": pay["note"], "qr": qr_svg(pay["wallet"])}
    return {
        "price": ledger.from_minor(_price_minor(settings)), "currency": pay["currency"] or ledger.CURRENCY,
        "features": FEATURES, "member": is_member(user), "member_since": iso(user.member_since),
        "ended_at": iso(user.member_ended_at), "payment": payment,
        "request": None if last is None else {"id": last.id, "status": last.status, "status_label": STATUS_AR.get(last.status),
                                              "txid": last.txid, "note": last.admin_note, "created_at": iso(last.created_at),
                                              "amount": ledger.from_minor(last.amount_minor), "network": last.network},
        "refund": refund_state(db, settings, user),
    }


def _clean_txid(network: str, txid: object) -> str:
    from app.services.verification import _clean_txid as clean

    return clean(network, txid)


def _txid_taken(db: Session, txid: str) -> bool:
    low = txid.lower()
    return bool(db.scalar(select(MembershipRequest.id).where(func.lower(MembershipRequest.txid) == low))
                or db.scalar(select(VerificationRequest.id).where(func.lower(VerificationRequest.txid) == low)))


def submit(db: Session, settings: Settings, limiter, user: User, txid: object, effects: Effects) -> dict:
    from app.services.messaging import _check_limits, _require_can_send
    from app.services.rate_limit import Limit
    from app.services.verification import payment_settings

    _require_can_send(user)
    if is_member(user):
        raise AppError(409, "already_member", "عضويتك مفعّلة بالفعل.")
    last = _latest(db, user.id)
    if last is not None and last.status == "pending":
        raise AppError(409, "request_open", "لديك طلب قيد المراجعة بالفعل.")
    pay = payment_settings(db)
    if not pay["available"]:
        raise AppError(503, "payment_unavailable", "الدفع غير متاح حاليًا. حاول لاحقًا.")
    _check_limits(limiter, [Limit(f"membership:{user.id}", settings.MEMBERSHIP_REQUESTS_PER_DAY, 86400)])
    tx = _clean_txid(pay["network"], txid)
    if _txid_taken(db, tx):
        raise AppError(409, "txid_used", "رقم العملية هذا مستخدم في طلب آخر.")
    now = clock.utcnow()
    req = MembershipRequest(user_id=user.id, user_public_id=user.public_id, amount_minor=_price_minor(settings),
                            currency=pay["currency"], network=pay["network"], wallet=pay["wallet"], txid=tx,
                            status="pending", created_at=now)
    try:
        with db.begin_nested():
            db.add(req)
            db.flush()
    except IntegrityError:
        raise AppError(409, "txid_used", "رقم العملية هذا مستخدم في طلب آخر.") from None
    audit.record(db, f"user:{user.public_id}", "membership_request", target_type="membership", target_id=req.id)
    effects.later(notify_admin, req.id)
    return overview(db, settings, user)


# ---------------------------------------------------------------------------
# admin decisions (panel + Telegram) — the only writers of member_since / member_ended_at
# ---------------------------------------------------------------------------

def _on_accept(db, settings, user, req, effects):
    from app.services import rewards

    rewards.on_membership_accepted(db, settings, user, req, effects)


def _on_refund(db, settings, user, refund, effects):
    from app.services import rewards

    rewards.on_membership_refunded(db, settings, user, refund, effects)


ON_ACCEPT: list = [_on_accept]  # V6 phase 5b: the invitee became a member → the inviter's reward
ON_REFUND: list = [_on_refund]  # V6 phase 5b: refunded within the window → the pending reward is cancelled


def decide(db: Session, settings: Settings, req: MembershipRequest, action: str, actor: str, note: str,
           effects: Effects) -> dict:
    if action not in ("accept", "reject"):
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    user = db.get(User, req.user_id) if req.user_id else None
    if user is not None:
        ledger.lock_user(db, user.id)
    db.refresh(req)
    if req.status != "pending":
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    now = clock.utcnow()
    note = (note or "").strip()[:500]
    if action == "reject" and not note:
        note = "لم نتمكن من تأكيد الدفع. تحقق من رقم العملية والمبلغ والشبكة."
    req.status = "accepted" if action == "accept" else "rejected"
    req.admin_note, req.decided_at, req.decided_by = note or None, now, actor[:80]
    if action == "accept" and user is not None:
        ledger.post(db, user_id=user.id, account="membership", kind="mem_pay", amount_minor=req.amount_minor,
                    source_type="mem_request", source_id=req.id, actor=actor, note=f"{req.network} {req.txid[:16]}")
        user.member_since, user.member_ended_at = now, None
        if user.verified_at is None:
            user.verified_at, user.verified_by = now, "membership"
        for hook in ON_ACCEPT:
            hook(db, settings, user, req, effects)
    audit.record(db, actor, f"membership_{action}", target_type="membership", target_id=req.id,
                 detail=f"{ledger.from_minor(req.amount_minor)} {req.currency} {req.network}", reason=note or None)
    if user is not None:
        notify.create(db, user.id, f"membership_{'accepted' if action == 'accept' else 'rejected'}", effects,
                      data={"text": "تم تفعيل عضويتك ✨" if action == "accept" else f"رُفض طلب العضوية: {note}"})
        effects.signal(user.id, "account")
        effects.later(mail_user, user.id, "accepted" if action == "accept" else "rejected", note)
    db.flush()
    return admin_view(db, req)


def _clean_address(network: str, address: object) -> str:
    rule = REFUND_NETWORKS.get(network)
    a = address.strip() if isinstance(address, str) else ""
    if rule is None:
        raise AppError(400, "invalid_network", "اختر الشبكة من القائمة.")
    if not re.match(rule["address"], a):
        raise AppError(400, "invalid_address", f"العنوان لا يطابق شكل عناوين {rule['label']}.")
    return a


def check_password(user: User, password: object) -> None:
    from app.security.passwords import verify_password

    if user.password_hash and not (isinstance(password, str) and verify_password(user.password_hash, password)):
        raise AppError(403, "wrong_password", "كلمة المرور غير صحيحة.")


def request_refund(db: Session, settings: Settings, user: User, body: dict, effects: Effects) -> dict:
    from app.services import email_codes

    ledger.lock_user(db, user.id)
    state = refund_state(db, settings, user)
    if not settings.MEMBERSHIP_REFUNDABLE:
        raise AppError(403, "refund_off", "استرجاع العضوية غير متاح.")
    if not state["eligible"]:
        raise AppError(403, "refund_not_allowed", "لا يمكن طلب الاسترجاع الآن (انتهت المدة أو لديك طلب مفتوح).")
    network = body.get("network")
    address = _clean_address(network, body.get("address"))
    check_password(user, body.get("password"))
    email_codes.verify(db, settings, user, "refund", body.get("code"))
    req = _accepted(db, user.id)
    fee = min(_fee_minor(settings), req.amount_minor)
    now = clock.utcnow()
    refund = MembershipRefund(user_id=user.id, user_public_id=user.public_id, request_id=req.id, network=network,
                              address=address, amount_minor=req.amount_minor - fee, fee_minor=fee, status="requested",
                              created_at=now)
    db.add(refund)
    db.flush()
    audit.record(db, f"user:{user.public_id}", "membership_refund_request", target_type="membership_refund",
                 target_id=refund.id, detail=f"{ledger.from_minor(refund.amount_minor)} {network}")
    effects.later(notify_admin_refund, refund.id)
    return overview(db, settings, user)


def decide_refund(db: Session, settings: Settings, refund: MembershipRefund, action: str, actor: str, *,
                  txid: object = None, note: str = "", effects: Effects) -> dict:
    if action not in ("done", "reject"):
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    user = db.get(User, refund.user_id) if refund.user_id else None
    if user is not None:
        ledger.lock_user(db, user.id)
    db.refresh(refund)
    if refund.status != "requested":
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    note = (note or "").strip()[:500]
    now = clock.utcnow()
    if action == "done":
        rule = REFUND_NETWORKS[refund.network]
        tx = txid.strip() if isinstance(txid, str) else ""
        if not re.match(rule["tx"], tx):
            raise AppError(400, "invalid_txid", "أدخل رقم عملية التحويل (TXID) الصحيح.")
        if db.scalar(select(MembershipRefund.id).where(func.lower(MembershipRefund.refund_txid) == tx.lower())):
            raise AppError(409, "txid_used", "رقم العملية هذا مسجّل لاسترجاع آخر.")
        refund.refund_txid = tx
        if user is not None:
            ledger.post(db, user_id=user.id, account="membership", kind="mem_refund", amount_minor=-refund.amount_minor,
                        source_type="mem_refund", source_id=refund.id, actor=actor, note=f"{refund.network} {tx[:16]}")
            if refund.fee_minor:
                ledger.post(db, user_id=user.id, account="membership", kind="mem_fee", amount_minor=-refund.fee_minor,
                            source_type="mem_refund", source_id=refund.id, actor=actor, note="رسوم الاسترجاع")
            end(db, user, actor, effects, reason="refund")
            for hook in ON_REFUND:
                hook(db, settings, user, refund, effects)
    else:
        if not note:
            raise AppError(400, "reason_required", "اكتب سبب الرفض.")
    refund.status = "done" if action == "done" else "rejected"
    refund.reason, refund.decided_at, refund.decided_by = note or None, now, actor[:80]
    audit.record(db, actor, f"membership_refund_{action}", target_type="membership_refund", target_id=refund.id,
                 detail=f"{ledger.from_minor(refund.amount_minor)} {refund.network}", reason=note or None)
    if user is not None:
        text = (f"أُرسل إليك {ledger.from_minor(refund.amount_minor)} USDT (استرجاع العضوية)." if action == "done"
                else f"رُفض طلب الاسترجاع: {note}")
        notify.create(db, user.id, f"refund_{action}", effects, data={"text": text})
        effects.signal(user.id, "account")
        effects.later(mail_user, user.id, f"refund_{action}", text)
    db.flush()
    return refund_view(db, refund)


def end(db: Session, user: User, actor: str, effects: Effects | None, reason: str = "") -> None:
    """The membership stops (refund, or stopped from the panel). The star goes only if the membership gave it."""
    if user.member_since is None or user.member_ended_at is not None:
        return
    user.member_ended_at = clock.utcnow()
    if user.verified_by == "membership":
        user.verified_at, user.verified_by = None, None
    audit.record(db, actor, "membership_end", target_type="user", target_id=user.public_id, reason=reason[:255] or None)
    if effects is not None:
        effects.signal(user.id, "account")


# ---------------------------------------------------------------------------
# panel views
# ---------------------------------------------------------------------------


def admin_view(db: Session, req: MembershipRequest) -> dict:
    from app.services.verification import NETWORKS, explorer_link, payment_settings

    pay = payment_settings(db)
    template = pay["explorer"] if pay["network"] == req.network else NETWORKS.get(req.network, {}).get("explorer", "")
    user = db.get(User, req.user_id) if req.user_id else None
    return {"id": req.id, "status": req.status, "status_label": STATUS_AR.get(req.status), "user_ref": req.user_id,
            "public_id": req.user_public_id, "name": user.display_name if user else None,
            "amount": ledger.from_minor(req.amount_minor), "currency": req.currency, "network": req.network,
            "wallet": req.wallet, "txid": req.txid, "explorer_url": explorer_link(template, req.txid),
            "note": req.admin_note, "created_at": iso(req.created_at), "decided_at": iso(req.decided_at),
            "decided_by": req.decided_by, "member": is_member(user)}


def refund_view(db: Session, r: MembershipRefund) -> dict:
    from app.services.verification import explorer_link

    rule = REFUND_NETWORKS.get(r.network, {})
    return {"id": r.id, "status": r.status, "status_label": REFUND_AR.get(r.status), "user_ref": r.user_id,
            "public_id": r.user_public_id, "network": r.network, "network_label": rule.get("label", r.network),
            "address": r.address, "amount": ledger.from_minor(r.amount_minor), "fee": ledger.from_minor(r.fee_minor),
            "refund_txid": r.refund_txid, "explorer_url": explorer_link(rule.get("explorer", ""), r.refund_txid) if r.refund_txid else None,
            "reason": r.reason, "created_at": iso(r.created_at), "decided_at": iso(r.decided_at), "decided_by": r.decided_by}


def admin_list(db: Session, status: str = "") -> dict:
    q = select(MembershipRequest).order_by(MembershipRequest.created_at.desc())
    if status in STATUS_AR:
        q = q.where(MembershipRequest.status == status)
    rq = select(MembershipRefund).order_by(MembershipRefund.created_at.desc())
    return {"requests": [admin_view(db, r) for r in db.execute(q.limit(200)).scalars()],
            "refunds": [refund_view(db, r) for r in db.execute(rq.limit(200)).scalars()],
            "counts": {s: db.scalar(select(func.count()).select_from(MembershipRequest).where(MembershipRequest.status == s)) or 0
                       for s in STATUS_AR},
            "open_refunds": db.scalar(select(func.count()).select_from(MembershipRefund)
                                      .where(MembershipRefund.status == "requested")) or 0,
            "members": db.scalar(select(func.count()).select_from(User).where(User.member_since.is_not(None),
                                                                             User.member_ended_at.is_(None))) or 0}


# ---------------------------------------------------------------------------
# Telegram + e-mail (background)
# ---------------------------------------------------------------------------


def notify_admin(state, request_id: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        req = db.get(MembershipRequest, request_id)
        if req is None:
            return
        v = admin_view(db, req)
    text = (f"💳 طلب عضوية — {v['public_id']}\nالمبلغ المطلوب: {v['amount']} {v['currency']} ({v['network']})\n"
            f"TXID: {v['txid']}\n{v['explorer_url'] or ''}")
    kb = {"inline_keyboard": [[{"text": "✅ قبول", "callback_data": f"ms:accept:{request_id}"},
                               {"text": "❌ رفض", "callback_data": f"ms:reject:{request_id}"}]]}
    try:
        res = bot.tg.send_message(bot.admin_id, text, reply_markup=kb)
        with state.database.session() as db:
            req = db.get(MembershipRequest, request_id)
            if req is not None and isinstance(res, dict):
                req.tg_message_id = res.get("message_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("membership notice not sent: %s", type(exc).__name__)


def notify_admin_refund(state, refund_id: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        r = db.get(MembershipRefund, refund_id)
        if r is None:
            return
        v = refund_view(db, r)
    text = (f"↩️ طلب استرجاع عضوية — {v['public_id']}\nالمبلغ: {v['amount']} USDT (بعد رسوم {v['fee']})\n"
            f"الشبكة: {v['network_label']}\nالعنوان: {v['address']}\n\nأرسل التحويل ثم سجّل رقم العملية من اللوحة ← العضوية.")
    try:
        bot.tg.send_message(bot.admin_id, text)
    except Exception as exc:  # noqa: BLE001
        log.warning("refund notice not sent: %s", type(exc).__name__)


def mail_user(state, user_id: str, what: str, note: str = "") -> None:
    from app.services import mailer, runtime_config

    with state.database.session() as db:
        user = db.get(User, user_id)
        if user is None or not user.email:
            return
        settings = runtime_config.effective_settings(db, state.settings)
        email = user.email
    if not settings.smtp_enabled:
        return
    text = {"accepted": "تم تفعيل عضويتك. أصبحت ميزات الأعضاء متاحة لك (النجمة الزرقاء، الأفكار مع صورة، صور المحادثة).",
            "rejected": f"لم نتمكن من قبول طلب العضوية. السبب: {note or '—'}"}.get(what, note)
    try:
        mailer.send_text(settings, email, f"{settings.APP_NAME}: العضوية", f"مرحبًا،\n\n{text}\n\nفريق {settings.APP_NAME}")
    except Exception as exc:  # noqa: BLE001
        log.info("membership mail not sent: %s", type(exc).__name__)


def install(state) -> None:
    """Telegram buttons ms:accept|reject:<request id> (admin chat only)."""
    from app.services.media_moderation import mark_done

    def on_callback(bot, cq: dict, payload: str) -> None:
        action, _, rid = payload.partition(":")
        if action not in ("accept", "reject") or not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", rid):
            bot.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        effects = Effects()
        try:
            with state.database.session() as db:
                req = db.get(MembershipRequest, rid)
                if req is None:
                    bot.tg.answer_callback(cq.get("id"), "غير موجود")
                    return
                decide(db, state.settings, req, action, f"telegram:{(cq.get('from') or {}).get('id')}", "", effects)
        except AppError as exc:
            bot.tg.answer_callback(cq.get("id"), exc.message)
            return
        state.dispatch(effects)
        label = {"accept": "✅ قُبلت العضوية", "reject": "❌ رُفض الطلب"}[action]
        bot.tg.answer_callback(cq.get("id"), label)
        msg = cq.get("message") or {}
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"), label)

    if state.bot is not None:
        state.bot.callback_handlers["ms"] = on_callback


def by_id(db: Session, model, item_id: object):
    row = db.get(model, item_id) if isinstance(item_id, str) and len(item_id) <= 32 else None
    if row is None:
        raise not_found()
    return row
