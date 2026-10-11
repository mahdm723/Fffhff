"""Admin accounts for the panel: password + TOTP, strict throttling, sessions.

* Separate from user accounts: own table, own cookie (scoped to ADMIN_PATH),
  short idle timeout and absolute lifetime.
* Login needs username + password + a 6-digit TOTP code. A TOTP code can be
  used once (replay guard). Unknown usernames cost the same time.
* Failed logins are counted per network and per username (security events);
  over ADMIN_LOGIN_MAX_FAILURES in ADMIN_LOGIN_WINDOW → refused before any check.
* Optional ADMIN_IP_ALLOWLIST: outside it the panel does not exist (404).
"""

from __future__ import annotations

import ipaddress
import math
from datetime import timedelta

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.models import AdminSession, AdminUser, SecurityEvent
from app.security import totp
from app.security.crypto import keyed_hash, new_token, sha256_hex
from app.security.passwords import hash_password, verify_password
from app.services.auth import ClientContext, log_event

COOKIE_NAME = "dz_admin"
# Roles → what they may do. Only super_admin exists today; add e.g. "moderator" later
# and give routes require_role("super_admin", "moderator").
ROLES = ("super_admin",)


def role_of(admin: AdminUser) -> str:
    return admin.role or "super_admin"
MIN_PASSWORD_LENGTH = 12


def ip_allowed(settings: Settings, ip: str) -> bool:
    raw = [p.strip() for p in settings.ADMIN_IP_ALLOWLIST.split(",") if p.strip()]
    if not raw:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for item in raw:
        try:
            if addr in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            continue
    return False


def _failures(db: Session, since, **where) -> list:
    q = select(SecurityEvent.created_at).where(SecurityEvent.type == "admin_login_failed", SecurityEvent.created_at > since)
    for col, value in where.items():
        q = q.where(getattr(SecurityEvent, col) == value)
    return db.execute(q.order_by(SecurityEvent.created_at)).scalars().all()


def _lockout_seconds(db: Session, settings: Settings, ctx: ClientContext, username_ref: str) -> int:
    since = clock.utcnow() - timedelta(seconds=settings.ADMIN_LOGIN_WINDOW)
    worst = 0
    for times in (_failures(db, since, ip_hash=ctx.ip_hash), _failures(db, since, detail=username_ref)):
        if len(times) >= settings.ADMIN_LOGIN_MAX_FAILURES:
            oldest = times[-settings.ADMIN_LOGIN_MAX_FAILURES]
            wait = (oldest + timedelta(seconds=settings.ADMIN_LOGIN_WINDOW) - clock.utcnow()).total_seconds()
            worst = max(worst, math.ceil(wait))
    return worst


def create_admin(db: Session, settings: Settings, username: str, password: str,
                 role: str = "super_admin") -> tuple[AdminUser, str]:
    if role not in ROLES:
        raise ValueError(f"role must be one of: {', '.join(ROLES)}")
    username = username.strip().lower()
    if not (3 <= len(username) <= 64) or not username.replace("-", "").replace("_", "").replace(".", "").isalnum():
        raise ValueError("username: 3-64 letters, digits, '.', '-' or '_'")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if db.scalar(select(AdminUser.id).where(AdminUser.username == username)):
        raise ValueError("this admin already exists")
    secret = totp.new_secret()
    admin = AdminUser(username=username, password_hash=hash_password(password), role=role,
                      totp_secret_enc=totp.encrypt_secret(settings.SECRET_KEY, secret))
    db.add(admin)
    db.flush()
    return admin, secret


def reset_totp(db: Session, settings: Settings, username: str) -> str:
    admin = db.scalar(select(AdminUser).where(AdminUser.username == username.strip().lower()))
    if admin is None:
        raise ValueError("no such admin")
    secret = totp.new_secret()
    admin.totp_secret_enc = totp.encrypt_secret(settings.SECRET_KEY, secret)
    admin.last_totp_step = None
    db.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))
    return secret


def set_password(db: Session, username: str, password: str) -> None:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    admin = db.scalar(select(AdminUser).where(AdminUser.username == username.strip().lower()))
    if admin is None:
        raise ValueError("no such admin")
    admin.password_hash = hash_password(password)
    db.execute(delete(AdminSession).where(AdminSession.admin_id == admin.id))


def login(db: Session, settings: Settings, ctx: ClientContext, username: object, password: object,
          code: object) -> tuple[AdminUser, str]:
    if not all(isinstance(v, str) for v in (username, password, code)) or len(str(password)) > 256:
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    uname = username.strip().lower()[:64]
    uref = keyed_hash(settings.SECRET_KEY, "admin-user", uname)[:24]
    wait = _lockout_seconds(db, settings, ctx, uref)
    if wait:
        raise AppError(429, "rate_limited", "محاولات كثيرة. حاول لاحقًا.", wait)

    admin = db.scalar(select(AdminUser).where(AdminUser.username == uname))
    ok_password = verify_password(admin.password_hash if admin else None, password)
    secret = totp.decrypt_secret(settings.SECRET_KEY, admin.totp_secret_enc) if admin else None
    step = totp.matching_step(secret or totp.new_secret(), code, clock.timestamp())
    replay = admin is not None and step is not None and admin.last_totp_step is not None and step <= admin.last_totp_step
    if admin is None or admin.disabled or not ok_password or secret is None or step is None or replay:
        log_event(db, "admin_login_failed", ctx, None, uref)
        db.commit()  # keep the failure even though we raise
        raise AppError(401, "invalid_credentials", "بيانات الدخول أو رمز التحقق غير صحيحة.")

    admin.last_totp_step = step
    now = clock.utcnow()
    admin.last_login_at = now
    token = new_token()
    db.add(AdminSession(token_hash=sha256_hex(token), admin_id=admin.id, created_at=now, last_seen_at=now,
                        expires_at=now + timedelta(seconds=settings.ADMIN_SESSION_TTL)))
    log_event(db, "admin_login", ctx, None, admin.username)
    return admin, token


def verify_step_up(db: Session, settings: Settings, ctx: ClientContext, admin: AdminUser, code: object) -> None:
    """Re-check the admin's authenticator code before a sensitive change (same replay guard and lockout as login)."""
    uref = keyed_hash(settings.SECRET_KEY, "admin-user", admin.username)[:24]
    wait = _lockout_seconds(db, settings, ctx, uref)
    if wait:
        raise AppError(429, "rate_limited", "محاولات كثيرة. حاول لاحقًا.", wait)
    row = db.get(AdminUser, admin.id)
    secret = totp.decrypt_secret(settings.SECRET_KEY, row.totp_secret_enc) if row else None
    step = totp.matching_step(secret, code, clock.timestamp()) if secret and isinstance(code, str) else None
    if step is None or (row.last_totp_step is not None and step <= row.last_totp_step):
        log_event(db, "admin_login_failed", ctx, None, uref)
        db.commit()  # keep the failure even though we raise
        raise AppError(403, "invalid_code", "رمز التحقق غير صحيح.")
    row.last_totp_step = step


def resolve(db: Session, settings: Settings, token: str | None) -> AdminUser | None:
    if not token or len(token) > 128:
        return None
    sess = db.get(AdminSession, sha256_hex(token))
    now = clock.utcnow()
    if sess is None or sess.expires_at <= now or now - sess.last_seen_at > timedelta(seconds=settings.ADMIN_SESSION_IDLE):
        if sess is not None:
            db.delete(sess)
        return None
    admin = db.get(AdminUser, sess.admin_id)
    if admin is None or admin.disabled:
        return None
    if now - sess.last_seen_at > timedelta(seconds=30):
        sess.last_seen_at = now
    return admin


def logout(db: Session, token: str | None) -> None:
    if token:
        db.execute(delete(AdminSession).where(AdminSession.token_hash == sha256_hex(token)))


def purge_sessions(db: Session, settings: Settings) -> int:
    now = clock.utcnow()
    idle = now - timedelta(seconds=settings.ADMIN_SESSION_IDLE)
    return db.execute(delete(AdminSession).where((AdminSession.expires_at <= now) | (AdminSession.last_seen_at < idle))).rowcount or 0


def count_admins(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(AdminUser)) or 0
