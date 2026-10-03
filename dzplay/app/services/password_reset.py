"""Account recovery ("نسيت كلمة السر") with an admin in the loop.

1. The user enters an e-mail (+ anti-bot). The answer is ALWAYS the same
   ("if this e-mail is registered you will receive a code"), whether the
   account exists, is Google-only, or is limited — no account enumeration.
2. If a password account exists, the admin's Telegram bot gets the e-mail, a
   short request id and buttons. The admin types `/code <id> <code>` or taps
   "generate" (the server makes a random code).
3. The server e-mails the code (SMTP) and tells the admin whether it worked.
4. The user types the code → a one-time token (RESET_TOKEN_TTL) → new password
   (+ confirmation + anti-bot) → every old session is revoked and the user is
   signed in.

Rules: codes stored only as argon2 hashes; valid RESET_CODE_TTL, single use;
a new request cancels older ones; RESET_MAX_CODE_ATTEMPTS wrong codes cancel
the code; RESET_MAX_REQUESTS per network → RESET_IP_BLOCK_DURATION block
(trusted IPs exempt, like the login protection); RESET_MAX_PER_EMAIL per
e-mail (silently). Codes, tokens and passwords are never logged.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
from datetime import timedelta

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.models import PasswordReset, SecurityEvent, User
from app.security.crypto import keyed_hash, new_token, sha256_hex
from app.security.passwords import hash_password, verify_password
from app.security.sessions import revoke_all_sessions
from app.services import audit, runtime_config
from app.services.auth import ClientContext, _check_antibot, log_event, normalize_email, validate_new_password
from app.services.mailer import MailError, send_reset_code
from app.services.rate_limit import Limit

log = logging.getLogger("dzplay.reset")

GENERIC = "إن كان هذا البريد مسجّلًا، سيصلك رمز الاستعادة على بريدك بعد مراجعة الطلب."
BAD_CODE = "الرمز غير صحيح أو منتهي الصلاحية."
_ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
_CODE_RE = re.compile(r"^[A-Za-z0-9]{4,12}$")
_DUMMY = hash_password("dzplay-dummy-reset-code")
ACTIVE = ("pending", "sent")


def _normalize_code(code: str) -> str:
    return code.strip().replace(" ", "").replace("-", "").upper()


def _new_short_id(db: Session) -> str:
    while True:
        sid = "".join(secrets.choice(_ID_ALPHABET) for _ in range(6))
        if not db.scalar(select(PasswordReset.id).where(PasswordReset.short_id == sid)):
            return sid


# ---------------------------------------------------------------------------
# 1. request
# ---------------------------------------------------------------------------


def _ip_block_seconds(db: Session, settings: Settings, ctx: ClientContext) -> int:
    if ctx.trusted:
        return 0
    since = clock.utcnow() - timedelta(seconds=max(settings.RESET_IP_WINDOW, settings.RESET_IP_BLOCK_DURATION))
    times = db.execute(select(SecurityEvent.created_at).where(
        SecurityEvent.type == "reset_request", SecurityEvent.ip_hash == ctx.ip_hash, SecurityEvent.created_at > since)
        .order_by(SecurityEvent.created_at.desc())).scalars().all()
    window_start = clock.utcnow() - timedelta(seconds=settings.RESET_IP_WINDOW)
    recent = [t for t in times if t > window_start]
    if len(recent) < settings.RESET_MAX_REQUESTS:
        return 0
    # Blocked for RESET_IP_BLOCK_DURATION counted from the request that reached the limit.
    reached = recent[settings.RESET_MAX_REQUESTS - 1]
    wait = (reached + timedelta(seconds=settings.RESET_IP_BLOCK_DURATION) - clock.utcnow()).total_seconds()
    return max(0, math.ceil(wait))


def request_reset(db: Session, settings: Settings, ctx: ClientContext, *, email: object,
                  antibot_payload: dict | None) -> dict:
    """Returns {"message"} for the user and, when the admin must be asked, {"notify": reset_id}."""
    try:
        email_n = normalize_email(email)
    except AppError:
        raise AppError(400, "invalid_email", "البريد الإلكتروني غير صالح.") from None
    wait = _ip_block_seconds(db, settings, ctx)
    if wait:
        log_event(db, "reset_blocked_ip", ctx)
        db.commit()
        raise AppError(429, "rate_limited", "طلبات استعادة كثيرة من هذه الشبكة. حاول لاحقًا.", wait)
    _check_antibot(db, settings, ctx, "reset", antibot_payload)

    email_ref = keyed_hash(settings.SECRET_KEY, "reset-email", email_n)[:32]
    since = clock.utcnow() - timedelta(seconds=settings.RESET_EMAIL_WINDOW)
    per_email = len(db.execute(select(SecurityEvent.id).where(
        SecurityEvent.type == "reset_request", SecurityEvent.detail == email_ref, SecurityEvent.created_at > since)).all())
    log_event(db, "reset_request", ctx, None, email_ref)
    result: dict = {"message": GENERIC}
    if per_email >= settings.RESET_MAX_PER_EMAIL:
        return result  # same answer; the admin is not bothered again

    user = db.scalar(select(User).where(User.email == email_n))
    if user is None or not user.password_hash or user.status == "banned" or user.is_official or user.is_system:
        return result  # Google-only / unknown / banned: same answer, nothing sent

    now = clock.utcnow()
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.status.in_(ACTIVE + ("verified",)))
               .values(status="cancelled"))
    reset = PasswordReset(short_id=_new_short_id(db), user_id=user.id, status="pending", ip_hash=ctx.ip_hash,
                          created_at=now, expires_at=now + timedelta(seconds=settings.RESET_CODE_TTL))
    db.add(reset)
    db.flush()
    result["notify"] = reset.id
    return result


# ---------------------------------------------------------------------------
# 2-3. admin (Telegram) sets / generates the code, server e-mails it
# ---------------------------------------------------------------------------


def admin_prompt(db: Session, reset_id: str, manual: bool = False) -> tuple[str, dict] | None:
    r = db.get(PasswordReset, reset_id)
    user = db.get(User, r.user_id) if r else None
    if r is None or user is None:
        return None
    how = ("اضغط «توليد رمز» فيظهر لك الرمز هنا لترسله أنت إلى هذا البريد (البريد التلقائي غير مُعدّ)"
           if manual else "اضغط «توليد رمز» ليُرسَل رمز عشوائي إلى البريد")
    text = (f"🔑 طلب استعادة حساب\n"
            f"البريد: {user.email}\n"
            f"رقم الطلب: {r.short_id}\n\n"
            f"{how}، أو اكتب:\n/code {r.short_id} <الرمز>")
    keyboard = {"inline_keyboard": [[{"text": "🎲 توليد رمز" if manual else "🎲 توليد رمز وإرساله",
                                      "callback_data": f"rgen:{r.short_id}"}],
                                    [{"text": "✖️ رفض الطلب", "callback_data": f"rdeny:{r.short_id}"}]]}
    return text, keyboard


def _find_active(db: Session, short_id: str) -> PasswordReset | None:
    sid = (short_id or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{4,12}", sid):
        return None
    return db.scalar(select(PasswordReset).where(PasswordReset.short_id == sid))


def set_code_and_send(database, settings: Settings, short_id: str, code: str | None) -> str:
    """Store the code (hashed), e-mail it, and return a reply for the admin. `code=None` → generate."""
    if code is None:
        code = "".join(secrets.choice("0123456789") for _ in range(settings.RESET_CODE_LENGTH))
    else:
        code = _normalize_code(code)
        if not _CODE_RE.match(code) or len(code) < settings.RESET_CODE_LENGTH:
            return f"❌ الرمز يجب أن يكون {settings.RESET_CODE_LENGTH} إلى 12 حرفًا أو رقمًا."
    with database.session() as db:
        r = _find_active(db, short_id)
        if r is None:
            return "❌ لا يوجد طلب بهذا الرقم."
        if r.status not in ACTIVE or r.expires_at <= clock.utcnow():
            return f"❌ الطلب {r.short_id} لم يعد صالحًا (منتهٍ أو مُلغى أو مستخدم)."
        user = db.get(User, r.user_id)
        r.code_hash = hash_password(code)
        r.code_set_at = clock.utcnow()
        r.attempts = 0
        r.expires_at = r.code_set_at + timedelta(seconds=settings.RESET_CODE_TTL)
        email, sid = user.email, r.short_id
        audit.record(db, "telegram", "reset_code_set", target_type="reset", target_id=sid)
        eff = runtime_config.effective_settings(db, settings)  # SMTP set in the admin panel applies live
    if not eff.smtp_enabled:
        # Manual mode (no e-mail server configured): the admin sends the code to the user themselves.
        with database.session() as db:
            r = _find_active(db, sid)
            if r is not None and r.status in ACTIVE:
                r.status = "sent"
            audit.record(db, "telegram", "reset_code_manual", target_type="reset", target_id=sid)
        hours = max(1, settings.RESET_CODE_TTL // 3600)
        return (f"📋 أرسل هذا الرمز بنفسك إلى صاحب الطلب {sid}:\n"
                f"البريد: {email}\n"
                f"الرمز: {code}\n\n"
                f"صالح {hours} ساعة ولمرة واحدة. يكتبه في التطبيق ثم يختار كلمة مرور جديدة.\n"
                "(لإرسال الرمز تلقائيًا: لوحة التحكم ← الأمان والنظام ← البريد)")
    try:
        send_reset_code(eff, email, code)
    except MailError as exc:
        log.warning("reset e-mail failed for request %s: %s", sid, exc)
        with database.session() as db:
            audit.record(db, "telegram", "reset_mail_failed", target_type="reset", target_id=sid, detail=str(exc))
        return f"❌ تعذّر إرسال البريد للطلب {sid} ({exc}). تحقق من إعدادات SMTP ثم أعد المحاولة."
    with database.session() as db:
        r = _find_active(db, sid)
        if r is not None and r.status in ACTIVE:
            r.status = "sent"
        audit.record(db, "telegram", "reset_mail_sent", target_type="reset", target_id=sid)
    return f"✅ أُرسل رمز الاستعادة إلى بريد صاحب الطلب {sid}."


def deny(database, short_id: str) -> str:
    with database.session() as db:
        r = _find_active(db, short_id)
        if r is None or r.status not in ACTIVE:
            return "لا يوجد طلب نشط بهذا الرقم."
        r.status = "cancelled"
        audit.record(db, "telegram", "reset_denied", target_type="reset", target_id=r.short_id)
        return f"✖️ رُفض الطلب {r.short_id}."


# ---------------------------------------------------------------------------
# 4. user: verify the code, then choose the new password
# ---------------------------------------------------------------------------


def verify_code(db: Session, settings: Settings, limiter, ctx: ClientContext, *, email: object, code: object) -> dict:
    if not ctx.trusted:
        decision = limiter.check_and_hit([Limit(f"reset_verify:{ctx.ip_hash}", settings.RESET_VERIFY_PER_IP_PER_HOUR, 3600)])
        if not decision.allowed:
            raise AppError(429, "rate_limited", "محاولات كثيرة. حاول لاحقًا.", math.ceil(decision.retry_after))
    try:
        email_n = normalize_email(email)
    except AppError:
        raise AppError(400, "invalid_code", BAD_CODE) from None
    code_n = _normalize_code(code) if isinstance(code, str) else ""
    user = db.scalar(select(User).where(User.email == email_n))
    now = clock.utcnow()
    r = None
    if user is not None:
        r = db.scalar(select(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.status == "sent",
                                                  PasswordReset.expires_at > now).order_by(PasswordReset.created_at.desc()))
    ok = verify_password(r.code_hash if r is not None else _DUMMY, code_n) if _CODE_RE.match(code_n or "-") else False
    if r is None or not ok:
        if r is not None:
            r.attempts += 1
            if r.attempts >= settings.RESET_MAX_CODE_ATTEMPTS:
                r.status = "cancelled"
                log_event(db, "reset_code_locked", ctx, r.user_id)
        log_event(db, "reset_code_failed", ctx, user.id if user else None)
        db.commit()
        raise AppError(400, "invalid_code", BAD_CODE)
    token = new_token()
    r.status = "verified"  # the code cannot be used again
    r.token_hash = sha256_hex(token)
    r.token_expires_at = now + timedelta(seconds=settings.RESET_TOKEN_TTL)
    log_event(db, "reset_code_ok", ctx, r.user_id)
    return {"reset_token": token, "expires_in": settings.RESET_TOKEN_TTL}


def complete(db: Session, settings: Settings, ctx: ClientContext, *, reset_token: object, password: object,
             password_confirm: object, antibot_payload: dict | None) -> User:
    if not isinstance(reset_token, str) or not (20 <= len(reset_token) <= 128):
        raise AppError(400, "reset_expired", "انتهت صلاحية الطلب. اطلب رمزًا جديدًا.")
    r = db.scalar(select(PasswordReset).where(PasswordReset.token_hash == sha256_hex(reset_token)))
    now = clock.utcnow()
    if r is None or r.status != "verified" or not r.token_expires_at or r.token_expires_at <= now:
        raise AppError(400, "reset_expired", "انتهت صلاحية الطلب. اطلب رمزًا جديدًا.")
    user = db.get(User, r.user_id)
    if user is None or user.status == "banned":
        raise AppError(400, "reset_expired", "انتهت صلاحية الطلب. اطلب رمزًا جديدًا.")
    pw = validate_new_password(settings, password, password_confirm, user.email)
    _check_antibot(db, settings, ctx, "reset", antibot_payload)
    user.password_hash = hash_password(pw)
    r.status = "used"
    r.token_hash = None
    db.execute(update(PasswordReset).where(PasswordReset.user_id == user.id, PasswordReset.id != r.id,
                                           PasswordReset.status.in_(ACTIVE + ("verified",))).values(status="cancelled"))
    revoke_all_sessions(db, user.id)  # every device signed in before is signed out
    log_event(db, "password_reset", ctx, user.id)
    return user


def purge(db: Session) -> int:
    cutoff = clock.utcnow() - timedelta(days=7)
    from sqlalchemy import delete

    return db.execute(delete(PasswordReset).where(PasswordReset.expires_at < cutoff)).rowcount or 0


# ---------------------------------------------------------------------------
# Telegram glue
# ---------------------------------------------------------------------------


def install(bot) -> None:
    """Register /code and the inline buttons on the bot."""
    database, settings = bot.database, bot.settings

    def on_code(b, short_id: str, code: str) -> None:
        if not short_id or not code:
            b.say("الاستعمال: /code <رقم الطلب> <الرمز>")
            return
        b.say(set_code_and_send(database, settings, short_id, code))

    def on_generate(b, cq: dict, short_id: str) -> None:
        b.tg.answer_callback(cq.get("id"), "جارٍ الإرسال…")
        msg = cq.get("message") or {}
        try:
            b.tg.edit_reply_markup(msg.get("chat", {}).get("id"), msg.get("message_id"))
        except Exception:  # noqa: BLE001 - buttons are cosmetic
            pass
        b.say(set_code_and_send(database, settings, short_id, None))

    def on_deny(b, cq: dict, short_id: str) -> None:
        b.tg.answer_callback(cq.get("id"), "تم")
        msg = cq.get("message") or {}
        try:
            b.tg.edit_reply_markup(msg.get("chat", {}).get("id"), msg.get("message_id"))
        except Exception:  # noqa: BLE001
            pass
        b.say(deny(database, short_id))

    bot.reset_handler = on_code
    bot.callback_handlers["rgen"] = on_generate
    bot.callback_handlers["rdeny"] = on_deny


def notify_admin(bot, reset_id: str) -> None:
    """Background: send the request to the admin's chat (never blocks the HTTP answer)."""
    with bot.database.session() as db:
        manual = not runtime_config.effective_settings(db, bot.settings).smtp_enabled
        prompt = admin_prompt(db, reset_id, manual)
    if prompt:
        text, keyboard = prompt
        bot.say(text, reply_markup=keyboard)
