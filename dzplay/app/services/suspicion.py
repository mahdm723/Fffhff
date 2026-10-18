"""V6 phase 16: is this account (or one of its phones) suspicious? A suspicious account never gets an automatic
membership grant: its request waits for the owner, with the reasons shown.

Signals:
* the account or one of its attested phones was marked «مشبوه» by an admin;
* one of its phones is also used by a banned or suspended account;
* an open report or an open automatic content flag against the account;
* one of its phones has an unlocked bootloader or an unverified boot (Key Attestation details).

Sharing a network / IP address is never a signal: mobile carriers put many people behind one address.
The reasons are the owner's (panel, Telegram): Arabic.
"""

from __future__ import annotations

import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.errors import AppError, not_found
from app.models import AppDevice, ContentFlag, Report, User, UserDevice
from app.services import audit


def _devices(db: Session, user: User) -> list[AppDevice]:
    return list(db.scalars(select(AppDevice).join(UserDevice, UserDevice.device_id == AppDevice.id)
                           .where(UserDevice.user_id == user.id)))


def _weak(device: AppDevice) -> bool:
    try:
        d = json.loads(device.details or "{}")
    except ValueError:
        return False
    return d.get("locked") is False or (d.get("boot") not in (None, "verified"))


def reasons(db: Session, user: User) -> list[str]:
    out: list[str] = []
    if user.suspicious_at is not None:
        out.append("الحساب معلَّم كمشبوه" + (f": {user.suspicious_reason}" if user.suspicious_reason else ""))
    for dev in _devices(db, user):
        if dev.suspicious_at is not None:
            out.append("جهاز الحساب معلَّم كمشبوه" + (f": {dev.suspicious_reason}" if dev.suspicious_reason else ""))
        bad = db.scalar(select(func.count()).select_from(UserDevice).join(User, User.id == UserDevice.user_id).where(
            UserDevice.device_id == dev.id, UserDevice.user_id != user.id, User.status.in_(("banned", "suspended")))) or 0
        if bad:
            out.append("الجهاز نفسه مستعمل في حساب محظور أو موقوف")
        if _weak(dev):
            out.append("جهاز بقفل إقلاع مفتوح أو إقلاع غير موثّق")
    if db.scalar(select(func.count()).select_from(Report).where(Report.reported_user_id == user.id,
                                                                Report.status == "open")):
        out.append("بلاغ مفتوح ضد الحساب")
    if db.scalar(select(func.count()).select_from(ContentFlag).where(ContentFlag.offender_id == user.id,
                                                                     ContentFlag.status == "open")):
        out.append("رصد آلي مفتوح على محتوى الحساب")
    return list(dict.fromkeys(out))


def mark_user(db: Session, user_id: str, on: bool, reason: str, actor: str) -> dict:
    user = db.get(User, str(user_id)[:32])
    if user is None:
        raise not_found()
    reason = (reason or "").strip()[:255]
    if on and len(reason) < 3:
        raise AppError(400, "reason_required", "اكتب السبب (3 أحرف على الأقل).")
    user.suspicious_at, user.suspicious_reason, user.suspicious_by = (clock.utcnow(), reason, actor[:80]) if on else (None, None, None)
    audit.record(db, actor, "user_suspicious" if on else "user_unsuspicious", target_type="user", target_id=user.public_id,
                 reason=reason or None)
    return {"suspicious": on, "reasons": reasons(db, user)}


def mark_device(db: Session, device_id: str, on: bool, reason: str, actor: str) -> dict:
    dev = db.get(AppDevice, str(device_id)[:32])
    if dev is None:
        raise not_found()
    reason = (reason or "").strip()[:255]
    if on and len(reason) < 3:
        raise AppError(400, "reason_required", "اكتب السبب (3 أحرف على الأقل).")
    dev.suspicious_at, dev.suspicious_reason = (clock.utcnow(), reason) if on else (None, None)
    audit.record(db, actor, "device_suspicious" if on else "device_unsuspicious", target_type="device", target_id=dev.id,
                 reason=reason or None)
    return {"suspicious": on}
