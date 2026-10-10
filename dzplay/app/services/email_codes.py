"""V6 one-time e-mail codes that confirm sensitive actions: a membership refund, a withdrawal, a giveaway e-mail.

6 digits, valid EMAIL_CODE_TTL, 5 wrong tries cancel it, a new code replaces the previous one. Only an HMAC of the
code is stored (keyed with SECRET_KEY). Purposes never reuse "payout" (removed with the V5 data by legacy_v6).
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, rate_limited
from app.models import EmailCode, User

PURPOSES = {"refund": "استرجاع العضوية", "withdraw": "سحب الأرباح", "giveaway": "تأكيد البريد للظرف الأحمر"}
MAX_ATTEMPTS = 5


def _hash(settings: Settings, user_id: str, purpose: str, email: str, code: str) -> str:
    msg = f"{user_id}:{purpose}:{email.lower()}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def mask(email: str) -> str:
    local, _, domain = email.partition("@")
    return f"{local[:1]}***@{domain}" if domain else "***"


def send(db: Session, settings: Settings, limiter, user: User, purpose: str, email: str | None = None) -> dict:
    """Create a code and e-mail it now (raises 503 when the mail cannot be sent: nothing is kept)."""
    from app.services import mail, runtime_config
    from app.services.rate_limit import Limit

    if purpose not in PURPOSES:
        raise AppError(400, "invalid_purpose", "طلب غير صالح.")
    to = (email or user.email or "").strip().lower()
    if not to or "@" not in to:
        raise AppError(400, "invalid_email", "بريد غير صالح.")
    decision = limiter.check_and_hit([Limit(f"email_code:{user.id}", 5, 3600), Limit(f"email_code_day:{user.id}", 15, 86400)])
    if not decision.allowed:
        raise rate_limited(decision.retry_after, "طلبت رموزًا كثيرة. انتظر قليلًا.")
    effective = runtime_config.effective_settings(db, settings)
    if not effective.smtp_enabled:
        raise AppError(503, "mail_unavailable", "إرسال البريد غير متاح حاليًا. حاول لاحقًا أو تواصل مع الدعم.")
    db.execute(delete(EmailCode).where(EmailCode.user_id == user.id, EmailCode.purpose == purpose,
                                       EmailCode.confirmed_at.is_(None)))
    code = f"{secrets.randbelow(10 ** 6):06d}"
    now = clock.utcnow()
    db.add(EmailCode(user_id=user.id, purpose=purpose, email=to, code_hash=_hash(settings, user.id, purpose, to, code),
                     created_at=now, expires_at=now + timedelta(seconds=settings.EMAIL_CODE_TTL)))
    db.flush()
    minutes = max(1, settings.EMAIL_CODE_TTL // 60)
    try:  # V6 phase 7: system mailbox, editable text (email.code)
        mail.send(db, settings, "system", to, "email.code", {"purpose": PURPOSES[purpose], "code": code, "minutes": minutes},
                  highlight=code)
    except Exception:  # noqa: BLE001 - MailError and anything else: no code without the e-mail
        db.rollback()
        raise AppError(503, "mail_failed", "تعذّر إرسال البريد الآن. حاول بعد قليل.") from None
    return {"sent_to": mask(to), "expires_in": settings.EMAIL_CODE_TTL}


def verify(db: Session, settings: Settings, user: User, purpose: str, code: object, email: str | None = None) -> EmailCode:
    """Check (and use up) the latest code for this purpose. Wrong codes count; the 5th one cancels it."""
    row = db.scalar(select(EmailCode).where(EmailCode.user_id == user.id, EmailCode.purpose == purpose,
                                            EmailCode.confirmed_at.is_(None)).order_by(EmailCode.created_at.desc()))
    if row is None or row.expires_at <= clock.utcnow():
        raise AppError(400, "code_expired", "الرمز منتهي أو غير موجود. اطلب رمزًا جديدًا.")
    if email is not None and row.email != email.strip().lower():
        raise AppError(400, "code_invalid", "الرمز غير صحيح.")
    given = code.strip() if isinstance(code, str) else ""
    if not hmac.compare_digest(row.code_hash, _hash(settings, user.id, purpose, row.email, given)):
        row.attempts = (row.attempts or 0) + 1
        if row.attempts >= MAX_ATTEMPTS:
            db.delete(row)
            db.commit()
            raise AppError(400, "code_cancelled", "محاولات كثيرة: أُلغي الرمز. اطلب رمزًا جديدًا.")
        db.commit()
        raise AppError(400, "code_invalid", "الرمز غير صحيح.")
    row.confirmed_at = clock.utcnow()
    return row
