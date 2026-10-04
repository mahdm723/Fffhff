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
    fcm: object | None = None  # FcmNotifier when FCM_SERVICE_ACCOUNT_FILE is set (incoming calls to the Android app)
    pipeline: object | None = None  # V5 MediaPipeline (user uploads -> worker -> Telegram storage)

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
        from app.services import media_items
        from app.services.media_pipeline import MediaPipeline

        state.pipeline = MediaPipeline(state)
        media_items.install(media)
        state.hub.system_handler = state.on_system_event
        if settings.fcm_enabled:
            from app.services.fcm import FcmNotifier

            try:
                state.fcm = FcmNotifier(settings, database)
            except Exception:  # noqa: BLE001 - a broken key file must not stop the app
                import logging

                logging.getLogger("dzplay.fcm").exception("FCM disabled: cannot load FCM_SERVICE_ACCOUNT_FILE")
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
        from app.services import media_moderation

        media_moderation.install(self)

    def load_runtime_config(self) -> None:
        """Apply bot settings saved from the admin panel (they take precedence over .env)."""
        from app.services import runtime_config

        self.reload_tunables()
        with self.database.session() as db:
            cfg = runtime_config.telegram_config(db, self.settings)
        if cfg["source"] == "panel":
            self.configure_telegram(cfg["token"], cfg["chat_id"], cfg["secret"], "panel")

    def reload_tunables(self) -> None:
        """Limits/conditions saved from the panel (V5): applied live, here and in the bot's settings copy."""
        from app.services import tunables

        with self.database.session() as db:
            tunables.apply(db, self.settings, self.bot.settings if self.bot is not None else None)

    def on_system_event(self, event: dict) -> None:
        if event.get("type") == "tunables":
            self.reload_tunables()

    def dispatch(self, effects: Effects) -> None:
        """Fire realtime signals / push after a successful commit."""
        for user_ids, reason in effects.signals:
            self.hub.notify(user_ids, {"type": "sync", "reason": reason})
        for user_ids, event in effects.events:
            self.hub.notify(user_ids, event)
        for user_id, _call_id in effects.call_push:
            if not self.hub.is_online(user_id):
                self.push.notify_call(user_id, self.settings.CALL_RING_TIMEOUT)
                if self.fcm is not None:
                    self.fcm.notify_call(user_id, _call_id, self.settings.CALL_RING_TIMEOUT)
        for fn, args in effects.tasks:  # V5: Telegram calls etc., after the commit, off the request thread
            self.pipeline.background(fn, self, *args)
        for user_id in dict.fromkeys(effects.push_to):
            if not self.hub.is_online(user_id):
                self.push.notify_user(user_id)
                if self.fcm is not None:
                    self.fcm.notify_message(user_id)
