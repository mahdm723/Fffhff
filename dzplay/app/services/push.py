"""Web Push notifications (optional; enabled when VAPID keys are configured).

The payload never contains the message text or anything about the sender:
the lock screen only shows "لديك رسالة جديدة على <APP_NAME>".
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit

from sqlalchemy import delete, select

from app.config import Settings
from app.db import Database
from app.models import PushSubscription

log = logging.getLogger("dzplay.push")

def push_payload(settings: Settings) -> dict:
    """The only content a push carries: the app's name and a generic line (no sender, no text)."""
    return {"title": settings.APP_NAME, "body": f"لديك رسالة جديدة على {settings.APP_NAME}", "url": "/#/messages"}


def push_endpoint_allowed(settings: Settings, endpoint: str) -> bool:
    """Only https URLs on a known browser push service (no internal hosts, ports or credentials: SSRF guard)."""
    try:
        u = urlsplit(endpoint)
        port = u.port
    except ValueError:
        return False
    host = (u.hostname or "").lower()
    if u.scheme != "https" or u.username or u.password or port not in (None, 443) or not host:
        return False
    allowed = [h.strip().lower() for h in settings.PUSH_ALLOWED_HOSTS.split(",") if h.strip()]
    return any(host == h or host.endswith("." + h) for h in allowed)


class PushNotifier:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="push") if settings.push_enabled else None

    @property
    def enabled(self) -> bool:
        return self._pool is not None

    def notify_user(self, user_id: str) -> None:
        if self._pool is not None:
            self._pool.submit(self._send, user_id)

    def _send(self, user_id: str, payload: dict | None = None, ttl: int = 24 * 3600, urgency: str = "normal") -> None:
        from pywebpush import WebPushException, webpush

        with self.database.session() as db:
            subs = db.execute(select(PushSubscription).where(PushSubscription.user_id == user_id)).scalars().all()
            dead: list[int] = []
            for sub in subs:
                if not push_endpoint_allowed(self.settings, sub.endpoint):  # saved before the SSRF guard existed
                    dead.append(sub.id)
                    continue
                try:
                    webpush(
                        subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                        data=json.dumps(payload or push_payload(self.settings), ensure_ascii=False),
                        vapid_private_key=self.settings.VAPID_PRIVATE_KEY,
                        vapid_claims={"sub": self.settings.VAPID_SUBJECT},
                        ttl=ttl,
                        headers={"Urgency": urgency},
                        timeout=10,
                    )
                except WebPushException as exc:
                    status = getattr(exc.response, "status_code", None)
                    if status in (404, 410):
                        dead.append(sub.id)
                    else:
                        log.warning("web push failed (%s)", status)
                except Exception:  # noqa: BLE001
                    log.exception("web push error")
            if dead:
                db.execute(delete(PushSubscription).where(PushSubscription.id.in_(dead)))

    def shutdown(self) -> None:
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
