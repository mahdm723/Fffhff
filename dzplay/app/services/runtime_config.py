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
