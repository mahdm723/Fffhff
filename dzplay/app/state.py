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
    chain_transport: object | None = None  # tests only: the TronGrid / BSC answers (httpx.MockTransport)
    _chain: object | None = None
    attestation_roots: set | None = None  # V6 phase 14: None = Google's roots (tests put their own)
    _revocations: object | None = None
    fcm: object | None = None  # FcmNotifier when FCM_SERVICE_ACCOUNT_FILE is set (new-message alerts in the Android app)
    pipeline: object | None = None  # V5 MediaPipeline (user uploads -> worker -> Telegram storage)
    mail_queue: object | None = None  # V6 phase 18: MailQueue (code e-mails, off the request path)

    @classmethod
    def build(cls, settings: Settings, telegram_transport=None, chain_transport=None) -> "AppState":
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
                    push=PushNotifier(settings, database), media=media, telegram_transport=telegram_transport,
                    chain_transport=chain_transport)
        holder["state"] = state
        from app.services import media_items
        from app.services.media_pipeline import MediaPipeline

        state.pipeline = MediaPipeline(state)
        from app.services.mail_queue import MailQueue

        state.mail_queue = MailQueue()  # V6 phase 18: code e-mails never leave from the request
        media_items.install(media)
        from app.services import badges

        badges.install(settings)  # V6 phase 17: the star follows the membership features switched in the panel
        from app.services import mail

        mail.install(state)  # V6 phase 7: failed deliveries are reported to the admin chat
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

    def chain_reader(self):
        """V6 phase 13: the blockchain reader (membership payments); it reads the live settings on every call."""
        if self._chain is None:
            from app.services.chain import ChainReader

            self._chain = ChainReader(self.settings, transport=self.chain_transport)
        return self._chain

    def trusted_roots(self) -> set:
        """V6 phase 14: the public keys an attestation chain must end at (Google's hardware attestation roots)."""
        if self.attestation_roots is None:
            from app.security.attestation import google_roots

            self.attestation_roots = google_roots()
        return self.attestation_roots

    def revoked_serials(self) -> set:
        """Google's list of revoked attestation certificates (refreshed daily in the background)."""
        if self._revocations is None:
            from app.security.attestation import RevocationList

            self._revocations = RevocationList(self.settings.ATTESTATION_STATUS_URL)
        return self._revocations.current()

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
        self.bot = BotService(bot_settings, self.database, self.telegram, self.limiter)
        password_reset.install(self.bot)
        from app.services import media_moderation, verification

        media_moderation.install(self)
        verification.install(self)
        from app.services import membership

        membership.install(self)
        from app.services import signup

        signup.install(self)  # V6 phase 18: «أُرسل يدويًا» / «إلغاء التسجيل»

    def load_runtime_config(self) -> None:
        """Apply bot settings saved from the admin panel (they take precedence over .env)."""
        from app.services import runtime_config

        self.reload_tunables()
        self.reload_i18n()
        with self.database.session() as db:
            cfg = runtime_config.telegram_config(db, self.settings)
        if cfg["source"] == "panel":
            self.configure_telegram(cfg["token"], cfg["chat_id"], cfg["secret"], "panel")

    def reload_tunables(self) -> None:
        """Limits/conditions saved from the panel (V5): applied live, here and in the bot's settings copy."""
        from app.services import tunables

        with self.database.session() as db:
            tunables.apply(db, self.settings, self.bot.settings if self.bot is not None else None)

    def reload_i18n(self) -> None:
        """V6 phase 12: the interface texts edited in the panel (all languages)."""
        from app.i18n import store

        with self.database.session() as db:
            store.load(db)

    def on_system_event(self, event: dict) -> None:
        if event.get("type") == "tunables":
            self.reload_tunables()
        elif event.get("type") == "i18n":
            self.reload_i18n()
            users = self.hub.local_users()
            if users:  # the open apps fetch their dictionary again and redraw the page
                self.hub._deliver_local(users, {"type": "i18n"})

    def dispatch(self, effects: Effects) -> None:
        """Fire realtime signals / push after a successful commit."""
        for user_ids, reason in effects.signals:
            self.hub.notify(user_ids, {"type": "sync", "reason": reason})
        for user_ids, event in effects.events:
            self.hub.notify(user_ids, event)
        for fn, args in effects.tasks:  # V5: Telegram requests etc., after the commit, off the request thread
            self.pipeline.background(fn, self, *args)
        for user_id in dict.fromkeys(effects.push_to):
            if not self.hub.is_online(user_id):
                self.push.notify_user(user_id)
                if self.fcm is not None:
                    self.fcm.notify_message(user_id)
