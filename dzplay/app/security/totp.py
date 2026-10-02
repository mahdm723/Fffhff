"""TOTP (RFC 6238) for admin 2FA, plus encryption of the stored secrets.

Compatible with Google Authenticator, Microsoft Authenticator, Aegis, 2FAS…
(SHA-1, 6 digits, 30-second steps). Secrets are stored encrypted with a key
derived from SECRET_KEY, so a database dump alone does not reveal them.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken

STEP = 30
DIGITS = 6


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def _key(secret: str) -> bytes:
    padded = secret.upper() + "=" * (-len(secret) % 8)
    return base64.b32decode(padded)


def code_at(secret: str, step: int) -> str:
    digest = hmac.new(_key(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10**DIGITS).zfill(DIGITS)


def current_step(now_ts: float) -> int:
    return int(now_ts // STEP)


def matching_step(secret: str, code: str, now_ts: float, window: int = 1) -> int | None:
    """The time step the code belongs to (±window steps for clock drift), or None."""
    code = (code or "").strip().replace(" ", "")
    if len(code) != DIGITS or not code.isdigit():
        return None
    now = current_step(now_ts)
    found = None
    for step in range(now - window, now + window + 1):
        if hmac.compare_digest(code_at(secret, step), code):
            found = step  # keep looping: constant work whatever matches
    return found


def provisioning_uri(secret: str, account: str, issuer: str = "DZPLAY") -> str:
    return (f"otpauth://totp/{quote(issuer)}:{quote(account)}?secret={secret}&issuer={quote(issuer)}"
            f"&algorithm=SHA1&digits={DIGITS}&period={STEP}")


def _fernet(secret_key: str) -> Fernet:
    raw = hashlib.sha256(f"dzplay-totp:{secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def encrypt_secret(secret_key: str, totp_secret: str) -> str:
    return _fernet(secret_key).encrypt(totp_secret.encode()).decode()


def decrypt_secret(secret_key: str, token: str) -> str | None:
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
