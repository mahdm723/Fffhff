"""Keyed hashing helpers.

IP addresses and e-mails used for throttling are stored only as HMACs keyed
with SECRET_KEY: blocks keep working, but the database never contains raw IPs.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets


def keyed_hash(secret: str, purpose: str, value: str) -> str:
    return hmac.new(secret.encode(), f"{purpose}:{value}".encode(), hashlib.sha256).hexdigest()


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def _fernet(secret_key: str, purpose: str):
    import base64

    from cryptography.fernet import Fernet

    raw = hashlib.sha256(f"dzplay-{purpose}:{secret_key}".encode()).digest()
    return Fernet(base64.urlsafe_b64encode(raw))


def seal(secret_key: str, purpose: str, text: str) -> str:
    """Encrypt a secret for storage (authenticated; key derived from SECRET_KEY and a purpose label)."""
    return _fernet(secret_key, purpose).encrypt(text.encode()).decode()


def unseal(secret_key: str, purpose: str, token: str) -> str | None:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(secret_key, purpose).decrypt(token.encode()).decode()
    except (InvalidToken, ValueError):
        return None
