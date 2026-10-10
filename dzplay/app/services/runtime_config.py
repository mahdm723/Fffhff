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

# V6 phase 7: two mailboxes. "system" keeps the original smtp.* keys (no migration); "support" has its own.
PROFILES = ("system", "support")
_PREFIX = {"system": "smtp.", "support": "smtp.support."}
_FIELDS = ("host", "port", "security", "username", "password", "from")
_SMTP_KEYS = tuple(_PREFIX["system"] + f for f in _FIELDS)  # kept for callers of the V5 name
_HOST_RE = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")


def _profile(profile: str) -> str:
    if profile not in _PREFIX:
        raise AppError(400, "invalid_profile", "طلب غير صالح.")
    return _PREFIX[profile]


def smtp_config(db: Session, settings: Settings, profile: str = "system") -> dict:
    """The effective SMTP settings of a mailbox. The password is included (server side only).

    system: panel values first, then .env.  support: its own panel values, else the system mailbox
    ("inherited": True), so support mail still goes out before the support account is set up."""
    pre = _profile(profile)
    host = _get(db, pre + "host")
    if host:
        sealed = _get(db, pre + "password")
        return {"host": host, "port": int(_get(db, pre + "port") or 587), "security": _get(db, pre + "security") or "starttls",
                "username": _get(db, pre + "username") or "", "from": _get(db, pre + "from") or "",
                "password": (unseal(settings.SECRET_KEY, _PURPOSE, sealed) or "") if sealed else "", "source": "panel",
                "inherited": False}
    if profile == "support":
        return {**smtp_config(db, settings, "system"), "inherited": True}
    if settings.smtp_enabled:
        return {"host": settings.SMTP_HOST, "port": settings.SMTP_PORT, "security": settings.SMTP_SECURITY,
                "username": settings.SMTP_USERNAME, "from": settings.SMTP_FROM, "password": settings.SMTP_PASSWORD,
                "source": "env", "inherited": False}
    return {"host": "", "port": 587, "security": "starttls", "username": "", "from": "", "password": "", "source": "none",
            "inherited": False}


def effective_settings(db: Session, settings: Settings, profile: str = "system") -> Settings:
    """Settings with the SMTP values of this mailbox (set in the panel, if any) applied."""
    cfg = smtp_config(db, settings, profile)
    if cfg["source"] != "panel":
        return settings
    return settings.model_copy(update={"SMTP_HOST": cfg["host"], "SMTP_PORT": cfg["port"], "SMTP_SECURITY": cfg["security"],
                                       "SMTP_USERNAME": cfg["username"], "SMTP_PASSWORD": cfg["password"],
                                       "SMTP_FROM": cfg["from"]})


def save_smtp(db: Session, settings: Settings, *, host: object, port: object, security: object, username: object,
              password: object, sender: object, actor: str, profile: str = "system") -> None:
    pre = _profile(profile)
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
        raise AppError(400, "invalid_from", "عنوان المرسل غير صالح (مثال: DALTA.BIT <you@gmail.com>).")
    if len(username) > 200 or (isinstance(password, str) and len(password) > 200):
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    for field, value in (("host", host), ("port", str(port)), ("security", security), ("username", username),
                         ("from", sender)):
        _put(db, pre + field, value, actor)
    if isinstance(password, str) and password:  # empty = keep the stored password
        _put(db, pre + "password", seal(settings.SECRET_KEY, _PURPOSE, password), actor)
    db.flush()


def clear_smtp(db: Session, profile: str = "system") -> None:
    pre = _profile(profile)
    db.execute(delete(AppSetting).where(AppSetting.key.in_(tuple(pre + f for f in _FIELDS))))
