"""Self-hosted anti-bot challenge (proof-of-work, ALTCHA-style).

The server picks a secret number n in [0, max_number] and publishes
sha256(salt + n). The browser must find n by brute force (a few hundred ms on a
phone for the default difficulty), which makes scripted mass registration and
password guessing expensive, without third-party trackers or puzzles.

The challenge is HMAC-signed (stateless issuance), bound to a purpose and an
expiry, and can be redeemed only once (stored in `used_challenges`).
Difficulty is raised for IPs with recent failures ("CAPTCHA when needed").
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.errors import AppError
from app.models import UsedChallenge

PURPOSES = ("register", "login")


@dataclass
class Challenge:
    algorithm: str
    challenge: str
    salt: str
    maxnumber: int
    signature: str

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _sign(secret: str, challenge: str) -> str:
    return hmac.new(secret.encode(), challenge.encode(), hashlib.sha256).hexdigest()


def create_challenge(secret: str, purpose: str, max_number: int, ttl: int) -> Challenge:
    expires = int(clock.timestamp()) + ttl
    salt = f"{secrets.token_hex(12)}?expires={expires}&purpose={purpose}"
    number = secrets.randbelow(max_number + 1)
    challenge = hashlib.sha256(f"{salt}{number}".encode()).hexdigest()
    return Challenge("SHA-256", challenge, salt, max_number, _sign(secret, challenge))


def solve(challenge: dict) -> int | None:
    """Reference solver (used by tests and the CLI)."""
    salt, target = challenge["salt"], challenge["challenge"]
    for n in range(challenge["maxnumber"] + 1):
        if hashlib.sha256(f"{salt}{n}".encode()).hexdigest() == target:
            return n
    return None


_INVALID = "فشل التحقق من أنك لست روبوتًا. أعد المحاولة."


def verify_solution(db: Session, secret: str, purpose: str, payload: dict | None, min_max_number: int) -> None:
    """Raise AppError unless `payload` is a valid, fresh, unused solution."""
    if not isinstance(payload, dict):
        raise AppError(400, "antibot_required", "يرجى إكمال التحقق من أنك لست روبوتًا.")
    try:
        challenge = str(payload["challenge"])
        salt = str(payload["salt"])
        signature = str(payload["signature"])
        number = int(payload["number"])
    except (KeyError, TypeError, ValueError):
        raise AppError(400, "antibot_invalid", _INVALID) from None

    if not hmac.compare_digest(_sign(secret, challenge), signature):
        raise AppError(400, "antibot_invalid", _INVALID)
    params = parse_qs(salt.split("?", 1)[1] if "?" in salt else "")
    try:
        expires = int(params["expires"][0])
        challenge_purpose = params["purpose"][0]
    except (KeyError, IndexError, ValueError):
        raise AppError(400, "antibot_invalid", _INVALID) from None
    if challenge_purpose != purpose:
        raise AppError(400, "antibot_invalid", _INVALID)
    if expires < clock.timestamp():
        raise AppError(400, "antibot_expired", "انتهت صلاحية التحقق. أعد المحاولة.")
    if hashlib.sha256(f"{salt}{number}".encode()).hexdigest() != challenge:
        raise AppError(400, "antibot_invalid", _INVALID)
    # The issued difficulty must be at least what is currently required for
    # this client (a bot cannot reuse an "easy" challenge after failures).
    max_number = int(payload.get("maxnumber", 0) or 0)
    if max_number < min_max_number or number > max_number:
        raise AppError(400, "antibot_invalid", _INVALID)

    # One-time use. Inserted in a SAVEPOINT so a replay does not poison the caller's transaction.
    try:
        with db.begin_nested():
            expires_at = datetime.fromtimestamp(expires, timezone.utc).replace(tzinfo=None) + timedelta(seconds=1)
            db.add(UsedChallenge(challenge=challenge, expires_at=expires_at))
    except IntegrityError:
        raise AppError(400, "antibot_used", _INVALID) from None
