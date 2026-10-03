"""Settings the admin can change from the panel without editing .env or restarting.

Today: the Telegram bot (token, admin chat ID, webhook secret). Values saved here take
precedence over .env; removing them falls back to .env. The token is stored sealed
(app.security.crypto.seal, key derived from SECRET_KEY) and is never returned to the browser.
"""

from __future__ import annotations

import re
import secrets

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.models import AppSetting
from app.security.crypto import seal, unseal

_PURPOSE = "runtime-config"
TOKEN_RE = re.compile(r"^[0-9]{6,12}:[A-Za-z0-9_-]{30,64}$")
CHAT_RE = re.compile(r"^-?[0-9]{3,20}$")
_KEYS = ("telegram.token", "telegram.chat_id", "telegram.secret")


def _get(db: Session, key: str) -> str | None:
    row = db.get(AppSetting, key)
    return row.value if row else None


def _put(db: Session, key: str, value: str, actor: str | None) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value, updated_at=clock.utcnow(), updated_by=actor))
    else:
        row.value, row.updated_at, row.updated_by = value, clock.utcnow(), actor


def telegram_config(db: Session, settings: Settings) -> dict:
    """The effective bot settings: panel values first, then .env."""
    sealed = _get(db, "telegram.token")
    token = unseal(settings.SECRET_KEY, _PURPOSE, sealed) if sealed else None
    if token:
        return {"token": token, "chat_id": _get(db, "telegram.chat_id") or "",
                "secret": _get(db, "telegram.secret") or settings.TELEGRAM_WEBHOOK_SECRET, "source": "panel"}
    if settings.TELEGRAM_BOT_TOKEN and settings.TELEGRAM_ADMIN_CHAT_ID:
        return {"token": settings.TELEGRAM_BOT_TOKEN, "chat_id": str(settings.TELEGRAM_ADMIN_CHAT_ID),
                "secret": settings.TELEGRAM_WEBHOOK_SECRET, "source": "env"}
    return {"token": None, "chat_id": "", "secret": "", "source": "none"}


def validate(token: object, chat_id: object) -> tuple[str, str]:
    token = token.strip() if isinstance(token, str) else ""
    chat_id = chat_id.strip() if isinstance(chat_id, str) else ""
    if not TOKEN_RE.match(token):
        raise AppError(400, "invalid_token", "رمز البوت غير صالح (الشكل: 123456789:AA…).")
    if not CHAT_RE.match(chat_id):
        raise AppError(400, "invalid_chat_id", "رقم المحادثة غير صالح (أرقام فقط).")
    return token, chat_id


def save_telegram(db: Session, settings: Settings, token: str, chat_id: str, actor: str) -> dict:
    """Store the bot settings (token sealed). A webhook secret is generated once and kept."""
    _put(db, "telegram.token", seal(settings.SECRET_KEY, _PURPOSE, token), actor)
    _put(db, "telegram.chat_id", chat_id, actor)
    if not _get(db, "telegram.secret"):
        _put(db, "telegram.secret", settings.TELEGRAM_WEBHOOK_SECRET or secrets.token_hex(32), actor)
    db.flush()
    return telegram_config(db, settings)


def clear_telegram(db: Session) -> None:
    db.execute(delete(AppSetting).where(AppSetting.key.in_(_KEYS)))


# ---------------------------------------------------------------------------
# e-mail (SMTP) for password-recovery codes
# ---------------------------------------------------------------------------

_SMTP_KEYS = ("smtp.host", "smtp.port", "smtp.security", "smtp.username", "smtp.password", "smtp.from")
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def smtp_config(db: Session, settings: Settings) -> dict:
    """The effective SMTP settings: panel values first, then .env. The password is included (server side only)."""
    host = _get(db, "smtp.host")
    if host:
        sealed = _get(db, "smtp.password")
        return {"host": host, "port": int(_get(db, "smtp.port") or 587), "security": _get(db, "smtp.security") or "starttls",
                "username": _get(db, "smtp.username") or "", "from": _get(db, "smtp.from") or "",
                "password": (unseal(settings.SECRET_KEY, _PURPOSE, sealed) or "") if sealed else "", "source": "panel"}
    if settings.smtp_enabled:
        return {"host": settings.SMTP_HOST, "port": settings.SMTP_PORT, "security": settings.SMTP_SECURITY,
                "username": settings.SMTP_USERNAME, "from": settings.SMTP_FROM, "password": settings.SMTP_PASSWORD,
                "source": "env"}
    return {"host": "", "port": 587, "security": "starttls", "username": "", "from": "", "password": "", "source": "none"}


def effective_settings(db: Session, settings: Settings) -> Settings:
    """Settings with the SMTP values set in the panel (if any) applied."""
    cfg = smtp_config(db, settings)
    if cfg["source"] != "panel":
        return settings
    return settings.model_copy(update={"SMTP_HOST": cfg["host"], "SMTP_PORT": cfg["port"], "SMTP_SECURITY": cfg["security"],
                                       "SMTP_USERNAME": cfg["username"], "SMTP_PASSWORD": cfg["password"],
                                       "SMTP_FROM": cfg["from"]})


def save_smtp(db: Session, settings: Settings, *, host: object, port: object, security: object, username: object,
              password: object, sender: object, actor: str) -> None:
    host = host.strip() if isinstance(host, str) else ""
    security = security if security in ("starttls", "ssl", "none") else ""
    username = username.strip() if isinstance(username, str) else ""
    sender = sender.strip() if isinstance(sender, str) else ""
    if not _HOST_RE.match(host):
        raise AppError(400, "invalid_host", "عنوان خادم البريد غير صالح (مثال: smtp.gmail.com).")
    if not isinstance(port, int) or not 1 <= port <= 65535:
        raise AppError(400, "invalid_port", "المنفذ غير صالح (عادة 587).")
    if not security or (security == "none" and settings.ENV == "production"):
        raise AppError(400, "invalid_security", "اختر STARTTLS أو SSL.")
    from email.utils import parseaddr

    if len(sender) > 200 or not _EMAIL_RE.match(parseaddr(sender)[1] or ""):
        raise AppError(400, "invalid_from", "عنوان المرسل غير صالح (مثال: DZPLAY <you@gmail.com>).")
    if len(username) > 200 or (isinstance(password, str) and len(password) > 200):
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    for key, value in (("smtp.host", host), ("smtp.port", str(port)), ("smtp.security", security),
                       ("smtp.username", username), ("smtp.from", sender)):
        _put(db, key, value, actor)
    if isinstance(password, str) and password:  # empty = keep the stored password
        _put(db, "smtp.password", seal(settings.SECRET_KEY, _PURPOSE, password), actor)
    db.flush()


def clear_smtp(db: Session) -> None:
    db.execute(delete(AppSetting).where(AppSetting.key.in_(_SMTP_KEYS)))
