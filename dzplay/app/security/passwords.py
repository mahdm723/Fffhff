"""Password hashing with Argon2id (memory-hard, OWASP recommended).

V6 phase 21 — a login or sign-up wave must never take the server down. Each hash holds 64 MiB for its whole run
(measured: 40 at once = 2.5 GB, enough to kill the app), and two CPU cores finish about 8 per second however many
start. So, per worker:
* at most PASSWORD_HASH_CONCURRENCY hashes run at once (default: the CPUs shared between the workers) — memory peak
  = that × 64 MiB;
* at most PASSWORD_HASH_QUEUE more wait, each for PASSWORD_HASH_WAIT_SECONDS at most;
* anything beyond is answered at once 503 "busy, try again in a moment" (Retry-After) — no thread, no memory used;
* the hashes themselves run on `slots` threads of their own at a low CPU priority (nice HASH_NICE; Argon2's lane threads
  inherit it): one hash keeps more than a core busy (4 lanes; 1.1–1.3 measured), so at normal priority a wave took the
  whole CPU from browsing (measured: read p95 from ~5 s to ~29 s). Now browsing and messages come first and hashing
  gets the CPU they leave.
So the waiting logins hold only a few of the worker's threads and little CPU: browsing and messages keep the rest.
The Argon2 parameters stay the library's (RFC 9106: 64 MiB, 3 passes, 4 lanes — above OWASP's minimum).
"""

from __future__ import annotations

import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.errors import AppError

_hasher = PasswordHasher()  # argon2id, library defaults (RFC 9106 low-memory profile)
# Used to spend the same time when the account does not exist (no user enumeration by timing).
_DUMMY_HASH = _hasher.hash("dzplay-dummy-password")
BUSY = "الخادم مشغول الآن. أعد المحاولة بعد لحظات."
RETRY_AFTER = 3  # seconds suggested to the app
HASH_NICE = 10  # CPU priority of the hashing threads (0 = normal, 19 = lowest)


def _lower_priority() -> None:
    """In each hashing thread, once: Linux keeps a nice value per thread, and new threads inherit it."""
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), HASH_NICE)
    except (AttributeError, OSError):  # not Linux, or already lower: the thread keeps its priority
        pass


class _Gate:
    """`slots` hashes at once; `queue` more may wait `wait` seconds; the rest are refused immediately."""

    def __init__(self, slots: int, queue: int, wait: float) -> None:
        self.slots, self.queue, self.wait = slots, queue, wait
        self._sem = threading.BoundedSemaphore(slots)
        self._lock = threading.Lock()
        self.inside = 0  # running + waiting
        self.refused = 0  # since start (for «صحة النظام» / tests)
        self._pool = ThreadPoolExecutor(max_workers=slots, thread_name_prefix="argon2", initializer=_lower_priority)

    def __enter__(self) -> None:
        with self._lock:
            if self.inside >= self.slots + self.queue:
                self.refused += 1
                raise AppError(503, "busy", BUSY, retry_after=RETRY_AFTER)
            self.inside += 1
        if not self._sem.acquire(timeout=self.wait):
            with self._lock:
                self.inside -= 1
                self.refused += 1
            raise AppError(503, "busy", BUSY, retry_after=RETRY_AFTER)

    def __exit__(self, *exc) -> None:
        self._sem.release()
        with self._lock:
            self.inside -= 1

    def run(self, fn, *args):
        """fn(*args) in a slot, on a low-priority hashing thread (one per slot: it never waits there)."""
        with self:
            return self._pool.submit(fn, *args).result()

    def close(self) -> None:
        self._pool.shutdown(wait=False)


def _cpus() -> int:
    """The CPUs this process may run on (a container pinned to 2 cores of a bigger host: 2, not the host's count)."""
    try:
        return len(os.sched_getaffinity(0)) or 2
    except (AttributeError, OSError):  # not on Linux
        return os.cpu_count() or 2


def default_slots(workers: int = 1) -> int:
    """The CPUs shared between the worker processes (at least 1 per worker)."""
    return max(1, math.ceil(_cpus() / max(1, workers)))


_gate = _Gate(default_slots(), 4 * default_slots(), 3.0)


def configure(concurrency: int = 0, queue: int = -1, wait: float = 3.0, workers: int = 1) -> None:
    """At start (main.create_app). concurrency 0 = default_slots(workers); queue -1 = 4 × concurrency."""
    global _gate
    slots = concurrency if concurrency > 0 else default_slots(workers)
    old, _gate = _gate, _Gate(slots, queue if queue >= 0 else 4 * slots, max(0.0, wait))
    old.close()


def gate() -> _Gate:
    return _gate


def hash_password(password: str) -> str:
    return _gate.run(_hasher.hash, password)


def _verify(password_hash: str | None, password: str) -> bool:
    try:
        return _hasher.verify(password_hash or _DUMMY_HASH, password) and password_hash is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def verify_password(password_hash: str | None, password: str) -> bool:
    return _gate.run(_verify, password_hash, password)


def needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)
