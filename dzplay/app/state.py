from __future__ import annotations

from dataclasses import dataclass

from app.config import Settings
from app.db import Database
from app.services.media import MediaStore
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
    media: MediaStore
    telegram: object | None = None  # TelegramClient when the bot is configured
    bot: object | None = None  # BotService when the bot is configured

    @classmethod
    def build(cls, settings: Settings, telegram_transport=None) -> "AppState":
        database = Database(settings.DATABASE_URL)
        limiter = make_rate_limiter(settings.REDIS_URL)
        telegram = None
        if settings.telegram_enabled:
            from app.services.telegram import TelegramClient

            telegram = TelegramClient(settings, transport=telegram_transport)

        def telegram_factory():
            if telegram is None:
                from app.services.telegram import TelegramError

                raise TelegramError("Telegram bot is not configured")
            return telegram

        media = MediaStore(settings, database, telegram_factory)
        state = cls(settings=settings, database=database, limiter=limiter, hub=Hub(settings.REDIS_URL),
                    push=PushNotifier(settings, database), media=media, telegram=telegram)
        if telegram is not None:
            from app.services.telegram_bot import BotService

            from app.services import password_reset

            state.bot = BotService(settings, database, telegram, media, limiter)
            password_reset.install(state.bot)
        return state

    def dispatch(self, effects: Effects) -> None:
        """Fire realtime signals / push after a successful commit."""
        for user_ids, reason in effects.signals:
            self.hub.notify(user_ids, {"type": "sync", "reason": reason})
        for user_id in dict.fromkeys(effects.push_to):
            if not self.hub.is_online(user_id):
                self.push.notify_user(user_id)
