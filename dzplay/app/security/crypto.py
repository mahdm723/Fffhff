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
