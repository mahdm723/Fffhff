"""V6 phase 8: the app's name (APP_NAME) for code that has no settings object at hand.

During a request, the settings bound by api.deps.current_user (media_items.bind_request) are used; elsewhere the
default. Prefer settings.APP_NAME wherever settings are available."""

from __future__ import annotations

from app.config import Settings


def app_name() -> str:
    from app.services.media_items import _settings

    s = _settings()
    return s.APP_NAME if s is not None else Settings.model_fields["APP_NAME"].default


def team_name() -> str:
    return f"فريق {app_name()}"


def official_name() -> str:
    return f"{app_name()} الرسمي"
