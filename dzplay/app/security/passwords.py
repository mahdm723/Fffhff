"""Password hashing with Argon2id (memory-hard, OWASP recommended).

V6 phase 21: each hash takes 64 MiB and a CPU core for ~0.1 s. At most PASSWORD_HASH_CONCURRENCY run at once per worker
(default: the number of CPUs); a login or sign-up wave waits its turn instead of exhausting the memory, and after
HASH_WAIT_SECONDS of waiting the request is answered "busy, try again" (503) rather than piling up.
"""

from __future__ import annotations

import os
import threading

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.errors import AppError

_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
# Used to spend the same time when the account does not exist (no user enumeration by timing).
_DUMMY_HASH = _hasher.hash("dzplay-dummy-password")
HASH_WAIT_SECONDS = 20
_slots = threading.BoundedSemaphore(os.cpu_count() or 2)


def configure(concurrency: int) -> None:
    """At start (main.create_app): hashes at once in this worker; 0 = the number of CPUs."""
    global _slots
    _slots = threading.BoundedSemaphore(max(1, concurrency or os.cpu_count() or 2))


class _Slot:
    def __enter__(self) -> None:
        self.sem = _slots
        if not self.sem.acquire(timeout=HASH_WAIT_SECONDS):
            raise AppError(503, "busy", "الخادم مشغول الآن. أعد المحاولة بعد لحظات.")

    def __exit__(self, *exc) -> None:
        self.sem.release()


def hash_password(password: str) -> str:
    with _Slot():
        return _hasher.hash(password)


def verify_password(password_hash: str | None, password: str) -> bool:
    with _Slot():
        try:
            return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            return False


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
