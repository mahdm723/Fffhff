"""The admin's Telegram bot: moderation buttons, password-recovery requests, quick stats.

Only messages whose sender AND chat are TELEGRAM_ADMIN_CHAT_ID (a private chat
with the admin) are obeyed; the moderators' group may only press moderation buttons.
Anything else is ignored silently and logged (rate-limited) as `telegram_unauthorized`.

* /start /help /stats
* /code <request> <code> — password-recovery requests (see password_reset)
* inline buttons: md: (media moderation), vf: (blue star requests), …

Updates are handled one at a time on a background thread (the webhook returns
immediately); slow jobs run on a second pool. Bot replies are plain text
(no parse mode), so user data can never inject formatting.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import timedelta

from sqlalchemy import func, select

from app import clock
from app.config import Settings
from app.models import PasswordReset, Post, SecurityEvent, User
from app.services.rate_limit import Limit
from app.services.telegram import TelegramClient, TelegramError

log = logging.getLogger("dzplay.bot")

HELP = (
    "الأوامر:\n"
    "/stats — ملخص سريع\n"
    "/code <رقم الطلب> <الرمز> — رمز استعادة حساب\n"
    "تصلك هنا أيضًا طلبات الإشراف وأزرارها."
)


class BotService:
    def __init__(self, settings: Settings, database, telegram: TelegramClient, limiter):
        self.settings = settings
        self.database = database
        self.tg = telegram
        self.limiter = limiter
        self.admin_id = str(settings.TELEGRAM_ADMIN_CHAT_ID).strip()
        self._updates = ThreadPoolExecutor(1, thread_name_prefix="tg-updates")
        self._work = ThreadPoolExecutor(2, thread_name_prefix="tg-work")
        self._seen_updates: deque[int] = deque(maxlen=1000)
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self.reset_handler = None  # set by password_reset: (bot, text_args, reply) -> None
        self.callback_handlers: dict[str, object] = {}

    # -------------------------------------------------------------- plumbing
    def submit(self, update: dict) -> Future | None:
        uid = update.get("update_id")
        if isinstance(uid, int):
            if uid in self._seen_updates:  # Telegram re-delivers when it got no 200 in time
                return None
            self._seen_updates.append(uid)
        return self._track(self._updates, self.handle, update)

    def run_later(self, fn, *args) -> Future:
        return self._track(self._work, fn, *args)

    def _track(self, pool: ThreadPoolExecutor, fn, *args) -> Future:
        with self._inflight_lock:
            self._inflight += 1
        try:
            return pool.submit(self._safe, fn, *args)
        except RuntimeError:  # shutting down
            with self._inflight_lock:
                self._inflight -= 1
            raise

    def _safe(self, fn, *args):
        try:
            return fn(*args)
        except Exception as exc:  # noqa: BLE001 - never let a bad update kill the worker
            log.error("bot error: %s", self.tg.redact(f"{type(exc).__name__}: {exc}"))
            return None
        finally:
            with self._inflight_lock:
                self._inflight -= 1

    def shutdown(self) -> None:
        self._updates.shutdown(wait=False, cancel_futures=True)
        self._work.shutdown(wait=False, cancel_futures=True)
        self.tg.close()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Test helper: wait until queued updates and background jobs are done."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._inflight_lock:
                if self._inflight == 0:
                    return
            time.sleep(0.02)
        raise TimeoutError("bot still busy")

    def say(self, text: str, **kw) -> None:
        try:
            self.tg.send_message(self.admin_id, text, **kw)
        except TelegramError as exc:
            log.warning("bot reply failed: %s", exc)

    # -------------------------------------------------------------- authorisation
    def _authorized(self, chat: dict | None, sender: dict | None) -> bool:
        return (bool(self.admin_id) and chat is not None and sender is not None
                and str(chat.get("id")) == self.admin_id and str(sender.get("id")) == self.admin_id)

    def _moderation_chat(self, chat: dict | None) -> bool:
        """The private moderators' group (V5): its members may press the media moderation buttons."""
        mod = str(self.settings.TELEGRAM_MODERATION_CHAT_ID or "").strip()
        return bool(mod) and chat is not None and str(chat.get("id")) == mod

    def _reject(self, sender: dict | None) -> None:
        decision = self.limiter.check_and_hit([Limit("tg_unauthorized_log", 30, 3600)])
        if not decision.allowed:
            return
        who = hashlib.sha256(str((sender or {}).get("id")).encode()).hexdigest()[:12]
        with self.database.session() as db:
            db.add(SecurityEvent(type="telegram_unauthorized", detail=f"sender:{who}", created_at=clock.utcnow()))

    # -------------------------------------------------------------- dispatch
    def handle(self, update: dict) -> None:
        cq = update.get("callback_query")
        if cq:
            chat = (cq.get("message") or {}).get("chat")
            moderator = self._moderation_chat(chat) and str(cq.get("data") or "").startswith("md:")
            if not moderator and not self._authorized(chat, cq.get("from")):
                self._reject(cq.get("from"))
                return
            self._callback(cq)
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        if self._moderation_chat(msg.get("chat")):
            return  # the moderators talk among themselves there; the bot only acts on its buttons
        if not self._authorized(msg.get("chat"), msg.get("from")):
            self._reject(msg.get("from"))
            return
        if isinstance(msg.get("text"), str) and msg["text"].startswith("/"):
            self._command(msg["text"].strip(), msg.get("message_id"))
        else:
            self.say(HELP)

    # -------------------------------------------------------------- commands
    def _command(self, text: str, message_id: int | None) -> None:
        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd == "/stats":
            self._stats()
        elif cmd == "/code" and self.reset_handler is not None:
            self.reset_handler(self, arg, rest)
        else:
            self.say("أمر غير معروف.\n\n" + HELP)

    def _stats(self) -> None:
        with self.database.session() as db:
            day = clock.utcnow() - timedelta(days=1)
            real = (User.is_official.is_not(True), User.is_system.is_not(True))
            users = db.scalar(select(func.count()).select_from(User).where(*real)) or 0
            new_users = db.scalar(select(func.count()).select_from(User).where(*real, User.created_at > day)) or 0
            ideas = db.scalar(select(func.count()).select_from(Post).where(Post.created_at > day)) or 0
            resets = db.scalar(select(func.count()).select_from(PasswordReset).where(PasswordReset.status == "pending")) or 0
        self.say(
            f"📊 {self.settings.APP_NAME}\n"
            f"المستخدمون: {users} (+{new_users} اليوم)\n"
            f"أفكار آخر 24 ساعة: {ideas}\n"
            f"طلبات استعادة تنتظر: {resets}"
        )

    # -------------------------------------------------------------- inline buttons
    def _callback(self, cq: dict) -> None:
        data = str(cq.get("data") or "")
        prefix = data.split(":", 1)[0]
        handler = self.callback_handlers.get(prefix)
        if handler is None:
            self.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        handler(self, cq, data.split(":", 1)[1] if ":" in data else "")
