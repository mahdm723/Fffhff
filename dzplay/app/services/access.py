"""V6 phase 14: who may open the app.

| client                                          | the app shell and its API                                    |
|-------------------------------------------------|--------------------------------------------------------------|
| our APK (valid dz_app cookie from attestation)  | yes                                                          |
| Android browser                                 | no: «حمّل التطبيق» page (APK download only); API → 403        |
| our APK too old, or without a valid cookie      | «حدّث التطبيق» page (every update is mandatory)               |
| iPhone / iPod / iPad (an iPad may say «Mac»:    | yes                                                          |
|   detected by touch → dz_touch cookie)          |                                                              |
| computer                                        | only if WEB_DESKTOP_ACCESS; else a «phone app» page          |

Always open: the panel (ADMIN_PATH), /download, /policies, static files, /.well-known, /api/app/* (the attestation
itself), the Telegram webhook, /healthz. ANDROID_APP_GATE off (panel) lets Android browsers in again — the safety
valve if attestation fails on phones in the field.

The dz_app cookie is a signed token (device, app version, expiry), issued after Key Attestation (first launch) or a
signature with the attested key (later launches). No user data in it.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.i18n import N_
from app.models import AppDevice, User, UserDevice

APP_COOKIE = "dz_app"
TOUCH_COOKIE = "dz_touch"
CHALLENGE_SECONDS = 300
_UA_APP = re.compile(r"DZPLAYApp/(\d+)\.(\d+)\.(\d+)")
_ANDROID = re.compile(r"Android", re.I)
_IOS = re.compile(r"iPhone|iPad|iPod", re.I)
_MAC = re.compile(r"Macintosh", re.I)

OPEN_PREFIXES = ("/download", "/policies", "/css/", "/js/", "/icons/", "/fonts/", "/brand/", "/.well-known/",
                 "/api/app/", "/api/telegram/webhook", "/api/i18n/", "/healthz", "/manifest.webmanifest", "/sw.js",
                 "/favicon", "/robots.txt", "/r/")
GATED_PAGES = ("/", "/index.html")


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(settings: Settings, *parts: str) -> str:
    msg = "|".join(parts).encode()
    return _b64(hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).digest()[:24])


# ---------------------------------------------------------------------------------------------- challenge


def new_challenge(settings: Settings) -> dict:
    """A one-time challenge for the app (stateless: its id carries it, signed, with an expiry)."""
    raw = secrets.token_bytes(32)
    exp = str(int(clock.timestamp()) + CHALLENGE_SECONDS)
    cid = f"{_b64(raw)}.{exp}.{_mac(settings, 'chal', _b64(raw), exp)}"
    return {"id": cid, "challenge": _b64(raw), "expires_in": CHALLENGE_SECONDS}


def use_challenge(settings: Settings, limiter, cid: object) -> bytes:
    """The challenge bytes of a valid, unexpired, never used id (else 400)."""
    from app.services.rate_limit import Limit

    try:
        b64, exp, mac = str(cid).split(".")
        ok = hmac.compare_digest(mac, _mac(settings, "chal", b64, exp)) and int(exp) >= clock.timestamp()
    except (ValueError, TypeError):
        ok = False
    if not ok:
        raise AppError(400, "challenge_invalid", N_("انتهت صلاحية التحقق. أعد المحاولة."))
    if not limiter.check_and_hit([Limit(f"app_challenge:{hashlib.sha256(b64.encode()).hexdigest()[:32]}", 1, 600)]).allowed:
        raise AppError(400, "challenge_invalid", N_("انتهت صلاحية التحقق. أعد المحاولة."))  # used already
    return _unb64(b64)


# ---------------------------------------------------------------------------------------------- the app token


def issue_token(settings: Settings, device_id: str, version_code: int) -> tuple[str, int]:
    max_age = max(1, int(settings.APP_TOKEN_HOURS)) * 3600
    exp = str(int(clock.timestamp()) + max_age)
    parts = (device_id, str(int(version_code)), exp)
    return "v1." + ".".join(parts) + "." + _mac(settings, "app", *parts), max_age


@dataclass
class AppToken:
    device_id: str
    version_code: int


def read_token(settings: Settings, value: str | None) -> AppToken | None:
    if not value or not value.startswith("v1."):
        return None
    try:
        _v, device_id, version, exp, mac = value.split(".")
        if not hmac.compare_digest(mac, _mac(settings, "app", device_id, version, exp)) or int(exp) < clock.timestamp():
            return None
        return AppToken(device_id, int(version))
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------------------------- who is asking


@dataclass
class Client:
    kind: str  # app | app_old | app_unverified | android | ios | ipad | desktop
    token: AppToken | None = None
    ua_version: int | None = None


def ua_version_code(ua: str) -> int | None:
    """DZPLAYApp/3.1.0 → 3*10000+1*100+0 — only to tell old apps from new ones before they prove themselves."""
    m = _UA_APP.search(ua or "")
    return int(m.group(1)) * 10000 + int(m.group(2)) * 100 + int(m.group(3)) if m else None


def classify(settings: Settings, ua: str, cookies: dict) -> Client:
    token = read_token(settings, cookies.get(APP_COOKIE))
    if token is not None:
        return Client("app", token)
    ua = ua or ""
    app_version = ua_version_code(ua)
    if app_version is not None:
        return Client("app_old" if app_version < version_floor() else "app_unverified", ua_version=app_version)
    if _ANDROID.search(ua):
        return Client("android")
    if _IOS.search(ua):
        return Client("ios")
    if _MAC.search(ua) and cookies.get(TOUCH_COOKIE) == "1":
        return Client("ipad")  # Safari on iPad says «Macintosh»: js/gate.js saw a touch screen
    return Client("desktop")


def version_floor() -> int:
    """The oldest app version (as DZPLAYApp/x.y.z) that knows how to prove itself: 3.2.0."""
    return 30200


def min_version_code() -> int:
    """Every update is mandatory: the version published on /download (static/download/version.json)."""
    from app.services import app_release

    info = app_release.info() or {}
    try:
        return int(info.get("min_version_code") or info.get("version_code") or 0)
    except (TypeError, ValueError):
        return 0


def decision(settings: Settings, client: Client) -> str | None:
    """None = let in; else the page to show: android | update | unverified | desktop."""
    if client.kind == "app":
        return "update" if client.token.version_code < min_version_code() else None
    if client.kind in ("ios", "ipad"):
        return None
    if client.kind == "desktop":
        return None if settings.WEB_DESKTOP_ACCESS else "desktop"
    if not settings.ANDROID_APP_GATE:  # the safety valve: Android browsers (and old apps) as before
        return None
    return {"app_old": "update", "app_unverified": "unverified"}.get(client.kind, "android")


def gated(path: str, admin_prefix: str) -> bool:
    """Does this path need an allowed client? (the app shell, its API, its WebSocket)"""
    if admin_prefix and (path == admin_prefix or path.startswith(admin_prefix + "/")):
        return False
    if path.startswith(OPEN_PREFIXES):
        return False
    return path in GATED_PAGES or path.startswith("/api/")


# ---------------------------------------------------------------------------------------------- devices


def register_device(db: Session, attested, version_code: int | None) -> AppDevice:
    from app.security.attestation import describe

    now = clock.utcnow()
    dev = db.scalar(select(AppDevice).where(AppDevice.key_id == attested.key_id))
    if dev is None:
        dev = AppDevice(key_id=attested.key_id, public_key=base64.b64encode(attested.public_key_der).decode(),
                        security_level=attested.description.security_level, created_at=now)
        db.add(dev)
    dev.details = describe(attested.description)
    dev.version_code = version_code or attested.package_version
    dev.last_seen_at, dev.revoked_at = now, None
    db.flush()
    return dev


def device_for_proof(db: Session, key_id: object) -> AppDevice | None:
    if not isinstance(key_id, str) or not re.fullmatch(r"[0-9a-f]{64}", key_id):
        return None
    dev = db.scalar(select(AppDevice).where(AppDevice.key_id == key_id))
    return dev if dev is not None and dev.revoked_at is None else None


def check_signup_device(db: Session, settings: Settings, device_id: str | None) -> None:
    """MAX_ACCOUNTS_PER_DEVICE accounts may be created from one attested phone (iPhone: e-mail + IP limits)."""
    if not device_id:
        return
    n = db.scalar(select(func.count()).select_from(User).where(User.registered_device_id == device_id)) or 0
    if n >= settings.MAX_ACCOUNTS_PER_DEVICE:
        raise AppError(403, "device_limit", N_("وصل هذا الجهاز إلى الحد الأقصى من الحسابات."))


def seen(db: Session, user: User, device_id: str | None, *, signup: bool = False) -> None:
    """Remember that this account used this phone (registration, every sign-in)."""
    if not device_id:
        return
    now = clock.utcnow()
    if signup:
        user.registered_device_id = device_id
    row = db.get(UserDevice, (user.id, device_id))
    if row is None:
        db.add(UserDevice(user_id=user.id, device_id=device_id, first_seen_at=now, last_seen_at=now))
    else:
        row.last_seen_at = now


def user_devices(db: Session, user: User) -> list[dict]:
    """For the panel: the phones of an account (security level, app version, first / last seen)."""
    user_id = user.id
    rows = db.execute(select(UserDevice, AppDevice).join(AppDevice, AppDevice.id == UserDevice.device_id)
                      .where(UserDevice.user_id == user_id).order_by(UserDevice.last_seen_at.desc())).all()
    out = []
    for link, dev in rows:
        accounts = db.scalar(select(func.count()).select_from(UserDevice).where(UserDevice.device_id == dev.id)) or 0
        out.append({"id": dev.id, "security_level": dev.security_level, "version_code": dev.version_code,
                    "suspicious": dev.suspicious_at is not None, "suspicious_reason": dev.suspicious_reason,
                    "details": dev.details, "first_seen": link.first_seen_at.isoformat() + "Z",
                    "last_seen": link.last_seen_at.isoformat() + "Z", "accounts": accounts,
                    "registered_here": dev.id == user.registered_device_id})
    return out
