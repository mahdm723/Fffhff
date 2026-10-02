"""The admin's Telegram bot: upload Reels and manage them from a chat.

Only messages whose sender AND chat are TELEGRAM_ADMIN_CHAT_ID (a private chat
with the admin) are obeyed. Anything else is ignored silently and logged
(rate-limited) as `telegram_unauthorized`.

* video (or video file)       → a video Reel; the caption becomes its text
* photo / album (media group) → one image Reel, pictures in the order sent
* /list /hide /show /delete /pin /unpin /caption /stats /help
* password-recovery requests are answered here too (see password_reset)

Updates are handled one at a time on a background thread (the webhook returns
immediately); media are prepared on a second pool. Bot replies are plain text
(no parse mode), so captions or user data can never inject formatting.
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
from app.models import PasswordReset, Reel, SecurityEvent, User
from app.services import audit, reels
from app.services.media import MediaError
from app.services.rate_limit import Limit
from app.services.telegram import FileTooLarge, TelegramClient, TelegramError

log = logging.getLogger("dzplay.bot")

HELP = (
    "أوامر DZPLAY:\n"
    "• أرسل فيديو مع وصف ← Reel فيديو\n"
    "• أرسل صورة أو ألبوم صور مع وصف ← Reel صور\n"
    "/list — آخر المحتوى\n"
    "/hide <id> — إخفاء\n"
    "/show <id> — إظهار\n"
    "/delete <id> — حذف\n"
    "/pin <id> [ساعات] — تثبيت في الأعلى مؤقتًا\n"
    "/unpin <id> — إلغاء التثبيت\n"
    "/caption <id> <نص> — تعديل الوصف\n"
    "/stats — ملخص سريع\n"
    "/code <رقم الطلب> <الرمز> — رمز استعادة حساب"
)
STATUS_AR = {"visible": "ظاهر", "hidden": "مخفي", "processing": "قيد التجهيز", "failed": "فشل"}


class BotService:
    def __init__(self, settings: Settings, database, telegram: TelegramClient, store, limiter):
        self.settings = settings
        self.database = database
        self.tg = telegram
        self.store = store
        self.limiter = limiter
        self.admin_id = str(settings.TELEGRAM_ADMIN_CHAT_ID).strip()
        self._updates = ThreadPoolExecutor(1, thread_name_prefix="tg-updates")
        self._work = ThreadPoolExecutor(2, thread_name_prefix="tg-media")
        self._albums: dict[str, threading.Timer] = {}
        self._albums_lock = threading.Lock()
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
        with self._albums_lock:
            for timer in self._albums.values():
                timer.cancel()
        self._updates.shutdown(wait=False, cancel_futures=True)
        self._work.shutdown(wait=False, cancel_futures=True)
        self.tg.close()

    def wait_idle(self, timeout: float = 30.0) -> None:
        """Test helper: wait until queued updates, album timers and media jobs are done."""
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._albums_lock, self._inflight_lock:
                if not self._albums and self._inflight == 0:
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
            if not self._authorized((cq.get("message") or {}).get("chat"), cq.get("from")):
                self._reject(cq.get("from"))
                return
            self._callback(cq)
            return
        msg = update.get("message")
        if not isinstance(msg, dict):
            return
        if not self._authorized(msg.get("chat"), msg.get("from")):
            self._reject(msg.get("from"))
            return
        doc = msg.get("document") or {}
        mime = str(doc.get("mime_type") or "")
        if msg.get("video") or mime.startswith("video/"):
            self._media(msg, "video", msg.get("video") or doc)
        elif msg.get("photo") or mime.startswith("image/"):
            photo = max(msg["photo"], key=lambda p: (p.get("width") or 0) * (p.get("height") or 0)) if msg.get("photo") else doc
            self._media(msg, "image", photo)
        elif isinstance(msg.get("text"), str) and msg["text"].startswith("/"):
            self._command(msg["text"].strip(), msg.get("message_id"))
        else:
            self.say(HELP)

    # -------------------------------------------------------------- uploads
    def _media(self, msg: dict, kind: str, file: dict) -> None:
        size = int(file.get("file_size") or 0)
        limit_mb = self.settings.TELEGRAM_MAX_FILE_MB
        if size > limit_mb * 1024 * 1024:
            self.say(f"❌ الملف حجمه {size / 1048576:.1f}MB، والحد {limit_mb}MB (قيد Telegram Bot API). "
                     "أرسله كفيديو عادي (يضغطه Telegram تلقائيًا) أو قصّه ثم أعد الإرسال.",
                     reply_to=msg.get("message_id"))
            return
        group = msg.get("media_group_id")
        caption = msg.get("caption")
        with self.database.session() as db:
            reel = None
            if group and kind == "image":
                since = clock.utcnow() - timedelta(hours=1)
                reel = db.scalar(select(Reel).where(Reel.media_group_id == str(group), Reel.created_at > since,
                                                    Reel.kind == "images"))
            if reel is None:
                reel = reels.create_reel(db, self.settings, "images" if kind == "image" else "video", caption,
                                         str(group) if group and kind == "image" else None)
            elif caption and not reel.caption:
                reel.caption = reels.clean_caption(self.settings, caption)
            reels.add_asset(db, reel, kind=kind, file_id=str(file.get("file_id")), unique_id=file.get("file_unique_id"),
                            size=size or None, message_id=msg.get("message_id"), width=file.get("width"),
                            height=file.get("height"))
            reel_id, short = reel.id, reel.short_id
        if group and kind == "image":
            self._settle_album(str(group), reel_id)
        else:
            self.run_later(self.publish, reel_id)
        log.info("bot: received %s for reel %s", kind, short)

    def _settle_album(self, group: str, reel_id: str) -> None:
        with self._albums_lock:
            old = self._albums.pop(group, None)
            if old:
                old.cancel()
            timer = threading.Timer(self.settings.TELEGRAM_ALBUM_SETTLE_SECONDS, self._album_done, (group, reel_id))
            timer.daemon = True
            self._albums[group] = timer
            timer.start()

    def _album_done(self, group: str, reel_id: str) -> None:
        with self._albums_lock:
            if self._albums.get(group) is None:
                return
            self.run_later(self.publish, reel_id)  # counted as in-flight before the timer entry goes away
            self._albums.pop(group, None)

    def publish(self, reel_id: str) -> None:
        """Fetch + prepare every file of the reel, then make it visible and confirm to the admin."""
        with self.database.session() as db:
            reel = db.get(Reel, reel_id)
            if reel is None:
                return
            assets = reels.assets_of(db, reel.id)
            try:
                for asset in assets:
                    if not asset.ready:
                        self.store.materialize(db, asset)
            except (MediaError, FileTooLarge, TelegramError) as exc:
                reel.status = "failed"
                reason = "الملف أكبر من حد Telegram Bot API." if isinstance(exc, FileTooLarge) else (
                    str(exc) if isinstance(exc, MediaError) else "تعذّر تنزيل الملف من Telegram.")
                reel.error = reason[:255]
                audit.record(db, "telegram", "reel_failed", target_type="reel", target_id=reel.short_id, detail=reason)
                db.commit()
                self.say(f"❌ لم يُنشر المحتوى {reel.short_id}: {reason}")
                return
            if reel.status == "processing":
                reel.status = "visible"
            reel.updated_at = clock.utcnow()
            audit.record(db, "telegram", "reel_published", target_type="reel", target_id=reel.short_id,
                         detail=f"{reel.kind}, {len(assets)} file(s)")
            kind = "فيديو" if reel.kind == "video" else f"{len(assets)} صورة"
            extra = f" ({assets[0].duration:.0f} ث)" if reel.kind == "video" and assets and assets[0].duration else ""
            text = f"✅ نُشر Reel {reel.short_id} — {kind}{extra}.\nللإخفاء: /hide {reel.short_id}"
        self.say(text)

    # -------------------------------------------------------------- commands
    def _command(self, text: str, message_id: int | None) -> None:
        parts = text.split(maxsplit=2)
        cmd = parts[0].split("@", 1)[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        rest = parts[2] if len(parts) > 2 else ""
        if cmd in ("/start", "/help"):
            self.say(HELP)
        elif cmd == "/list":
            self._list()
        elif cmd == "/stats":
            self._stats()
        elif cmd in ("/hide", "/show", "/delete", "/pin", "/unpin", "/caption"):
            self._reel_command(cmd, arg, rest)
        elif cmd == "/code" and self.reset_handler is not None:
            self.reset_handler(self, arg, rest)
        else:
            self.say("أمر غير معروف.\n\n" + HELP)

    def _list(self) -> None:
        with self.database.session() as db:
            rows = reels.recent(db, 15)
            if not rows:
                self.say("لا يوجد محتوى بعد. أرسل فيديو أو صورًا للبدء.")
                return
            now = clock.utcnow()
            lines = []
            for r in rows:
                pinned = " 📌" if r.pinned_until and r.pinned_until > now else ""
                kind = "🎬" if r.kind == "video" else "🖼"
                lines.append(f"{r.short_id} {kind} {STATUS_AR.get(r.status, r.status)}{pinned} · "
                             f"👍{r.likes_count} 👎{r.dislikes_count} 💬{r.comments_count} 👁{r.views_count}")
        self.say("آخر المحتوى:\n" + "\n".join(lines))

    def _stats(self) -> None:
        with self.database.session() as db:
            day = clock.utcnow() - timedelta(days=1)
            users = db.scalar(select(func.count()).select_from(User).where(User.is_official.is_not(True))) or 0
            new_users = db.scalar(select(func.count()).select_from(User).where(User.created_at > day)) or 0
            s = reels.summary(db)
            resets = db.scalar(select(func.count()).select_from(PasswordReset).where(PasswordReset.status == "pending")) or 0
        self.say(
            "📊 DZPLAY\n"
            f"المستخدمون: {users} (+{new_users} اليوم)\n"
            f"Reels: {s['visible']} ظاهر · {s['hidden']} مخفي · {s['processing']} قيد التجهيز\n"
            f"تفاعل آخر 24 ساعة: 👍👎 {s['reactions_24h']} · 💬 {s['comments_24h']} · 👁 {s['views_24h']}\n"
            f"طلبات استعادة تنتظر: {resets}"
        )

    def _reel_command(self, cmd: str, ref: str, rest: str) -> None:
        if not ref:
            self.say(f"اكتب رقم المحتوى بعد الأمر، مثلًا: {cmd} ab12cd")
            return
        with self.database.session() as db:
            reel = reels.find(db, ref)
            if reel is None:
                self.say(f"لا يوجد محتوى بالرقم {ref[:20]}.")
                return
            sid = reel.short_id
            if cmd == "/hide":
                reels.set_status(db, reel, "hidden")
                reply = f"🙈 أُخفي {sid}."
            elif cmd == "/show":
                if reel.status in ("processing", "failed"):
                    self.say(f"لا يمكن إظهار {sid}: حالته {STATUS_AR.get(reel.status)}.")
                    return
                reels.set_status(db, reel, "visible")
                reply = f"👁 أصبح {sid} ظاهرًا."
            elif cmd == "/delete":
                reels.delete_reel(db, self.store, reel)
                reply = f"🗑 حُذف {sid} نهائيًا."
            elif cmd == "/pin":
                hours = int(rest) if rest.strip().isdigit() and 0 < int(rest) <= 24 * 30 else None
                until = reels.pin(db, self.settings, reel, hours)
                reply = f"📌 ثُبّت {sid} حتى {until:%Y-%m-%d %H:%M} UTC."
            elif cmd == "/unpin":
                reels.unpin(reel)
                reply = f"أُلغي تثبيت {sid}."
            else:  # /caption
                reels.edit_caption(db, self.settings, reel, rest)
                reply = f"✏️ عُدّل وصف {sid}."
            audit.record(db, "telegram", f"reel_{cmd[1:]}", target_type="reel", target_id=sid)
        self.say(reply)

    # -------------------------------------------------------------- inline buttons
    def _callback(self, cq: dict) -> None:
        data = str(cq.get("data") or "")
        prefix = data.split(":", 1)[0]
        handler = self.callback_handlers.get(prefix)
        if handler is None:
            self.tg.answer_callback(cq.get("id"), "غير معروف")
            return
        handler(self, cq, data.split(":", 1)[1] if ":" in data else "")
