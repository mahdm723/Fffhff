"""Optional Firebase Cloud Messaging (HTTP v1) for the Android app.

Rings the app when it is closed ("مكالمة واردة على DZPLAY", full-screen on the phone) and
shows a new-message alert. Data-only messages with no sender, name or text in them: the
app fetches the details itself after it opens. Enabled only when FCM_SERVICE_ACCOUNT_FILE
points to a Firebase service-account JSON (kept outside the repository).
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete, select

from app.config import Settings
from app.db import Database
from app.models import FcmToken

log = logging.getLogger("dzplay.fcm")
SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def call_message(token: str, call_id: str, ttl: int) -> dict:
    return {"message": {"token": token, "data": {"type": "call", "call_id": call_id},
                        "android": {"priority": "HIGH", "ttl": f"{max(5, ttl)}s"}}}


def chat_message(token: str) -> dict:
    return {"message": {"token": token, "data": {"type": "message"},
                        "android": {"priority": "HIGH", "ttl": "86400s", "collapse_key": "dz-message"}}}


class FcmNotifier:
    def __init__(self, settings: Settings, database: Database) -> None:
        self.settings = settings
        self.database = database
        with open(settings.FCM_SERVICE_ACCOUNT_FILE, encoding="utf-8") as f:
            info = json.load(f)
        self.project_id = settings.FCM_PROJECT_ID or info.get("project_id", "")
        from google.oauth2 import service_account

        self._creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="fcm")

    def notify_call(self, user_id: str, call_id: str, ttl: int) -> None:
        self._pool.submit(self._send, user_id, lambda t: call_message(t, call_id, ttl))

    def notify_message(self, user_id: str) -> None:
        self._pool.submit(self._send, user_id, chat_message)

    def _token(self) -> str:
        import google.auth.transport.requests

        if not self._creds.valid:
            self._creds.refresh(google.auth.transport.requests.Request())
        return self._creds.token

    def _send(self, user_id: str, build) -> None:
        import requests

        try:
            with self.database.session() as db:
                tokens = list(db.execute(select(FcmToken.token).where(FcmToken.user_id == user_id)).scalars())
                if not tokens:
                    return
                url = f"https://fcm.googleapis.com/v1/projects/{self.project_id}/messages:send"
                headers = {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}
                dead = []
                for t in tokens:
                    r = requests.post(url, headers=headers, json=build(t), timeout=10)
                    if r.status_code == 404 or "UNREGISTERED" in r.text:  # app uninstalled / token rotated
                        dead.append(t)
                    elif r.status_code >= 300:
                        log.warning("fcm send failed (%s)", r.status_code)
                if dead:
                    db.execute(delete(FcmToken).where(FcmToken.token.in_(dead)))
        except Exception:  # noqa: BLE001
            log.exception("fcm error")

    def shutdown(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)
