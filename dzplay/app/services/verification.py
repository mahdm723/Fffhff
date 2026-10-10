"""V5 blue star: an official, trusted account — NOT a real-identity verification (no documents).

* Conditions (editable live): VERIFY_MIN_POSTS, VERIFY_MIN_LIKES, VERIFY_MIN_ACCOUNT_AGE_DAYS, and no
  violations. The phone shows progress bars; the request form unlocks when all are met.
* Payment: a one-time crypto payment to the wallet set from the panel (currency, network, address,
  explorer link template). Empty settings = "الدفع غير متاح حاليًا" and no request can be sent.
  The amount is free (optional PAYMENT_MIN_AMOUNT); the TXID is checked per network and is unique.
  Payments are verified by a human (explorer link) — nothing is automated or custodial.
* User.verified_at is written only here (grant / revoke), from admin endpoints or the admin's Telegram
  buttons. No user-facing endpoint accepts it (mass-assignment tests).
* The star shows on the profile, ideas, search and direct chats — never in anonymous chats.
* V6: new requests are closed (VERIFY_ENABLED=False); the star moves to memberships (V6 phase 5).
  Existing stars are kept, pending requests can still be decided from the panel.
"""

from __future__ import annotations

import json
import logging
import re
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import AppSetting, MediaItem, Post, Report, User, VerificationRequest
from app.services import audit
from app.services.content import clean_message
from app.services.messaging import Effects, iso

log = logging.getLogger("dzplay.verification")

ACCOUNT_TYPES = {"writer": "كاتب", "creator": "صانع محتوى", "page": "صفحة", "other": "أخرى"}
STATUS_AR = {"pending": "قيد المراجعة", "accepted": "مقبول", "rejected": "مرفوض", "needs_fix": "يحتاج تصحيحًا"}

# Networks the admin can choose. `tx` validates a transaction id; `explorer` is the default link template.
NETWORKS: dict[str, dict] = {
    "TRC20": {"label": "TRON (TRC20)", "tx": r"^[0-9a-fA-F]{64}$", "explorer": "https://tronscan.org/#/transaction/{txid}"},
    "BEP20": {"label": "BNB Smart Chain (BEP20)", "tx": r"^0x[0-9a-fA-F]{64}$", "explorer": "https://bscscan.com/tx/{txid}"},
    "ERC20": {"label": "Ethereum (ERC20)", "tx": r"^0x[0-9a-fA-F]{64}$", "explorer": "https://etherscan.io/tx/{txid}"},
    "POLYGON": {"label": "Polygon", "tx": r"^0x[0-9a-fA-F]{64}$", "explorer": "https://polygonscan.com/tx/{txid}"},
    "ARBITRUM": {"label": "Arbitrum One", "tx": r"^0x[0-9a-fA-F]{64}$", "explorer": "https://arbiscan.io/tx/{txid}"},
    "SOL": {"label": "Solana", "tx": r"^[1-9A-HJ-NP-Za-km-z]{80,90}$", "explorer": "https://solscan.io/tx/{txid}"},
    "TON": {"label": "TON", "tx": r"^(?:[0-9a-fA-F]{64}|[A-Za-z0-9_\-+/]{43}=?)$", "explorer": "https://tonviewer.com/transaction/{txid}"},
    "BTC": {"label": "Bitcoin", "tx": r"^[0-9a-fA-F]{64}$", "explorer": "https://mempool.space/tx/{txid}"},
    "OTHER": {"label": "أخرى", "tx": r"^[A-Za-z0-9:_\-+/=]{16,150}$", "explorer": ""},
}
_PAY_KEYS = ("pay.currency", "pay.network", "pay.wallet", "pay.explorer", "pay.note")
_WALLET = re.compile(r"^[A-Za-z0-9:_\-.]{10,128}$")
_CURRENCY = re.compile(r"^[A-Za-z0-9]{2,12}$")


# ---------------------------------------------------------------------------
# payment settings (panel)
# ---------------------------------------------------------------------------


def payment_settings(db: Session) -> dict:
    rows = {r.key: r.value for r in db.execute(select(AppSetting).where(AppSetting.key.in_(_PAY_KEYS))).scalars()}
    net = rows.get("pay.network") or ""
    cfg = {"currency": rows.get("pay.currency") or "", "network": net,
           "network_label": NETWORKS.get(net, {}).get("label", net), "wallet": rows.get("pay.wallet") or "",
           "explorer": rows.get("pay.explorer") or NETWORKS.get(net, {}).get("explorer", ""), "note": rows.get("pay.note") or ""}
    cfg["available"] = bool(cfg["currency"] and net in NETWORKS and cfg["wallet"])
    return cfg


def save_payment_settings(db: Session, body: dict, actor: str) -> dict:
    currency = str(body.get("currency") or "").strip().upper()
    network = str(body.get("network") or "").strip().upper()
    wallet = str(body.get("wallet") or "").strip()
    explorer = str(body.get("explorer") or "").strip()
    note = str(body.get("note") or "").strip()
    if currency or network or wallet:  # all empty = payments off
        if not _CURRENCY.match(currency):
            raise AppError(400, "invalid_currency", "العملة غير صالحة (مثال: USDT).")
        if network not in NETWORKS:
            raise AppError(400, "invalid_network", "اختر الشبكة من القائمة.")
        if not _WALLET.match(wallet):
            raise AppError(400, "invalid_wallet", "عنوان المحفظة غير صالح.")
    if explorer and (not explorer.startswith("https://") or "{txid}" not in explorer or len(explorer) > 200):
        raise AppError(400, "invalid_explorer", "رابط المستكشف يجب أن يبدأ بـ https:// ويحتوي {txid}.")
    if len(note) > 300:
        raise AppError(400, "invalid_note", "الملاحظة طويلة جدًا.")
    now = clock.utcnow()
    for key, value in zip(_PAY_KEYS, (currency, network, wallet, explorer, note)):
        row = db.get(AppSetting, key)
        if row is None:
            db.add(AppSetting(key=key, value=value, updated_at=now, updated_by=actor))
        else:
            row.value, row.updated_at, row.updated_by = value, now, actor
    audit.record(db, actor, "payment_settings", detail=f"{currency} {network} {wallet[:6]}…{wallet[-4:]}" if wallet else "off")
    db.flush()
    return payment_settings(db)


def qr_svg(text: str) -> str:
    """The wallet address as an SVG QR code (data: URI; CSP allows data: images)."""
    import base64
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=8, border=2)
    buf = io.BytesIO()
    img.save(buf)
    return "data:image/svg+xml;base64," + base64.b64encode(buf.getvalue()).decode()


def explorer_link(template: str, txid: str) -> str | None:
    from urllib.parse import quote

    # the TXID is format-checked per network; quoting it as well keeps it inside the template's path
    return template.replace("{txid}", quote(txid, safe="")) if template and "{txid}" in template else None


# ---------------------------------------------------------------------------
# conditions
# ---------------------------------------------------------------------------


def violations(db: Session, user: User) -> int:
    reports = db.scalar(select(func.count()).select_from(Report).where(
        Report.reported_user_id == user.id, Report.status == "resolved", Report.resolution.is_not(None),
        Report.resolution.notin_(("dismiss", "keep")))) or 0
    media = db.scalar(select(func.count()).select_from(MediaItem).where(
        MediaItem.owner_id == user.id, MediaItem.state == "removed",
        (MediaItem.removed_by.like("telegram:%")) | (MediaItem.removed_by.like("admin:%")))) or 0
    removed_posts = db.scalar(select(func.count()).select_from(Post).where(
        Post.author_id == user.id, Post.status == "removed")) or 0
    return int(reports) + int(media) + int(removed_posts)


def conditions(db: Session, settings: Settings, user: User) -> dict:
    posts = db.scalar(select(func.count()).select_from(Post).where(Post.author_id == user.id, Post.status == "visible")) or 0
    likes = db.scalar(select(func.coalesce(func.sum(Post.likes_count), 0)).where(
        Post.author_id == user.id, Post.status == "visible")) or 0
    age_days = max(0, (clock.utcnow() - user.created_at).days)
    bad = violations(db, user)
    items = [
        {"key": "posts", "label": "أفكار منشورة", "value": int(posts), "target": settings.VERIFY_MIN_POSTS},
        {"key": "likes", "label": "إعجابات على محتواك", "value": int(likes), "target": settings.VERIFY_MIN_LIKES},
        {"key": "age", "label": "عمر الحساب (يوم)", "value": int(age_days), "target": settings.VERIFY_MIN_ACCOUNT_AGE_DAYS},
        {"key": "clean", "label": "بدون مخالفات", "value": 0 if bad else 1, "target": 1},
    ]
    for it in items:
        it["met"] = it["value"] >= it["target"]
    return {"items": items, "met": all(it["met"] for it in items) and user.status == "active"}


# ---------------------------------------------------------------------------
# user side
# ---------------------------------------------------------------------------


def _request_view(r: VerificationRequest) -> dict:
    return {"id": r.id, "status": r.status, "status_label": STATUS_AR.get(r.status, r.status),
            "account_type": r.account_type, "description": r.description, "reason": r.reason,
            "amount": r.amount, "currency": r.currency, "network": r.network, "txid": r.txid,
            "admin_note": r.admin_note if r.status in ("rejected", "needs_fix") else None,
            "created_at": iso(r.created_at), "updated_at": iso(r.updated_at)}


def latest(db: Session, user_id: str) -> VerificationRequest | None:
    return db.execute(select(VerificationRequest).where(VerificationRequest.user_id == user_id)
                      .order_by(VerificationRequest.created_at.desc()).limit(1)).scalar()


def overview(db: Session, settings: Settings, user: User) -> dict:
    pay = payment_settings(db)
    last = latest(db, user.id)
    warning = (f"أرسل {pay['currency']} عبر شبكة {pay['network_label']} فقط. الإرسال عبر شبكة أخرى أو بعملة أخرى "
               "يضيع ولا يمكن استرجاعه، والمسؤولية على المرسل.") if pay["available"] else None
    return {
        "enabled": settings.VERIFY_ENABLED,
        "verified": user.verified_at is not None,
        "verified_at": iso(user.verified_at),
        "conditions": conditions(db, settings, user),
        "payment": {"available": pay["available"], "currency": pay["currency"], "network": pay["network"],
                    "network_label": pay["network_label"], "wallet": pay["wallet"] if pay["available"] else None,
                    "qr": qr_svg(pay["wallet"]) if pay["available"] else None, "note": pay["note"] or None,
                    "min_amount": settings.PAYMENT_MIN_AMOUNT or None, "warning": warning},
        "account_types": ACCOUNT_TYPES,
        "request": _request_view(last) if last else None,
    }


def _clean_amount(settings: Settings, amount: object) -> str:
    try:
        value = Decimal(str(amount).strip().replace(",", "."))
    except (InvalidOperation, ValueError):
        raise AppError(400, "invalid_amount", "أدخل المبلغ الذي أرسلته (رقم).") from None
    if not value.is_finite() or value <= 0 or value > Decimal("1000000") or value.as_tuple().exponent < -8:
        raise AppError(400, "invalid_amount", "أدخل المبلغ الذي أرسلته (رقم).")
    if settings.PAYMENT_MIN_AMOUNT and value < Decimal(str(settings.PAYMENT_MIN_AMOUNT)):
        raise AppError(400, "amount_too_low", f"أقل مبلغ مقبول هو {settings.PAYMENT_MIN_AMOUNT:g}.")
    return format(value.normalize(), "f")


def _clean_txid(network: str, txid: object) -> str:
    tx = txid.strip() if isinstance(txid, str) else ""
    rule = NETWORKS.get(network, NETWORKS["OTHER"])["tx"]
    if not re.match(rule, tx):
        raise AppError(400, "invalid_txid", f"رقم العملية (TXID) لا يطابق شكل شبكة {NETWORKS.get(network, {}).get('label', network)}.")
    return tx


def _form(settings: Settings, body: dict) -> dict:
    account_type = body.get("account_type")
    if account_type not in ACCOUNT_TYPES:
        raise AppError(400, "invalid_type", "اختر نوع الحساب.")
    description = clean_message(body.get("description"), 1000, "reject")
    reason = clean_message(body.get("reason"), 1000, "reject")
    return {"account_type": account_type, "description": description, "reason": reason,
            "amount": _clean_amount(settings, body.get("amount"))}


def submit(db: Session, settings: Settings, limiter, user: User, body: dict, effects: Effects) -> dict:
    from app.services.messaging import _check_limits, _require_can_send
    from app.services.rate_limit import Limit

    _require_can_send(user)
    if not settings.VERIFY_ENABLED:
        raise AppError(403, "verify_off", "طلبات النجمة الزرقاء متوقفة: ستصبح النجمة جزءًا من العضوية قريبًا.")
    if user.verified_at is not None:
        raise AppError(409, "already_verified", "حسابك موثّق بالفعل.")
    last = latest(db, user.id)
    if last is not None and last.status in ("pending", "needs_fix"):
        raise AppError(409, "request_open", "لديك طلب قيد المراجعة بالفعل.")
    if not conditions(db, settings, user)["met"]:
        raise AppError(403, "conditions_not_met", "لم تكتمل شروط التوثيق بعد.")
    pay = payment_settings(db)
    if not pay["available"]:
        raise AppError(503, "payment_unavailable", "الدفع غير متاح حاليًا.")
    _check_limits(limiter, [Limit(f"verify:{user.id}", 5, 3600)])
    form = _form(settings, body)
    txid = _clean_txid(pay["network"], body.get("txid"))
    now = clock.utcnow()
    req = VerificationRequest(user_id=user.id, **form, currency=pay["currency"], network=pay["network"],
                              wallet=pay["wallet"], txid=txid, status="pending",
                              stats=json.dumps(conditions(db, settings, user)["items"], ensure_ascii=False),
                              created_at=now, updated_at=now)
    try:
        with db.begin_nested():
            db.add(req)
    except IntegrityError:
        raise AppError(409, "txid_used", "رقم العملية هذا مستخدم في طلب آخر.") from None
    effects.later(notify_admin, req.id)
    return {"request": _request_view(req)}


def resubmit(db: Session, settings: Settings, user: User, request_id: object, body: dict, effects: Effects) -> dict:
    req = db.get(VerificationRequest, request_id) if isinstance(request_id, str) and len(request_id) <= 32 else None
    if req is None or req.user_id != user.id:
        raise not_found()
    if req.status != "needs_fix":
        raise AppError(409, "not_editable", "لا يمكن تعديل هذا الطلب الآن.")
    form = _form(settings, body)
    txid = _clean_txid(req.network, body.get("txid"))
    for k, v in form.items():
        setattr(req, k, v)
    if db.scalar(select(VerificationRequest.id).where(VerificationRequest.txid == txid, VerificationRequest.id != req.id)):
        raise AppError(409, "txid_used", "رقم العملية هذا مستخدم في طلب آخر.")
    req.txid, req.status, req.updated_at = txid, "pending", clock.utcnow()
    db.flush()
    effects.later(notify_admin, req.id)
    return {"request": _request_view(req)}


# ---------------------------------------------------------------------------
# admin side (panel + Telegram buttons): the ONLY writers of User.verified_at
# ---------------------------------------------------------------------------


def grant(db: Session, user: User, actor: str, effects: Effects | None = None) -> None:
    user.verified_at, user.verified_by = clock.utcnow(), actor[:80]
    audit.record(db, actor, "verify_grant", target_type="user", target_id=user.public_id)
    if effects is not None:
        effects.signal(user.id, "account")


def revoke(db: Session, user: User, actor: str, reason: str = "", effects: Effects | None = None) -> None:
    user.verified_at, user.verified_by = None, None
    audit.record(db, actor, "verify_revoke", target_type="user", target_id=user.public_id, reason=reason[:255] or None)
    if effects is not None:
        effects.signal(user.id, "account")


def decide(db: Session, req: VerificationRequest, action: str, actor: str, note: str, effects: Effects) -> dict:
    if action not in ("accept", "reject", "fix"):
        raise AppError(400, "invalid_action", "إجراء غير صالح.")
    if req.status not in ("pending", "needs_fix"):
        raise AppError(409, "already_decided", "تم البت في هذا الطلب.")
    note = (note or "").strip()[:500]
    if action in ("reject", "fix") and not note:
        note = "لم نتمكن من تأكيد الدفع." if action == "reject" else "تحقق من رقم العملية والمبلغ ثم أعد الإرسال."
    user = db.get(User, req.user_id)
    now = clock.utcnow()
    req.status = {"accept": "accepted", "reject": "rejected", "fix": "needs_fix"}[action]
    req.admin_note = note or None
    req.decided_at, req.decided_by, req.updated_at = now, actor[:80], now
    if action == "accept" and user is not None:
        grant(db, user, actor, effects)
    audit.record(db, actor, f"verify_{action}", target_type="verification", target_id=req.id,
                 detail=f"{req.amount} {req.currency} {req.network}", reason=note or None)
    if user is not None:
        effects.signal(user.id, "account")
        effects.later(notify_user, req.id)
    db.flush()
    return admin_view(db, req)


def admin_view(db: Session, req: VerificationRequest) -> dict:
    user = db.get(User, req.user_id)
    pay = payment_settings(db)
    template = pay["explorer"] if pay["network"] == req.network else NETWORKS.get(req.network, {}).get("explorer", "")
    return {**_request_view(req), "admin_note": req.admin_note, "public_id": user.public_id if user else None,
            "user_ref": req.user_id, "name": user.display_name if user else None, "wallet": req.wallet,
            "stats": json.loads(req.stats) if req.stats else [], "explorer_url": explorer_link(template, req.txid),
            "decided_by": req.decided_by, "decided_at": iso(req.decided_at),
            "verified": bool(user and user.verified_at)}


def admin_list(db: Session, status: str = "", limit: int = 200) -> dict:
    stmt = select(VerificationRequest).order_by(VerificationRequest.created_at.desc())
    if status in STATUS_AR:
        stmt = stmt.where(VerificationRequest.status == status)
    rows = db.execute(stmt.limit(min(limit, 500))).scalars().all()
    return {"requests": [admin_view(db, r) for r in rows],
            "counts": {s: db.scalar(select(func.count()).select_from(VerificationRequest)
                                    .where(VerificationRequest.status == s)) or 0 for s in STATUS_AR},
            "networks": {k: v["label"] for k, v in NETWORKS.items()}}


# ---------------------------------------------------------------------------
# notifications (background)
# ---------------------------------------------------------------------------


def notify_admin(state, request_id: str) -> None:
    bot = state.bot
    if bot is None:
        return
    with state.database.session() as db:
        req = db.get(VerificationRequest, request_id)
        if req is None:
            return
        v = admin_view(db, req)
    stats = " · ".join(f"{s['label']}: {s['value']}" for s in v["stats"] if s["key"] != "clean")
    text = (f"⭐ طلب توثيق — {v['public_id']}\nالنوع: {ACCOUNT_TYPES.get(v['account_type'])}\n"
            f"المبلغ: {v['amount']} {v['currency']} ({v['network']})\nTXID: {v['txid']}\n"
            f"{v['explorer_url'] or ''}\n{stats}\n\n{v['description'][:400]}")
    kb = {"inline_keyboard": [[{"text": "✅ قبول", "callback_data": f"vf:accept:{request_id}"},
                               {"text": "❌ رفض", "callback_data": f"vf:reject:{request_id}"}],
                              [{"text": "✏️ يحتاج تصحيحًا", "callback_data": f"vf:fix:{request_id}"}]]}
    try:
        res = bot.tg.send_message(bot.admin_id, text, reply_markup=kb)
        with state.database.session() as db:
            req = db.get(VerificationRequest, request_id)
            if req is not None and isinstance(res, dict):
                req.tg_message_id = res.get("message_id")
    except Exception as exc:  # noqa: BLE001
        log.warning("verification notice not sent: %s", type(exc).__name__)


def notify_user(state, request_id: str) -> None:
    from app.services import mail

    with state.database.session() as db:
        req = db.get(VerificationRequest, request_id)
        user = db.get(User, req.user_id) if req else None
        if req is None or user is None or not user.email:
            return
        status, note = req.status, req.admin_note
        text = {"accepted": "تمت الموافقة على طلب التوثيق. تظهر الآن النجمة الزرقاء بجانب اسمك.",
                "rejected": f"رُفض طلب التوثيق. السبب: {note or '—'}",
                "needs_fix": f"طلب التوثيق يحتاج تصحيحًا: {note or '—'}\nادخل إلى التطبيق ← حسابي ← التوثيق لتعديله."}.get(status)
        out = mail.prepare(db, state.settings, "system", user.email, "email.notice",
                           {"topic": "طلب التوثيق", "message": text}) if text else None
    if out is None:
        return
    try:
        mail.deliver(out)
    except Exception as exc:  # noqa: BLE001
        log.info("verification mail not sent: %s", type(exc).__name__)


def install(state) -> None:
    """Telegram buttons vf:accept|reject|fix:<request id> (admin chat only)."""
    from app.services.media_moderation import mark_done

    def on_callback(bot, cq: dict, payload: str) -> None:
        action, _, rid = payload.partition(":")
        if action not in ("accept", "reject", "fix") or not re.fullmatch(r"[A-Za-z0-9_-]{8,32}", rid):
            bot.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        effects = Effects()
        try:
            with state.database.session() as db:
                req = db.get(VerificationRequest, rid)
                if req is None:
                    bot.tg.answer_callback(cq.get("id"), "غير موجود")
                    return
                decide(db, req, action, f"telegram:{(cq.get('from') or {}).get('id')}", "", effects)
        except AppError as exc:
            bot.tg.answer_callback(cq.get("id"), exc.message)
            return
        state.dispatch(effects)
        label = {"accept": "✅ قُبل", "reject": "❌ رُفض", "fix": "✏️ طُلب تصحيح"}[action]
        bot.tg.answer_callback(cq.get("id"), label)
        msg = cq.get("message") or {}
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"), label)

    if state.bot is not None:
        state.bot.callback_handlers["vf"] = on_callback


def is_verified(user: User | None) -> bool:
    return bool(user is not None and user.verified_at is not None)

