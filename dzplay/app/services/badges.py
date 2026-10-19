"""V6 phase 17: the badges next to a name, computed when a payload is built (never stored as such).

* The blue star: verified_at is set (by an admin, the old V5 verification or the membership). A star that came with
  the membership (verified_by="membership") shows only while the membership feature «star» is on
  (MEMBER_FEATURE_STAR, panel): switching a feature off takes it from everyone at once; stars granted by hand stay.
* V6 phase 19: the gold badge — the official account only (users.is_official). It replaces the star.
"""

from __future__ import annotations

from app.config import Settings
from app.models import User

_settings: Settings | None = None  # the running app's settings (live: the panel's tunables are applied in place)


def install(settings: Settings) -> None:
    global _settings
    _settings = settings


def official(user: User | None) -> bool:
    return bool(user is not None and user.is_official)


def star(user: User | None) -> bool:
    if user is None or user.verified_at is None or user.is_official:
        return False
    if user.verified_by == "membership":
        from app.services.membership import feature_on

        return feature_on(_settings, "star")
    return True
