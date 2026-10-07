"""V6 membership (features only: blue star, picture posts, chat pictures — never any return or earning).

Phase 4 needs only `is_member`; payments, refunds and the admin side arrive in phase 5."""

from __future__ import annotations

from app.models import User


def is_member(user: User | None) -> bool:
    return bool(user is not None and user.member_since is not None and user.member_ended_at is None)
