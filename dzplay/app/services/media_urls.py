"""Signed media URLs: short-lived and bound to the viewer's session (copying one elsewhere does not work)."""

from __future__ import annotations

import hashlib
import hmac

from app import clock
from app.config import Settings


def session_key(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:32]


def _media_sig(settings: Settings, asset_id: str, variant: str, exp: int, skey: str) -> str:
    msg = f"media:{asset_id}:{variant}:{exp}:{skey}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def media_url(settings: Settings, asset_id: str, variant: str, skey: str) -> str:
    # Expiry rounded to half the TTL so URLs stay identical for a while (better HTTP/SW caching).
    half = max(60, settings.MEDIA_URL_TTL // 2)
    exp = (int(clock.timestamp()) // half + 2) * half
    return f"/media/{asset_id}/{variant}?e={exp}&s={_media_sig(settings, asset_id, variant, exp, skey)}"


def verify_media_sig(settings: Settings, asset_id: str, variant: str, exp: int, sig: str, skey: str) -> bool:
    if exp < clock.timestamp() or exp > clock.timestamp() + settings.MEDIA_URL_TTL * 2:
        return False
    return hmac.compare_digest(_media_sig(settings, asset_id, variant, exp, skey), sig or "")
