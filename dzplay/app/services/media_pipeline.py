"""Runs user uploads through the media worker and stores the result in Telegram (V5).

    phone ──(checked, compressed)──▶ POST /api/uploads ──▶ UPLOAD_TMP_DIR/<id>.in (size-capped tmpfs)
          ──▶ media worker: magic bytes, re-encode, strip metadata, NSFW ──▶ <id>.img.webp
          ──▶ app: sendDocument to the private storage channel (file_id kept in the DB, never sent to clients)
          ──▶ prepared copy moved into the media cache; every temp file deleted.

One job at a time (MEDIA_WORKER=inline: in this process, `nice`d; MEDIA_WORKER=queue: the separate,
network-less media-worker container through Redis). A refused file never reaches Telegram.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from app import clock
from app.models import MediaItem, SecurityEvent
from app.services import media_check
from app.services.telegram import TelegramError

log = logging.getLogger("dzplay.uploads")

QUEUE = "dz:media:jobs"
RESULT = "dz:media:result:"
HEARTBEAT = "dz:media:worker"
MIME = {"img": "image/webp"}
EXT = {"img": ".webp"}


class MediaPipeline:
    def __init__(self, state):
        self.state = state
        self.tmp = Path(state.settings.UPLOAD_TMP_DIR).resolve()
        self._jobs = ThreadPoolExecutor(1, thread_name_prefix="media-job")
        self._bg = ThreadPoolExecutor(1, thread_name_prefix="media-tg")
        self._lock = threading.Lock()
        self._pending = 0
        self._redis = None

    @property
    def settings(self):
        return self.state.settings

    # -------------------------------------------------------------- availability
    def storage_chat(self) -> str:
        return str(self.settings.TELEGRAM_STORAGE_CHANNEL_ID or "").strip()

    def unavailable(self) -> str | None:
        if self.state.telegram is None or not self.storage_chat():
            return "رفع الصور غير متاح حاليًا."
        if self.settings.MEDIA_WORKER == "queue" and not self.worker_alive():
            return "معالجة الصور متوقفة مؤقتًا. حاول بعد قليل."
        return None

    def busy(self, incoming: int) -> bool:
        with self._lock:
            if self._pending >= self.settings.UPLOAD_QUEUE_MAX:
                return True
        used = 0
        if self.tmp.exists():
            used = sum(p.stat().st_size for p in self.tmp.iterdir() if p.is_file())
        return used + incoming > self.settings.UPLOAD_TMP_MAX_MB * 1024 * 1024

    def redis(self):
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(self.settings.REDIS_URL, socket_timeout=self.settings.MEDIA_WORKER_TIMEOUT + 10)
        return self._redis

    def worker_alive(self) -> bool:
        try:
            return bool(self.redis().exists(HEARTBEAT))
        except Exception:  # noqa: BLE001
            return False

    def input_path(self, item_id: str) -> Path:
        self.tmp.mkdir(parents=True, exist_ok=True)
        return media_check.paths(self.tmp, item_id)["in"]

    # -------------------------------------------------------------- jobs
    def submit(self, item_id: str, job: dict):
        with self._lock:
            self._pending += 1
        return self._jobs.submit(self._safe_job, item_id, job)

    def background(self, fn, *args):
        """Telegram calls that must not slow a request down (moderation notices, deletions)."""
        with self._lock:
            self._pending += 1
        return self._bg.submit(self._safe_bg, fn, *args)

    def _safe_bg(self, fn, *args) -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            msg = self.state.telegram.redact(exc) if self.state.telegram else str(exc)
            log.error("media background task failed: %s: %s", type(exc).__name__, msg)
        finally:
            with self._lock:
                self._pending -= 1

    def _safe_job(self, item_id: str, job: dict) -> None:
        try:
            self._process(item_id, job)
        except Exception:  # noqa: BLE001
            log.exception("upload %s failed", item_id[:8])
            self._fail(item_id, "تعذّرت معالجة الملف. حاول مرة أخرى.")
        finally:
            media_check.cleanup(self.tmp, item_id)
            with self._lock:
                self._pending -= 1

    def _process(self, item_id: str, job: dict) -> None:
        with self.state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None or item.state != "uploaded":
                return
            item.state = "processing"
        if self.settings.MEDIA_WORKER == "queue":
            result = self._remote(job)
        else:
            result = media_check.run_job(self.settings, self.tmp, job)
        if not result.get("ok"):
            self._reject(item_id, result)
            return
        self._store(item_id, result)

    def _remote(self, job: dict) -> dict:
        r = self.redis()
        r.lpush(QUEUE, json.dumps(job))
        got = r.brpop(RESULT + job["id"], timeout=self.settings.MEDIA_WORKER_TIMEOUT)
        if not got:
            return {"ok": False, "code": "timeout", "error": "استغرقت المعالجة وقتًا طويلًا. حاول لاحقًا."}
        try:
            result = json.loads(got[1])
        except ValueError:
            return {"ok": False, "code": "error", "error": "تعذّرت معالجة الملف."}
        return result if isinstance(result, dict) else {"ok": False, "code": "error", "error": "تعذّرت معالجة الملف."}

    def _notify(self, owner_id: str | None, item_id: str, state: str, error: str | None = None) -> None:
        if owner_id:
            self.state.hub.notify([owner_id], {"type": "upload", "id": item_id, "state": state, "error": error})

    def _fail(self, item_id: str, error: str) -> None:
        with self.state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None or item.state not in ("uploaded", "processing"):
                return
            item.state, item.error = "failed", error[:255]
            owner = item.owner_id
        self._notify(owner, item_id, "failed", error)

    def _reject(self, item_id: str, result: dict) -> None:
        error = str(result.get("error") or "الملف مرفوض.")[:255]
        with self.state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None:
                return
            item.state, item.error = ("rejected", error) if result.get("code") not in ("error", "timeout", "missing") \
                else ("failed", error)
            item.nsfw_score = result.get("nsfw")
            owner = item.owner_id
            if result.get("code") == "nsfw":  # repeated attempts are visible to the admins
                db.add(SecurityEvent(type="media_rejected_nsfw", user_id=owner, detail=f"{item.purpose}",
                                     created_at=clock.utcnow()))
            state = item.state
        self._notify(owner, item_id, state, error)

    def _store(self, item_id: str, result: dict) -> None:
        tg = self.state.telegram
        chat = self.storage_chat()
        files = media_check.paths(self.tmp, item_id)
        main = "img"
        with self.state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None:
                return
            caption = f"#{item.purpose} {item.owner_public_id or '-'} {item_id[:12]}"
        if tg is None or not chat:
            self._fail(item_id, "رفع الصور غير متاح حاليًا.")
            return
        try:
            msg = tg.send_document(chat, files[main], f"{item_id[:12]}{EXT[main]}", MIME[main], caption)
        except TelegramError as exc:
            log.error("storage upload failed: %s", exc)
            self._fail(item_id, "تعذّر حفظ الملف الآن. حاول بعد قليل.")
            return
        doc = msg.get("document") or {}
        if not doc.get("file_id"):
            self._fail(item_id, "تعذّر حفظ الملف الآن. حاول بعد قليل.")
            return
        with self.state.database.session() as db:
            item = db.get(MediaItem, item_id)
            if item is None:
                return
            item.tg_file_id, item.tg_unique_id = str(doc["file_id"]), doc.get("file_unique_id")
            item.tg_message_id = msg.get("message_id")
            item.width, item.height = result.get("width"), result.get("height")
            item.size = result.get("size")
            item.nsfw_score, item.blur = result.get("nsfw"), result.get("blur")
            item.state, item.ready_at = "ready", clock.utcnow()
            produced = {v: files[v] for v in media_check.OUTPUTS[result["kind"]] if files[v].exists()}
            self.state.media.adopt(db, item, produced)
            owner = item.owner_id
        log.info("upload %s stored (%s, %s ms)", item_id[:8], result.get("type"), result.get("ms"))
        self._notify(owner, item_id, "ready")

    # -------------------------------------------------------------- lifecycle
    def wait_idle(self, timeout: float = 60.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if self._pending == 0:
                    return
            time.sleep(0.02)
        raise TimeoutError("media pipeline still busy")

    def shutdown(self) -> None:
        self._jobs.shutdown(wait=False, cancel_futures=True)
        self._bg.shutdown(wait=False, cancel_futures=True)
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception:  # noqa: BLE001
                pass
