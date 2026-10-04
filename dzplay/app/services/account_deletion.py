"""V5 «حذف حسابي»: a user deletes their own account.

Deleted at once: the account, sessions, profile, ideas (with their comments and reactions), comments,
reactions, conversations and messages, support tickets, blue-star requests, creator reels, earnings
records and payout e-mail, notification tokens; pictures/videos leave the cache and the Telegram storage
channel. Kept: reported media as evidence (up to MEDIA_EVIDENCE_RETENTION_DAYS), reports others filed
about this account (until REPORT_RETENTION), and the audit log line saying an account was deleted
(public ID only).
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import User
from app.security.passwords import verify_password
from app.services import admin_access, audit
from app.services.messaging import Effects

CONFIRM_TEXT = "حذف حسابي"


def delete_own(db: Session, user: User, *, password: object, confirm: object, effects: Effects, store=None) -> None:
    if user.is_official or user.is_system:
        raise AppError(400, "protected", "لا يمكن حذف هذا الحساب.")
    if user.password_hash:
        if not isinstance(password, str) or not verify_password(user.password_hash, password):
            raise AppError(403, "wrong_password", "كلمة المرور غير صحيحة.")
    elif not isinstance(confirm, str) or confirm.strip() != CONFIRM_TEXT:  # Google-only accounts
        raise AppError(400, "confirm_required", f"اكتب «{CONFIRM_TEXT}» للتأكيد.")
    public_id = user.public_id
    admin_access.delete_account(db, user.id, effects, store)
    audit.record(db, "user", "account_self_delete", target_type="user", target_id=public_id)
