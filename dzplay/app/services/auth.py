"""Registration, login and brute-force protection.

Layered protection (all thresholds in config):

1. Anti-bot proof-of-work on register/login, harder for IPs with failures.
2. Progressive delay between failed attempts (1s, 2s, 4s … capped).
3. LOGIN_MAX_ATTEMPTS consecutive failures from one IP on one account block
   that IP for IP_BLOCK_DURATION. In the default "ip_account" scope this does
   not lock out other people sharing the IP (NAT, mobile carrier, VPN); the
   whole IP is blocked only after IP_MAX_FAILED_LOGINS failures across
   accounts (credential stuffing). LOGIN_BLOCK_SCOPE="ip" blocks the whole IP
   after LOGIN_MAX_ATTEMPTS failures.
4. Per-account lock (any IP) to stop distributed guessing on one account.
5. Max accounts created per IP per window.
6. Every decision is written to `security_events`.

IPs and e-mails are only stored as keyed hashes for these purposes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, rate_limited
from app.models import AuthThrottle, SecurityEvent, User
from app.security import pow as antibot
from app.security.crypto import keyed_hash
from app.security.passwords import hash_password, needs_rehash, verify_password
from app.services import google_auth

_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,189}\.[^@\s.]{2,63}$")
_INVALID_CREDENTIALS = "البريد الإلكتروني أو كلمة المرور غير صحيحة."
_BLOCKED = "تم إيقاف محاولات تسجيل الدخول مؤقتًا بسبب كثرة المحاولات الخاطئة."


@dataclass
class ClientContext:
    ip: str
    ip_hash: str
    trusted: bool


def make_context(settings: Settings, ip: str) -> ClientContext:
    return ClientContext(ip=ip, ip_hash=keyed_hash(settings.SECRET_KEY, "ip", ip), trusted=ip in settings.trusted_ips)


def log_event(db: Session, type_: str, ctx: ClientContext | None = None, user_id: str | None = None, detail: str | None = None) -> None:
    db.add(SecurityEvent(type=type_, ip_hash=ctx.ip_hash if ctx else None, user_id=user_id, detail=detail, created_at=clock.utcnow()))


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def normalize_email(raw: object) -> str:
    if not isinstance(raw, str):
        raise AppError(400, "invalid_email", "البريد الإلكتروني غير صالح.")
    email = raw.strip().lower()
    if len(email) > 254 or not _EMAIL_RE.match(email):
        raise AppError(400, "invalid_email", "البريد الإلكتروني غير صالح.")
    return email


def validate_new_password(settings: Settings, password: object, confirm: object, email: str) -> str:
    if not isinstance(password, str) or not isinstance(confirm, str):
        raise AppError(400, "invalid_password", "كلمة المرور غير صالحة.")
    if password != confirm:
        raise AppError(400, "password_mismatch", "كلمتا المرور غير متطابقتين.")
    if len(password) < settings.PASSWORD_MIN_LENGTH:
        raise AppError(400, "weak_password", f"كلمة المرور يجب أن تكون {settings.PASSWORD_MIN_LENGTH} أحرف على الأقل.")
    if len(password) > settings.PASSWORD_MAX_LENGTH:
        raise AppError(400, "invalid_password", "كلمة المرور طويلة جدًا.")
    if password.strip().lower() == email or len(set(password)) < 4:
        raise AppError(400, "weak_password", "كلمة المرور ضعيفة جدًا. اختر كلمة مرور أقوى.")
    return password


# ---------------------------------------------------------------------------
# anti-bot difficulty
# ---------------------------------------------------------------------------


def _ip_is_suspicious(db: Session, settings: Settings, ctx: ClientContext) -> bool:
    if ctx.trusted:
        return False
    row = db.get(AuthThrottle, f"ip:{ctx.ip_hash}")
    window = timedelta(seconds=settings.LOGIN_FAILURE_WINDOW)
    return bool(row and row.failures > 0 and clock.utcnow() - row.last_failure_at < window)


def required_pow_difficulty(db: Session, settings: Settings, ctx: ClientContext) -> int:
    if _ip_is_suspicious(db, settings, ctx):
        return settings.POW_ELEVATED_MAX_NUMBER
    return settings.POW_MAX_NUMBER


def issue_challenge(db: Session, settings: Settings, ctx: ClientContext, purpose: str) -> dict:
    if purpose not in antibot.PURPOSES:
        raise AppError(400, "invalid_purpose", "طلب غير صالح.")
    max_number = required_pow_difficulty(db, settings, ctx)
    return antibot.create_challenge(settings.SECRET_KEY, purpose, max_number, settings.POW_CHALLENGE_TTL).as_dict()


def _check_antibot(db: Session, settings: Settings, ctx: ClientContext, purpose: str, payload: dict | None) -> None:
    if not settings.ANTIBOT_ENABLED:
        return
    antibot.verify_solution(db, settings.SECRET_KEY, purpose, payload, required_pow_difficulty(db, settings, ctx))


# ---------------------------------------------------------------------------
# registration
# ---------------------------------------------------------------------------


def _check_registration_quota(db: Session, settings: Settings, ctx: ClientContext) -> None:
    if ctx.trusted:
        return
    since = clock.utcnow() - timedelta(seconds=settings.ACCOUNTS_PER_IP_WINDOW)
    rows = db.execute(
        select(SecurityEvent.created_at)
        .where(SecurityEvent.type == "register", SecurityEvent.ip_hash == ctx.ip_hash, SecurityEvent.created_at > since)
        .order_by(SecurityEvent.created_at)
    ).scalars().all()
    if len(rows) >= settings.MAX_ACCOUNTS_PER_IP:
        log_event(db, "register_limited", ctx)
        db.commit()
        retry = (rows[0] + timedelta(seconds=settings.ACCOUNTS_PER_IP_WINDOW) - clock.utcnow()).total_seconds()
        raise rate_limited(retry, "تم إنشاء عدد كبير من الحسابات من هذه الشبكة. حاول لاحقًا.")


def register(db: Session, settings: Settings, ctx: ClientContext, *, email: object, password: object,
             password_confirm: object, antibot_payload: dict | None, honeypot: str | None,
             gender: object = None, age_confirmed: object = False, display_name: object = None) -> User:
    if honeypot:
        log_event(db, "honeypot", ctx)
        db.commit()
        raise AppError(400, "antibot_invalid", "فشل التحقق من أنك لست روبوتًا. أعد المحاولة.")
    email_n = normalize_email(email)
    pw = validate_new_password(settings, password, password_confirm, email_n)
    if gender not in ("male", "female", "unspecified"):
        raise AppError(400, "gender_required", "اختر: رجل، أنثى، أو أفضّل عدم الذكر.")
    if age_confirmed is not True:
        raise AppError(400, "age_required", "يجب تأكيد أن عمرك 18 سنة أو أكثر والموافقة على شروط الاستخدام.")
    from app.services import names

    name = names.require_name(display_name, settings)
    _check_registration_quota(db, settings, ctx)
    _check_antibot(db, settings, ctx, "register", antibot_payload)

    if db.scalar(select(User.id).where(User.email == email_n)):
        db.commit()  # keep the consumed challenge
        raise AppError(409, "email_taken", "هذا البريد مسجّل مسبقًا. سجّل الدخول بدلًا من ذلك.")
    now = clock.utcnow()
    user = User(email=email_n, password_hash=hash_password(pw), registration_ip_hash=ctx.ip_hash,
                privacy_ack_version=PRIVACY_VERSION, gender=gender, gender_asked_at=now, age_confirmed_at=now,
                display_name=name, name_norm=names.search_key(name))
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        raise AppError(409, "email_taken", "هذا البريد مسجّل مسبقًا. سجّل الدخول بدلًا من ذلك.") from None
    log_event(db, "register", ctx, user.id)
    return user


# ---------------------------------------------------------------------------
# login
# ---------------------------------------------------------------------------


def _active(row: AuthThrottle | None, now: datetime) -> bool:
    return bool(row and row.blocked_until and row.blocked_until > now)


def _check_login_allowed(db: Session, settings: Settings, ctx: ClientContext, acct: str) -> None:
    now = clock.utcnow()
    keys = [f"ipacct:{ctx.ip_hash}:{acct}", f"acct:{acct}"]
    if not ctx.trusted:
        keys.insert(0, f"ip:{ctx.ip_hash}")
    for key in keys:
        row = db.get(AuthThrottle, key)
        if _active(row, now):
            raise rate_limited((row.blocked_until - now).total_seconds(), _BLOCKED)

    # Progressive slow-down between failures on this IP/account pair.
    row = db.get(AuthThrottle, f"ipacct:{ctx.ip_hash}:{acct}")
    if row and row.failures > 0:
        delay = min(settings.LOGIN_BACKOFF_MAX, settings.LOGIN_BACKOFF_BASE * 2 ** (row.failures - 1))
        wait = (row.last_failure_at + timedelta(seconds=delay) - now).total_seconds()
        if wait > 0:
            raise rate_limited(wait, "انتظر قليلًا قبل المحاولة مرة أخرى.")


def _bump(db: Session, key: str, now: datetime, window: int) -> AuthThrottle:
    row = db.get(AuthThrottle, key)
    if row is None:
        row = AuthThrottle(key=key, failures=0, first_failure_at=now, last_failure_at=now)
        db.add(row)
    elif now - row.first_failure_at > timedelta(seconds=window) and not _active(row, now):
        row.failures = 0
        row.first_failure_at = now
        row.blocked_until = None
    row.failures += 1
    row.last_failure_at = now
    return row


def _record_failure(db: Session, settings: Settings, ctx: ClientContext, acct: str, user_id: str | None) -> None:
    now = clock.utcnow()
    window = settings.LOGIN_FAILURE_WINDOW
    block_until = now + timedelta(seconds=settings.IP_BLOCK_DURATION)

    pair = _bump(db, f"ipacct:{ctx.ip_hash}:{acct}", now, window)
    if pair.failures >= settings.LOGIN_MAX_ATTEMPTS:
        pair.blocked_until = block_until
        log_event(db, "login_blocked_ip_account", ctx, user_id)

    if not ctx.trusted:
        ip_row = _bump(db, f"ip:{ctx.ip_hash}", now, window)
        threshold = settings.LOGIN_MAX_ATTEMPTS if settings.LOGIN_BLOCK_SCOPE == "ip" else settings.IP_MAX_FAILED_LOGINS
        if ip_row.failures >= threshold:
            ip_row.blocked_until = block_until
            log_event(db, "login_blocked_ip", ctx, user_id)

    acct_row = _bump(db, f"acct:{acct}", now, window)
    if acct_row.failures >= settings.ACCOUNT_MAX_FAILED_LOGINS:
        acct_row.blocked_until = now + timedelta(seconds=settings.ACCOUNT_LOCK_DURATION)
        log_event(db, "account_locked", ctx, user_id)

    log_event(db, "login_failed", ctx, user_id)


def _clear_failures(db: Session, settings: Settings, ctx: ClientContext, acct: str) -> None:
    keys = [f"ipacct:{ctx.ip_hash}:{acct}", f"acct:{acct}"]
    if settings.LOGIN_BLOCK_SCOPE == "ip":
        keys.append(f"ip:{ctx.ip_hash}")  # "consecutive" failures from this IP are reset
    for key in keys:
        row = db.get(AuthThrottle, key)
        if row is not None:
            db.delete(row)


def login(db: Session, settings: Settings, ctx: ClientContext, *, email: object, password: object,
          antibot_payload: dict | None) -> User:
    try:
        email_n = normalize_email(email)
    except AppError:
        raise AppError(401, "invalid_credentials", _INVALID_CREDENTIALS) from None
    if not isinstance(password, str) or len(password) > settings.PASSWORD_MAX_LENGTH:
        raise AppError(401, "invalid_credentials", _INVALID_CREDENTIALS)
    acct = keyed_hash(settings.SECRET_KEY, "acct", email_n)

    _check_login_allowed(db, settings, ctx, acct)
    _check_antibot(db, settings, ctx, "login", antibot_payload)

    user = db.scalar(select(User).where(User.email == email_n))
    if not verify_password(user.password_hash if user else None, password) or user is None:
        _record_failure(db, settings, ctx, acct, user.id if user else None)
        db.commit()  # persist counters + consumed challenge before refusing
        raise AppError(401, "invalid_credentials", _INVALID_CREDENTIALS)

    if user.status == "banned":
        log_event(db, "login_banned", ctx, user.id)
        db.commit()
        raise AppError(403, "account_banned", "تم إيقاف هذا الحساب بسبب مخالفة شروط الاستخدام.")

    _clear_failures(db, settings, ctx, acct)
    if needs_rehash(user.password_hash):
        user.password_hash = hash_password(password)
    log_event(db, "login_success", ctx, user.id)
    return user


# ---------------------------------------------------------------------------
# Google
# ---------------------------------------------------------------------------


def google_login(db: Session, settings: Settings, ctx: ClientContext, *, credential: object,
                 expected_nonce: str | None) -> User:
    if not settings.google_enabled:
        raise AppError(404, "google_disabled", "تسجيل الدخول عبر Google غير مفعّل.")
    if not isinstance(credential, str) or not credential or len(credential) > 8192:
        raise AppError(400, "google_invalid", "تعذّر التحقق من حساب Google.")
    try:
        claims = google_auth.verify_google_id_token(credential, settings.GOOGLE_CLIENT_ID)
    except Exception:  # noqa: BLE001 - any verification problem is a refusal
        log_event(db, "google_invalid_token", ctx)
        db.commit()
        raise AppError(401, "google_invalid", "تعذّر التحقق من حساب Google.") from None

    if claims.get("iss") not in google_auth.GOOGLE_ISSUERS or claims.get("aud") != settings.GOOGLE_CLIENT_ID:
        raise AppError(401, "google_invalid", "تعذّر التحقق من حساب Google.")
    if not expected_nonce or claims.get("nonce") != expected_nonce:
        raise AppError(401, "google_invalid", "انتهت صلاحية جلسة تسجيل الدخول. أعد المحاولة.")
    if not claims.get("email_verified") or not claims.get("email") or not claims.get("sub"):
        raise AppError(401, "google_unverified", "بريد حساب Google غير مؤكد.")

    sub = str(claims["sub"])
    email_n = normalize_email(claims["email"])
    user = db.scalar(select(User).where(User.google_sub == sub))
    if user is not None:
        if user.status == "banned":
            raise AppError(403, "account_banned", "تم إيقاف هذا الحساب بسبب مخالفة شروط الاستخدام.")
        log_event(db, "login_success_google", ctx, user.id)
        return user

    if db.scalar(select(User.id).where(User.email == email_n)):
        # Do not silently merge: the password account's e-mail was never verified,
        # so auto-linking could hand a pre-registered account to someone else.
        raise AppError(409, "email_registered_with_password",
                       "هذا البريد مسجّل بكلمة مرور. سجّل الدخول بالبريد وكلمة المرور.")

    _check_registration_quota(db, settings, ctx)
    user = User(email=email_n, google_sub=sub, password_hash=None, registration_ip_hash=ctx.ip_hash,
                privacy_ack_version=PRIVACY_VERSION, onboarding_required=True)
    db.add(user)
    try:
        db.flush()
    except IntegrityError:
        raise AppError(409, "email_taken", "تعذّر إنشاء الحساب. حاول مرة أخرى.") from None
    log_event(db, "register", ctx, user.id, "google")
    return user


def count_events(db: Session, type_: str, since: datetime) -> int:
    return db.scalar(select(func.count()).select_from(SecurityEvent).where(SecurityEvent.type == type_, SecurityEvent.created_at > since)) or 0
