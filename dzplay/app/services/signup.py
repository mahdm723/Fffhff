"""V6 phase 15: e-mail confirmation at signup, and the owner's admin signup code.

With REGISTRATION_EMAIL_VERIFICATION on (panel setting, on by default) the signup form does not create the account:

1. the form passes every rule of auth.validate_registration (anti-bot, per-network / per-phone limits, the mailbox
   not already used), then a 6-digit code is e-mailed from the system mailbox in the person's language and a
   `signup_pending` row waits (hashes only: the code as an HMAC, the password as argon2);
2. the app shows «أدخل الرمز المرسل إلى بريدك»; the right code creates the account the usual way
   (auth.create_user, marked signup_verified_via = "email");
3. wrong codes count (REGISTER_CODE_MAX_ATTEMPTS cancel the signup), the code expires after REGISTER_CODE_TTL, a new
   one can be sent after REGISTER_RESEND_SECONDS (REGISTER_MAX_SENDS at most), REGISTER_PER_EMAIL_PER_DAY signups
   per mailbox, REGISTER_STARTS_PER_IP_PER_HOUR / REGISTER_VERIFY_PER_IP_PER_HOUR per network.

V6 phase 18 — the codes leave from a background queue (services/mail_queue.py), never from the request:
* the answer is the same, and takes the same time, whether the address already has an account or not. A taken
  address gets a pending row too (with a code that is never sent) and the queue e-mails it «لديك حساب بالفعل»;
* REGISTRATION_CODE_AUTO on (default): the code is e-mailed, and the owner gets a note in Telegram (masked address);
  off: the code and the address go to the owner in Telegram («أُرسل يدويًا» / «إلغاء التسجيل») to send by hand;
* an e-mail that fails (or no mailbox set up) falls back to the manual path + an alert (at most every 30 minutes).
«التسجيل غير متاح مؤقتًا» only when neither the system mailbox nor the Telegram bot works.

The admin signup code: a code the owner chooses in the panel (12+ characters, stored as an argon2 hash, off by
default, optional expiry and number of uses — it switches itself off at the last one). Typed on the code screen
instead of the 6 digits, it confirms the signup (signup_verified_via = "admin_code"); a strict per-network limit
on tries, and every use goes to the audit log and to the owner's Telegram at once.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock, i18n
from app.config import Settings
from app.errors import AppError, rate_limited
from app.i18n import N_
from app.models import AppSetting, SignupPending, User
from app.security.crypto import keyed_hash
from app.security.passwords import hash_password, verify_password
from app.services import audit, auth, mail, runtime_config
from app.services.email_codes import mask
from app.services.rate_limit import Limit

log = logging.getLogger("dzplay.signup")
ADMIN_CODE_KEY = "signup.admin_code"
ADMIN_CODE_MIN = 12
ADMIN_CODE_MAX = 128
ALERT_EVERY = 1800
_SIX = re.compile(r"[0-9]{6}")
UNAVAILABLE = N_("التسجيل غير متاح مؤقتًا. حاول لاحقًا.")


def _id(token: object) -> str | None:
    if not isinstance(token, str) or not 20 <= len(token) <= 64:
        return None
    return hashlib.sha256(token.encode()).hexdigest()


def _code_hash(settings: Settings, pending_id: str, code: str) -> str:
    return hmac.new(settings.SECRET_KEY.encode(), f"signup:{pending_id}:{code}".encode(), hashlib.sha256).hexdigest()


def _new_code() -> str:
    return f"{secrets.randbelow(10 ** 6):06d}"


# ---------------------------------------------------------------------------------------------- the mailbox


def _alert_unavailable(state, why: str) -> None:
    """The owner hears that people cannot sign up (at most every 30 minutes)."""
    bot = getattr(state, "bot", None)
    if bot is None:
        return
    try:
        if state.limiter.check_and_hit([Limit("signup_unavailable_alert", 1, ALERT_EVERY)]).allowed:
            bot.say(f"⚠️ التسجيل متوقف مؤقتًا: {why}.\nلا تُنشأ حسابات جديدة حتى يعمل بريد النظام: لوحة القيادة ← "
                    "الأمان والنظام ← البريد ← «إرسال تجربة». أو أوقف «تأكيد البريد» من الإعدادات ← التسجيل.")
    except Exception:  # noqa: BLE001 - an alert never breaks the request
        pass


def _unavailable(state, why: str) -> AppError:
    _alert_unavailable(state, why)
    return AppError(503, "registration_unavailable", UNAVAILABLE)


def _ready(state, db: Session) -> None:
    """A code can reach the person: the system mailbox, or the owner's bot (manual path)."""
    if not runtime_config.effective_settings(db, state.settings, "system").smtp_enabled and state.bot is None:
        raise _unavailable(state, "لا بريد نظام ولا بوت Telegram")


def _job(row: SignupPending, code: str) -> dict:
    return {"id": row.id, "email": row.email, "lang": row.language, "code": code, "existing": bool(row.existing)}


def _queue(state, job: dict) -> None:
    if not state.mail_queue.submit(deliver, state, job):
        raise _unavailable(state, "طابور البريد ممتلئ")


def deliver(state, job: dict) -> None:
    """Background (mail queue): the code — or «لديك حساب بالفعل» for a taken address — by e-mail in automatic mode,
    to the owner in manual mode or when the e-mail cannot leave."""
    settings = state.settings
    if not settings.REGISTRATION_CODE_AUTO:
        _manual(state, job, "")
        return
    minutes = max(1, settings.REGISTER_CODE_TTL // 60)
    with state.database.session() as db:
        if job["existing"]:
            out = mail.prepare(db, settings, "system", job["email"], "email.signup_exists", {}, lang=job["lang"])
        else:
            out = mail.prepare(db, settings, "system", job["email"], "email.signup_code",
                               {"code": job["code"], "minutes": minutes}, highlight=job["code"], lang=job["lang"])
    reason = "بريد النظام غير مضبوط" if out is None else ""
    if out is not None:
        try:
            mail.deliver(out)
        except Exception as exc:  # noqa: BLE001 - MailError or anything else: the owner takes over
            reason = f"تعذّر الإرسال ({type(exc).__name__})"
    if not reason:
        _note(state, ("ℹ️ محاولة تسجيل ببريد مسجّل مسبقًا ({masked}): أُرسلت له رسالة «لديك حساب بالفعل»."
                      if job["existing"] else "ℹ️ أُرسل رمز تسجيل تلقائيًا إلى {masked}.").format(masked=mask(job["email"])))
        return
    _alert(state, f"⚠️ تعذّر إرسال رمز التسجيل تلقائيًا: {reason}. أُرسل إليك لترسله يدويًا. أصلح البريد من لوحة القيادة ← "
                  "الأمان والنظام ← البريد ← «إرسال تجربة».")
    _manual(state, job, reason)


def _note(state, text: str, **kw) -> None:
    bot = state.bot
    if bot is None:
        return
    try:
        bot.say(text, **kw)
    except Exception as exc:  # noqa: BLE001 - a note never breaks the queue
        log.warning("signup note not sent: %s", type(exc).__name__)


def _alert(state, text: str) -> None:
    try:
        if state.limiter.check_and_hit([Limit("signup_mail_failed_alert", 1, ALERT_EVERY)]).allowed:
            _note(state, text)
    except Exception:  # noqa: BLE001
        pass


def _manual(state, job: dict, reason: str) -> None:
    """The owner sends the code by hand (manual mode, or the e-mail could not leave)."""
    if state.bot is None:
        log.warning("signup code for %s could not be delivered (no mailbox, no bot)", mask(job["email"]))
        return
    why = f" — {reason}" if reason else ""
    if job["existing"]:
        _note(state, f"ℹ️ محاولة تسجيل ببريد مسجّل مسبقًا{why}\nالبريد: {job['email']}\n"
                     "لا رمز: صاحبه يملك حسابًا (يسجّل الدخول أو يستعيد كلمة المرور).")
        return
    minutes = max(1, state.settings.REGISTER_CODE_TTL // 60)
    ref = job["id"][:16]
    keyboard = {"inline_keyboard": [[{"text": "✅ أُرسل يدويًا", "callback_data": f"sg:sent:{ref}"}],
                                    [{"text": "✖️ إلغاء التسجيل", "callback_data": f"sg:cancel:{ref}"}]]}
    _note(state, f"✉️ رمز تسجيل حساب جديد{why}\nالبريد: {job['email']}\nالرمز: {job['code']}\n\n"
                 f"صالح {minutes} دقيقة ولمرة واحدة. أرسله بنفسك إلى هذا البريد، ثم اضغط «أُرسل يدويًا».",
          reply_markup=keyboard)


def _limit_ip(state, ctx, what: str, limit: int) -> None:
    if ctx.trusted:
        return
    d = state.limiter.check_and_hit([Limit(f"signup_{what}:{ctx.ip_hash}", max(1, limit), 3600)])
    if not d.allowed:
        raise rate_limited(d.retry_after)


def _answer(settings: Settings, token: str, row: SignupPending) -> dict:
    return {"verify": True, "signup_token": token, "sent_to": mask(row.email), "expires_in": settings.REGISTER_CODE_TTL,
            "resend_in": settings.REGISTER_RESEND_SECONDS, "code_length": 6}


# ---------------------------------------------------------------------------------------------- the flow


def start(state, ctx, lang: str | None, **form) -> dict:
    """The signup form (with the code on): check everything, keep it pending, e-mail the code."""
    settings = state.settings
    with state.database.session() as db:
        _ready(state, db)
        _limit_ip(state, ctx, "start", settings.REGISTER_STARTS_PER_IP_PER_HOUR)
        reg = auth.validate_registration(db, settings, ctx, allow_taken=True, **form)
        canon = auth.canonical_email(reg.email)
        d = state.limiter.check_and_hit([Limit(f"signup_mailbox:{keyed_hash(settings.SECRET_KEY, 'signup', canon)[:40]}",
                                               max(1, settings.REGISTER_PER_EMAIL_PER_DAY), 86400)])
        if not d.allowed:
            db.commit()  # keep the consumed anti-bot challenge
            raise rate_limited(d.retry_after, "طُلب التسجيل بهذا البريد مرات كثيرة اليوم. حاول لاحقًا.")
        existing = auth.email_in_use(db, reg.email)  # same work, same answer: only the e-mail differs (in the queue)
        db.execute(delete(SignupPending).where(SignupPending.email_canonical == canon))  # a new try replaces the old
        token = secrets.token_urlsafe(24)
        now = clock.utcnow()
        row = SignupPending(id=_id(token), email=reg.email, email_canonical=canon, password_hash=hash_password(reg.password),
                            display_name=reg.display_name, gender=reg.gender, language=i18n.normalize(lang),
                            ip_hash=ctx.ip_hash, device_id=ctx.device_id, attempts=0, sends=1, last_sent_at=now,
                            expires_at=now + timedelta(seconds=settings.REGISTER_CODE_TTL), created_at=now, code_hash="",
                            existing=existing or None)
        code = _new_code()  # a taken address: never sent (nobody can guess it in REGISTER_CODE_MAX_ATTEMPTS tries)
        row.code_hash = _code_hash(settings, row.id, code)
        db.add(row)
        db.commit()
        answer = _answer(settings, token, row)
        job = _job(row, code)
    try:
        _queue(state, job)
    except AppError:
        with state.database.session() as db:
            db.execute(delete(SignupPending).where(SignupPending.id == job["id"]))
            db.commit()
        raise
    return answer


def _pending(db: Session, token: object) -> SignupPending:
    pid = _id(token)
    row = db.get(SignupPending, pid) if pid else None
    if row is None:
        raise AppError(400, "signup_expired", "انتهت صلاحية التسجيل. أعد إدخال بياناتك.")
    if row.expires_at <= clock.utcnow():
        db.delete(row)
        db.commit()
        raise AppError(400, "signup_expired", "انتهت صلاحية الرمز. أعد إدخال بياناتك أو اطلب رمزًا جديدًا.")
    return row


def resend(state, ctx, token: object) -> dict:
    """A new code (after REGISTER_RESEND_SECONDS, REGISTER_MAX_SENDS at most); the old one stops working."""
    settings = state.settings
    with state.database.session() as db:
        pid = _id(token)
        row = db.get(SignupPending, pid) if pid else None
        if row is None:
            raise AppError(400, "signup_expired", "انتهت صلاحية التسجيل. أعد إدخال بياناتك.")
        wait = (row.last_sent_at + timedelta(seconds=settings.REGISTER_RESEND_SECONDS) - clock.utcnow()).total_seconds()
        if wait > 0:
            raise rate_limited(wait, "انتظر قليلًا قبل إعادة إرسال الرمز.")
        if (row.sends or 0) >= settings.REGISTER_MAX_SENDS:
            raise AppError(429, "too_many_sends", "أُرسل الرمز مرات كثيرة. أعد التسجيل لاحقًا.")
        _limit_ip(state, ctx, "start", settings.REGISTER_STARTS_PER_IP_PER_HOUR)
        _ready(state, db)
        now = clock.utcnow()
        code = _new_code()
        row.code_hash = _code_hash(settings, row.id, code)
        row.sends = (row.sends or 0) + 1
        row.attempts = 0
        row.last_sent_at = now
        row.expires_at = now + timedelta(seconds=settings.REGISTER_CODE_TTL)
        db.commit()
        answer = _answer(settings, token, row)
        job = _job(row, code)
    _queue(state, job)
    return answer


def verify(db: Session, state, ctx, token: object, code: object, effects) -> User:
    """The code from the e-mail (or the owner's admin signup code): create the account. The caller signs it in."""
    settings = state.settings
    _limit_ip(state, ctx, "verify", settings.REGISTER_VERIFY_PER_IP_PER_HOUR)
    row = _pending(db, token)
    given = code.strip() if isinstance(code, str) else ""
    via = None
    if _SIX.fullmatch(given):
        if hmac.compare_digest(row.code_hash, _code_hash(settings, row.id, given)):
            via = "email"
    elif given:
        d = state.limiter.check_and_hit([Limit(f"admin_signup_code:{ctx.ip_hash}",
                                               max(1, settings.ADMIN_SIGNUP_CODE_TRIES_PER_IP), 86400)])
        if not d.allowed:
            raise rate_limited(d.retry_after)
        if admin_code_matches(db, given):
            via = "admin_code"
    if via is None:
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= settings.REGISTER_CODE_MAX_ATTEMPTS:
            db.delete(row)
            db.commit()
            raise AppError(400, "code_cancelled", "محاولات كثيرة: أُلغي التسجيل. أعد إدخال بياناتك.")
        db.commit()
        raise AppError(400, "code_invalid", "الرمز غير صحيح.")
    if auth.email_in_use(db, row.email):
        db.delete(row)
        db.commit()
        raise AppError(409, "email_taken", "هذا البريد مسجّل مسبقًا. سجّل الدخول بدلًا من ذلك.")
    from app.services import access

    ctx.device_id = ctx.device_id or row.device_id  # the phone the signup started on (its cookie may have lapsed)
    access.check_signup_device(db, settings, ctx.device_id)  # the phone's limit again (it may have changed)
    user = auth.create_user(db, settings, ctx, email=row.email, password_hash=row.password_hash, gender=row.gender,
                            display_name=row.display_name, verified_via=via, language=row.language)
    if via == "admin_code":
        used = use_admin_code(db)
        audit.record(db, "signup", "admin_signup_code_used", target_type="user", target_id=user.id,
                     detail=f"{mask(row.email)} — {used['uses']}/{used['max_uses'] or '∞'}"
                            + ("" if used["enabled"] else " — أُوقف الرمز تلقائيًا"))
        effects.later(alert_admin_code_used, mask(row.email), user.public_id or "", used["uses"], used["max_uses"],
                      used["enabled"])
    db.delete(row)
    return user


def install(state) -> None:
    """Telegram buttons sg:<sent|cancel>:<first 16 characters of the pending id> (admin chat only)."""
    from app.services.media_moderation import mark_done

    def on_callback(bot, cq: dict, payload: str) -> None:
        action, _, ref = payload.partition(":")
        if action not in ("sent", "cancel") or not re.fullmatch(r"[0-9a-f]{16}", ref):
            bot.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        actor = f"telegram:{(cq.get('from') or {}).get('id')}"
        with state.database.session() as db:
            row = db.scalar(select(SignupPending).where(SignupPending.id.like(f"{ref}%")))
            if row is None:
                label = "انتهى هذا التسجيل (أُكمل أو أُلغي أو انتهت صلاحيته)"
            elif action == "sent":
                audit.record(db, actor, "signup_code_sent_by_hand", target_type="signup", detail=mask(row.email))
                label = "✅ أُرسل الرمز يدويًا"
            else:
                audit.record(db, actor, "signup_cancelled", target_type="signup", detail=mask(row.email))
                db.delete(row)
                label = "✖️ أُلغي التسجيل"
        bot.tg.answer_callback(cq.get("id"), label[:60])
        msg = cq.get("message") or {}
        mark_done(state, (msg.get("chat") or {}).get("id"), msg.get("message_id"), label)

    if state.bot is not None:
        state.bot.callback_handlers["sg"] = on_callback


def alert_admin_code_used(state, masked: str, public_id: str, uses: int, max_uses: int | None, enabled: bool) -> None:
    """Telegram, at once: every account made with the admin signup code."""
    bot = getattr(state, "bot", None)
    if bot is None:
        return
    left = "" if enabled else "\nوصل الرمز إلى حده الأقصى وأُوقف تلقائيًا."
    bot.say(f"🔑 استُعمل رمز إنشاء الحساب الإداري.\nحساب جديد: {masked} ({public_id})\n"
            f"الاستعمالات: {uses}/{max_uses or '∞'}{left}\nإن لم تكن أنت: أوقف الرمز من لوحة القيادة ← الأمان والنظام.")


# ---------------------------------------------------------------------------------------------- the admin signup code


def _load(db: Session, lock: bool = False) -> dict:
    row = db.get(AppSetting, ADMIN_CODE_KEY, with_for_update=lock) if lock else db.get(AppSetting, ADMIN_CODE_KEY)
    if row is None:
        return {}
    try:
        data = json.loads(row.value)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return {}


def _store(db: Session, data: dict, actor: str) -> None:
    now = clock.utcnow()
    row = db.get(AppSetting, ADMIN_CODE_KEY)
    if row is None:
        db.add(AppSetting(key=ADMIN_CODE_KEY, value=json.dumps(data), updated_at=now, updated_by=actor[:80]))
    else:
        row.value, row.updated_at, row.updated_by = json.dumps(data), now, actor[:80]


def _parse_when(value: object) -> datetime | None:
    if value in (None, ""):
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise AppError(400, "invalid_input", "تاريخ الانتهاء غير صالح.") from None
    if dt.tzinfo is not None:  # the panel sends UTC ("…Z"); stored naive UTC like every date here
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _active(data: dict) -> bool:
    if not data.get("enabled") or not data.get("hash"):
        return False
    exp = data.get("expires_at")
    if exp and datetime.fromisoformat(exp) <= clock.utcnow():
        return False
    return not (data.get("max_uses") and int(data.get("uses") or 0) >= int(data["max_uses"]))


def admin_code_status(db: Session) -> dict:
    """For the panel (never the code or its hash)."""
    data = _load(db)
    exp = data.get("expires_at")
    return {"configured": bool(data.get("hash")), "enabled": bool(data.get("enabled")), "active": _active(data),
            "expired": bool(exp and datetime.fromisoformat(exp) <= clock.utcnow()),
            "expires_at": exp + "Z" if exp else None, "max_uses": data.get("max_uses"), "uses": int(data.get("uses") or 0),
            "set_at": (data["set_at"] + "Z") if data.get("set_at") else None, "set_by": data.get("set_by"),
            "min_length": ADMIN_CODE_MIN}


def save_admin_code(db: Session, actor: str, *, code: object, enabled: bool, expires_at: object, max_uses: object) -> dict:
    """Set a new code (resets the uses), switch it on / off, change its expiry and number of uses. The caller audits
    the change (never the code itself)."""
    data = _load(db, lock=True)
    if code not in (None, ""):
        if not isinstance(code, str) or not ADMIN_CODE_MIN <= len(code.strip()) <= ADMIN_CODE_MAX:
            raise AppError(400, "code_too_short", "الرمز {min} حرفًا على الأقل.", params={"min": ADMIN_CODE_MIN})
        if _SIX.fullmatch(code.strip()):
            raise AppError(400, "invalid_input", "طلب غير صالح.")
        data.update(hash=hash_password(code.strip()), uses=0, set_at=clock.utcnow().isoformat(), set_by=actor[:80])
    if enabled and not data.get("hash"):
        raise AppError(400, "code_required", "اكتب رمزًا أولًا.")
    exp = _parse_when(expires_at)
    if enabled and exp is not None and exp <= clock.utcnow():
        raise AppError(400, "invalid_input", "تاريخ الانتهاء يجب أن يكون في المستقبل.")
    if max_uses in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(max_uses)
        except (TypeError, ValueError):
            raise AppError(400, "invalid_input", "طلب غير صالح.") from None
        if not 1 <= limit <= 10_000:
            raise AppError(400, "invalid_input", "طلب غير صالح.")
    data.update(enabled=bool(enabled), expires_at=exp.isoformat() if exp else None, max_uses=limit)
    if enabled and limit and int(data.get("uses") or 0) >= limit:
        data["uses"] = 0  # switched on again with a new allowance
    _store(db, data, actor)
    db.flush()
    return admin_code_status(db)


def admin_code_matches(db: Session, given: str) -> bool:
    data = _load(db)
    return _active(data) and len(given) <= ADMIN_CODE_MAX and verify_password(data["hash"], given)


def use_admin_code(db: Session) -> dict:
    """Count a use (under a row lock); the last allowed use switches the code off."""
    data = _load(db, lock=True)
    data["uses"] = int(data.get("uses") or 0) + 1
    if data.get("max_uses") and data["uses"] >= int(data["max_uses"]):
        data["enabled"] = False
    _store(db, data, "signup")
    return {"uses": data["uses"], "max_uses": data.get("max_uses"), "enabled": bool(data.get("enabled"))}
