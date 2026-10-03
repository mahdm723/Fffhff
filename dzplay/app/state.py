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
    telegram_secret: str = ""  # X-Telegram-Bot-Api-Secret-Token expected on the webhook
    telegram_source: str = "none"  # panel | env | none
    telegram_transport: object | None = None  # tests only (httpx.MockTransport)

    @classmethod
    def build(cls, settings: Settings, telegram_transport=None) -> "AppState":
        database = Database(settings.DATABASE_URL)
        limiter = make_rate_limiter(settings.REDIS_URL)
        holder: dict = {}

        def telegram_factory():  # follows reconfiguration from the admin panel
            telegram = holder["state"].telegram
            if telegram is None:
                from app.services.telegram import TelegramError

                raise TelegramError("Telegram bot is not configured")
            return telegram

        media = MediaStore(settings, database, telegram_factory)
        state = cls(settings=settings, database=database, limiter=limiter, hub=Hub(settings.REDIS_URL),
                    push=PushNotifier(settings, database), media=media, telegram_transport=telegram_transport)
        holder["state"] = state
        if settings.telegram_enabled:  # .env values; panel values are applied at startup (load_runtime_config)
            state.configure_telegram(settings.TELEGRAM_BOT_TOKEN, settings.TELEGRAM_ADMIN_CHAT_ID,
                                     settings.TELEGRAM_WEBHOOK_SECRET, "env")
        return state

    def configure_telegram(self, token: str | None, chat_id: str = "", secret: str = "", source: str = "none") -> None:
        """(Re)build the Telegram client + bot live; token None disables the bot.

        The app runs one worker: other workers (if you add some) pick a change up on restart.
        """
        from app.services import password_reset
        from app.services.telegram import TelegramClient
        from app.services.telegram_bot import BotService

        old = self.bot
        self.bot, self.telegram = None, None
        if old is not None:
            old.shutdown()
        self.telegram_secret, self.telegram_source = (secret or "", source) if token else ("", "none")
        if not token:
            return
        bot_settings = self.settings.model_copy(update={
            "TELEGRAM_BOT_TOKEN": token, "TELEGRAM_ADMIN_CHAT_ID": str(chat_id), "TELEGRAM_WEBHOOK_SECRET": secret or ""})
        self.telegram = TelegramClient(bot_settings, transport=self.telegram_transport)
        self.bot = BotService(bot_settings, self.database, self.telegram, self.media, self.limiter)
        password_reset.install(self.bot)

    def load_runtime_config(self) -> None:
        """Apply bot settings saved from the admin panel (they take precedence over .env)."""
        from app.services import runtime_config

        with self.database.session() as db:
            cfg = runtime_config.telegram_config(db, self.settings)
        if cfg["source"] == "panel":
            self.configure_telegram(cfg["token"], cfg["chat_id"], cfg["secret"], "panel")

    def dispatch(self, effects: Effects) -> None:
        """Fire realtime signals / push after a successful commit."""
        for user_ids, reason in effects.signals:
            self.hub.notify(user_ids, {"type": "sync", "reason": reason})
        for user_id in dict.fromkeys(effects.push_to):
            if not self.hub.is_online(user_id):
                self.push.notify_user(user_id)
