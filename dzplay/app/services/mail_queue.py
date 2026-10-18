"""V6 phase 18: the e-mails that carry a code (signup, password reset) leave from this queue, never from the request.

The answer to «إنشاء الحساب» / «نسيت كلمة السر» then takes the same time whether the address exists or not, whatever
the mail server does. One worker thread and a bounded in-memory queue: the codes never rest in the database (a
restart loses what was waiting; the person presses «إعادة الإرسال»). Each job handles its own failure (the manual
path in Telegram + an alert to the owner).
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("dzplay.mail_queue")


class MailQueue:
    def __init__(self, limit: int = 500):
        self.limit = limit
        self._pool = ThreadPoolExecutor(1, thread_name_prefix="mail-queue")
        self._lock = threading.Lock()
        self._pending = 0

    def submit(self, fn, *args) -> bool:
        """Queue fn(*args) and return at once (False when the queue is full: the caller says «try later»)."""
        with self._lock:
            if self._pending >= self.limit:
                log.warning("mail queue full (%s jobs): job refused", self._pending)
                return False
            self._pending += 1
        self._pool.submit(self._run, fn, args)
        return True

    def _run(self, fn, args) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - one job never stops the queue (its own fallback already ran or failed)
            log.exception("mail job failed")
        finally:
            with self._lock:
                self._pending -= 1

    def wait_idle(self, timeout: float = 30.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending == 0:
                    return
            time.sleep(0.02)
        raise TimeoutError("mail queue still busy")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
