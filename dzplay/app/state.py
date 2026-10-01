from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import Database
from app.services.messaging import Effects
from app.services.push import PushNotifier
from app.services.realtime import Hub
from app.services.rate_limit import make_rate_limiter


@dataclass
class AppState:
    settings: Settings
    database: Database
    limiter: object
    hub: Hub
    push: PushNotifier

    @classmethod
    def build(cls, settings: Settings) -> "AppState":
        database = Database(settings.DATABASE_URL)
        return cls(
            settings=settings,
            database=database,
            limiter=make_rate_limiter(settings.REDIS_URL),
            hub=Hub(settings.REDIS_URL),
            push=PushNotifier(settings, database),
        )

    def dispatch(self, effects: Effects) -> None:
        """Fire realtime signals / push after a successful commit."""
        for user_ids, reason in effects.signals:
            self.hub.notify(user_ids, {"type": "sync", "reason": reason})
        for user_id in dict.fromkeys(effects.push_to):
            if not self.hub.is_online(user_id):
                self.push.notify_user(user_id)
