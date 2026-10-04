"""V5 monetization (verified creators) and «أموالي» (an immutable earnings ledger).

* Shown to verified users only; extra conditions MONETIZE_MIN_REELS / MONETIZE_MIN_LIKES with progress.
* Application: content type, payout e-mail (the account e-mail, or another one confirmed by a code sent
  to it), terms. Reviewed by the admins (panel or Telegram buttons).
* «أموالي» appears once accepted. Only admins write to the ledger; the user can only read it.
  Balance = sum of entries. Nothing is ever edited or deleted: a mistake is undone by a reversal entry.
  A payout is a Binance-style "Red Packet" the admin sends to the payout e-mail outside the app, then
  records here (amount, date, note): it is deducted and the user is told in the app and by e-mail.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import EmailCode, LedgerEntry, MonetizationApplication, User
from app.services import audit
from app.services.content import clean_message
from app.services.creator_reels import published_stats
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.money")
STATUS_AR = {"pending": "قيد المراجعة", "accepted": "مقبول", "rejected": "مرفوض", "needs_fix": "يحتاج تصحيحًا"}
KINDS_AR = {"earning": "أرباح", "payout": "دفعة (الظرف الأحمر)", "adjustment": "تسوية", "reversal": "إلغاء قيد"}
_EMAIL = re.compile(r"^[^@\s<>]{1,64}@[^@\s<>]{1,190}\.[A-Za-z]{2,24}$")
PAYOUT_TEXT = "تم إرسال أرباحك إلى بريدك عبر الظرف الأحمر، ادخل إلى المنصة لاستلامها."


def _hash(settings: Settings, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"email-code:{code}".encode(), hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# application
# ---------------------------------------------------------------------------


def latest(db: Session, user_id: str) -> MonetizationApplication | None:
    return db.execute(select(MonetizationApplication).where(MonetizationApplication.user_id == user_id)
                      .order_by(MonetizationApplication.created_at.desc()).limit(1)).scalar()


def is_accepted(db: Session, user: User) -> bool:
    app_ = latest(db, user.id)
    return bool(app_ and app_.status == "accepted" and user.verified_at is not None)


def conditions(db: Session, settings: Settings, user: User) -> dict:
    reels, likes = published_stats(db, user.id)
    items = [
        {"key": "verified", "label": "حساب موثّق (النجمة الزرقاء)", "value": 1 if user.verified_at else 0, "target": 1},
        {"key": "reels", "label": "فيديوهات منشورة من الاستوديو", "value": reels, "target": settings.MONETIZE_MIN_REELS},
        {"key": "likes", "label": "إعجابات على فيديوهاتك", "value": likes, "target": settings.MONETIZE_MIN_LIKES},
    ]
    for it in items:
        it["met"] = it["value"] >= it["target"]
    return {"items": items, "met": all(i["met"] for i in items)}


def _app_view(a: MonetizationApplication) -> dict:
    return {"id": a.id, "status": a.status, "status_label": STATUS_AR.get(a.status), "content_type": a.content_type,
            "payout_email": a.payout_email, "admin_note": a.admin_note if a.status in ("rejected", "needs_fix") else None,
            "created_at": iso(a.created_at)}


def overview(db: Session, settings: Settings, user: User) -> dict:
    if user.verified_at is None:
        raise AppError(403, "not_verified", "تحقيق الدخل متاح للحسابات الموثّقة فقط.")
    a = latest(db, user.id)
    return {"enabled": settings.MONETIZE_ENABLED, "conditions": conditions(db, settings, user),
            "account_email": user.email, "application": _app_view(a) if a else None,
            "accepted": bool(a and a.status == "accepted")}


def send_email_code(db: Session, settings: Settings, limiter, user: User, email: object, effects: Effects) -> dict:
    from app.services.messaging import _check_limits
    from app.services.rate_limit import Limit

    addr = email.strip().lower() if isinstance(email, str) else ""
    if not _EMAIL.match(addr):
        raise AppError(400, "invalid_email", "البريد غير صالح.")
    if addr == user.email.lower():
        return {"needed": False}
    _check_limits(limiter, [Limit(f"emailcode:{user.id}", 5, 3600)])
    code = f"{secrets.randbelow(1_000_000):06d}"
    now = clock.utcnow()
    db.add(EmailCode(user_id=user.id, purpose="payout", email=addr, code_hash=_hash(settings, code), created_at=now,
                     expires_at=now + timedelta(seconds=settings.EMAIL_CODE_TTL)))
    effects.later(_mail_code, addr, code)
    return {"needed": True, "sent": True}


def _mail_code(state, email: str, code: str) -> None:
    from app.services import mailer, runtime_config

    with state.database.session() as db:
        settings = runtime_config.effective_settings(db, state.settings)
    try:
        mailer.send_text(settings, email, f"{settings.APP_NAME}: رمز تأكيد بريد الأرباح",
                         f"رمز تأكيد بريد استلام الأرباح: {code}\nصالح لمدة {settings.EMAIL_CODE_TTL // 60} دقيقة.\n"
                         "إذا لم تطلب هذا الرمز فتجاهل الرسالة.")
    except Exception as exc:  # noqa: BLE001
        log.warning("email code not sent: %s", type(exc).__name__)


def _confirm_email(db: Session, settings: Settings, user: User, email: str, code: object) -> None:
    rows = db.execute(select(EmailCode).where(EmailCode.user_id == user.id, EmailCode.email == email,
                                              EmailCode.purpose == "payout", EmailCode.confirmed_at.is_(None),
                                              EmailCode.expires_at > clock.utcnow())
                      .order_by(EmailCode.created_at.desc()).limit(1)).scalars().all()
    rec = rows[0] if rows else None
    if rec is None:
        raise AppError(400, "code_required", "أرسل رمز التأكيد إلى هذا البريد أولًا.")
    if rec.attempts >= 5:
        raise AppError(429, "code_locked", "محاولات كثيرة. اطلب رمزًا جديدًا.")
    if not isinstance(code, str) or not hmac.compare_digest(rec.code_hash, _hash(settings, code.strip())):
        rec.attempts += 1
        db.flush()
        raise AppError(400, "wrong_code", "الرمز غير صحيح.")
    rec.confirmed_at = clock.utcnow()


def apply(db: Session, settings: Settings, user: User, body: dict, effects: Effects) -> dict:
    from app.services.messaging import _require_can_send

    _require_can_send(user)
    if not settings.MONETIZE_ENABLED:
        raise AppError(403, "monetize_off", "طلبات تحقيق الدخل متوقفة حاليًا.")
    if user.verified_at is None:
        raise AppError(403, "not_verified", "تحقيق الدخل متاح للحسابات الموثّقة فقط.")
    last = latest(db, user.id)
    if last is not None and last.status in ("pending", "accepted"):
        raise AppError(409, "application_open", "لديك طلب مقبول أو قيد المراجعة.")
    if not conditions(db, settings, user)["met"]:
        raise AppError(403, "conditions_not_met", "لم تكتمل شروط تحقيق الدخل بعد.")
    if body.get("terms") is not True:
        raise AppError(400, "terms_required", "وافق على شروط تحقيق الدخل.")
    content = clean_message(body.get("content_type"), 200, "reject")
    email = (body.get("payout_email") or user.email).strip().lower() if isinstance(body.get("payout_email") or user.email, str) else ""
    if not _EMAIL.match(email):
        raise AppError(400, "invalid_email", "البريد غير صالح.")
    if email != user.email.lower():
        _confirm_email(db, settings, user, email, body.get("code"))
    now = clock.utcnow()
    if last is not None and last.status == "needs_fix":
        a = last
        a.content_type, a.payout_email, a.status, a.updated_at = content, email, "pending", now
    else:
        a = MonetizationApplication(user_id=user.id, content_type=content, payout_email=email, status="pending",
                                    stats=json.dumps(conditions(db, settings, user)["items"], ensure_ascii=False),
                                    created_at=now, updated_at=now)
        db.add(a)
    db.flush()
    effects.later(notify_admin, a.id)
    return {"application": _app_view(a)}


def decide(db: Session, a: MonetizationApplication, action: str, actor: str, note: str, effects: Effects) -> dict:
    if action not in ("accept", "reject", "fix"):
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    if a.status not in ("pending", "needs_fix"):
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    now = clock.utcnow()
    a.status = {"accept": "accepted", "reject": "rejected", "fix": "needs_fix"}[action]
    a.admin_note = (note or "").strip()[:500] or None
    a.decided_at, a.decided_by, a.updated_at = now, actor[:80], now
    audit.record(db, actor, f"monetize_{action}", target_type="monetization", target_id=a.id, reason=a.admin_note)
    effects.signal(a.user_id, "account")
    db.flush()
    return admin_view(db, a)


def admin_view(db: Session, a: MonetizationApplication) -> dict:
    u = db.get(User, a.user_id)
    return {**_app_view(a), "admin_note": a.admin_note, "public_id": u.public_id if u else None, "user_ref": a.user_id,
            "stats": json.loads(a.stats) if a.stats else [], "decided_by": a.decided_by, "balance": balance(db, a.user_id)}


def admin_list(db: Session, status: str = "") -> dict:
    q = select(MonetizationApplication).order_by(MonetizationApplication.created_at.desc())
    if status in STATUS_AR:
        q = q.where(MonetizationApplication.status == status)
    return {"applications": [admin_view(db, a) for a in db.execute(q.limit(300)).scalars()]}


def notify_admin(state, app_id: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        a = db.get(MonetizationApplication, app_id)
        if a is None:
            return
        v = admin_view(db, a)
    stats = " · ".join(f"{s['label']}: {s['value']}" for s in v["stats"])
    kb = {"inline_keyboard": [[{"text": "✅ قبول", "callback_data": f"mn:accept:{app_id}"},
                               {"text": "❌ رفض", "callback_data": f"mn:reject:{app_id}"}]]}
    try:
        res = bot.tg.send_message(bot.admin_id, f"💰 طلب تحقيق دخل — {v['public_id']}\nالمحتوى: {v['content_type']}\n{stats}",
                                  reply_markup=kb)
        with state.database.session() as db:
            a = db.get(MonetizationApplication, app_id)
            if a is not None and isinstance(res, dict):
                a.tg_message_id = res.get("message_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("monetization notice not sent: %s", type(exc).__name__)


# ---------------------------------------------------------------------------
# ledger (admins write, the user reads)
# ---------------------------------------------------------------------------


def to_minor(amount: object) -> int:
    try:
        value = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise AppError(400, "invalid_amount", "مبلغ غير صالح.") from None
    if not value.is_finite() or value <= 0 or value > Decimal("10000000") or value.as_tuple().exponent < -2:
        raise AppError(400, "invalid_amount", "مبلغ غير صالح (رقم موجب بخانتين عشريتين على الأكثر).")
    return int(value * 100)


def fmt(minor: int) -> str:
    return f"{Decimal(minor) / 100:.2f}"


def balance(db: Session, user_id: str, currency: str | None = None) -> dict[str, str]:
    q = select(LedgerEntry.currency, func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
        LedgerEntry.user_id == user_id).group_by(LedgerEntry.currency)
    out = {cur: fmt(int(total)) for cur, total in db.execute(q).all()}
    return {currency: out.get(currency, "0.00")} if currency else out


def _balance_minor(db: Session, user_id: str, currency: str) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(LedgerEntry.amount_minor), 0)).where(
        LedgerEntry.user_id == user_id, LedgerEntry.currency == currency)) or 0)


def _entry_view(e: LedgerEntry) -> dict:
    return {"id": e.id, "kind": e.kind, "kind_label": KINDS_AR.get(e.kind, e.kind), "amount": fmt(e.amount_minor),
            "currency": e.currency, "note": e.note, "paid_on": e.paid_on, "reverses": e.reverses_id,
            "created_at": iso(e.created_at)}


def _user_of(db: Session, user_ref: str) -> User:
    user = db.get(User, user_ref) if isinstance(user_ref, str) and len(user_ref) <= 32 else None
    if user is None:
        raise not_found()
    return user


def record(db: Session, settings: Settings, user_ref: str, *, kind: str, amount: object, currency: str | None,
           note: object, actor: str, paid_on: object = None, effects: Effects) -> dict:
    """Admin: an earning, an adjustment (+/-) or a payout. Payouts never exceed the balance."""
    user = _user_of(db, user_ref)
    if kind not in ("earning", "adjustment", "payout"):
        raise AppError(400, "invalid_kind", "نوع القيد غير صالح.")
    cur = (currency or settings.EARNINGS_CURRENCY).strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{2,12}", cur):
        raise AppError(400, "invalid_currency", "العملة غير صالحة.")
    negative = isinstance(amount, str) and amount.strip().startswith("-") and kind == "adjustment"
    minor = to_minor(amount.strip().lstrip("-") if isinstance(amount, str) else amount)
    if kind == "payout" or negative:
        minor = -minor
    if not is_accepted(db, user):
        raise AppError(409, "not_monetized", "هذا المستخدم غير مقبول في تحقيق الدخل.")
    if kind == "payout":
        if _balance_minor(db, user.id, cur) + minor < 0:
            raise AppError(409, "insufficient_balance", "المبلغ أكبر من الرصيد.")
        if not isinstance(paid_on, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", paid_on):
            raise AppError(400, "invalid_date", "أدخل تاريخ الإرسال (YYYY-MM-DD).")
    text = note.strip()[:500] if isinstance(note, str) and note.strip() else None
    e = LedgerEntry(user_id=user.id, kind=kind, amount_minor=minor, currency=cur, note=text,
                    paid_on=paid_on if kind == "payout" else None, created_by=actor[:80], created_at=clock.utcnow())
    db.add(e)
    db.flush()
    audit.record(db, actor, f"ledger_{kind}", target_type="user", target_id=user.public_id,
                 detail=f"#{e.id} {fmt(minor)} {cur}", reason=text)
    effects.signal(user.id, "money")
    if kind == "payout":
        effects.later(_mail_payout, user.id, fmt(-minor), cur)
    return _entry_view(e)


def reverse(db: Session, entry_id: int, actor: str, note: object, effects: Effects) -> dict:
    """The only way to correct a mistake: a new entry with the opposite amount (each entry once)."""
    e = db.get(LedgerEntry, entry_id)
    if e is None:
        raise not_found()
    if e.kind == "reversal":
        raise AppError(400, "cannot_reverse", "لا يمكن إلغاء قيد إلغاء.")
    if db.scalar(select(LedgerEntry.id).where(LedgerEntry.reverses_id == e.id)):
        raise AppError(409, "already_reversed", "هذا القيد أُلغي بالفعل.")
    if e.amount_minor > 0 and _balance_minor(db, e.user_id, e.currency) - e.amount_minor < 0:
        raise AppError(409, "insufficient_balance", "الإلغاء سيجعل الرصيد سالبًا.")
    text = note.strip()[:500] if isinstance(note, str) and note.strip() else f"إلغاء القيد #{e.id}"
    r = LedgerEntry(user_id=e.user_id, kind="reversal", amount_minor=-e.amount_minor, currency=e.currency, note=text,
                    reverses_id=e.id, created_by=actor[:80], created_at=clock.utcnow())
    db.add(r)
    db.flush()
    u = db.get(User, e.user_id)
    audit.record(db, actor, "ledger_reversal", target_type="user", target_id=u.public_id if u else None,
                 detail=f"#{r.id} reverses #{e.id} {fmt(-e.amount_minor)} {e.currency}", reason=text)
    effects.signal(e.user_id, "money")
    return _entry_view(r)


def history(db: Session, user_id: str, limit: int = 200) -> list[dict]:
    rows = db.execute(select(LedgerEntry).where(LedgerEntry.user_id == user_id)
                      .order_by(LedgerEntry.created_at.desc(), LedgerEntry.id.desc()).limit(limit)).scalars().all()
    return [_entry_view(e) for e in rows]


def my_money(db: Session, settings: Settings, user: User) -> dict:
    if not is_accepted(db, user):
        raise AppError(403, "not_monetized", "«أموالي» تظهر بعد قبول طلب تحقيق الدخل.")
    reversed_ids = set(db.execute(select(LedgerEntry.reverses_id).where(LedgerEntry.user_id == user.id,
                                                                       LedgerEntry.reverses_id.is_not(None))).scalars())
    earned = sum(e.amount_minor for e in db.execute(select(LedgerEntry).where(
        LedgerEntry.user_id == user.id, LedgerEntry.kind == "earning", LedgerEntry.currency == settings.EARNINGS_CURRENCY)).scalars()
        if e.id not in reversed_ids)
    a = latest(db, user.id)
    return {"currency": settings.EARNINGS_CURRENCY,
            "balance": balance(db, user.id, settings.EARNINGS_CURRENCY)[settings.EARNINGS_CURRENCY],
            "balances": balance(db, user.id), "total_earned": fmt(earned), "payout_email": a.payout_email if a else None,
            "history": history(db, user.id)}


def _mail_payout(state, user_id: str, amount: str, currency: str) -> None:
    from app.services import mailer, runtime_config

    with state.database.session() as db:
        user = db.get(User, user_id)
        a = latest(db, user_id)
        settings = runtime_config.effective_settings(db, state.settings)
        to = (a.payout_email if a else None) or (user.email if user else None)
    if not to:
        return
    try:
        mailer.send_text(settings, to, f"{settings.APP_NAME}: أرباحك", f"مرحبًا،\n\n{PAYOUT_TEXT}\nالمبلغ: {amount} {currency}\n\nفريق {settings.APP_NAME}")
    except Exception as exc:  # noqa: BLE001
        log.warning("payout mail not sent: %s", type(exc).__name__)


def install(state) -> None:
    """Telegram buttons mn:accept|reject:<application id> (admin chat only)."""
    from app.services.media_moderation import mark_done

    def on_callback(bot, cq: dict, payload: str) -> None:
        action, _, aid = payload.partition(":")
        if action not in ("accept", "reject") or not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", aid):
            bot.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        effects = Effects()
        try:
            with state.database.session() as db:
                a = db.get(MonetizationApplication, aid)
                if a is None:
                    bot.tg.answer_callback(cq.get("id"), "غير موجود")
                    return
                decide(db, a, action, f"telegram:{(cq.get('from') or {}).get('id')}", "", effects)
        except AppError as exc:
            bot.tg.answer_callback(cq.get("id"), exc.message)
            return
        state.dispatch(effects)
        label = "✅ قُبل" if action == "accept" else "❌ رُفض"
        bot.tg.answer_callback(cq.get("id"), label)
        msg = cq.get("message") or {}
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"), label)

    if state.bot is not None:
        state.bot.callback_handlers["mn"] = on_callback
