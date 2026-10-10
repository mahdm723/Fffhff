"""The media worker (V5): `python -m app.media_worker` — the `media-worker` container.

It has no database credentials, no Telegram token and no internet access (internal Docker network with
Redis only). It takes one job at a time from Redis, processes the named file in the shared size-capped
tmpfs (UPLOAD_TMP_DIR) with app.services.media_check, writes the result back to Redis and goes on.
A heartbeat key lets the app refuse uploads (instead of queueing them) while the worker is down.
"""

from __future__ import annotations

import json
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from app.config import Settings
from app.services import media_check
from app.services.media_pipeline import HEARTBEAT, QUEUE, RESULT

log = logging.getLogger("dzplay.media_worker")


def _valid(job: object) -> bool:
    return (isinstance(job, dict) and isinstance(job.get("id"), str) and bool(media_check.JOB_ID.match(job["id"]))
            and job.get("kind") == "image" and isinstance(job.get("types"), list))


def main() -> int:
    settings = Settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not settings.REDIS_URL:
        log.error("REDIS_URL is required")
        return 2
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    serve(settings, stop)
    return 0


def serve(settings: Settings, stop: threading.Event) -> None:
    import redis

    r = redis.Redis.from_url(settings.REDIS_URL, socket_timeout=30)
    tmp = Path(settings.UPLOAD_TMP_DIR).resolve()

    def heartbeat() -> None:
        while not stop.is_set():
            try:
                r.set(HEARTBEAT, str(int(time.time())), ex=30)
            except Exception:  # noqa: BLE001
                log.warning("redis unreachable")
            stop.wait(10)

    threading.Thread(target=heartbeat, daemon=True).start()
    if settings.SERVER_NSFW_CHECK:
        media_check._session()  # load the model once, before the first job
    log.info("media worker ready (tmp=%s)", tmp)
    while not stop.is_set():
        try:
            got = r.brpop(QUEUE, timeout=5)
        except Exception:  # noqa: BLE001
            stop.wait(2)
            continue
        if not got:
            continue
        try:
            job = json.loads(got[1])
        except ValueError:
            continue
        if not _valid(job):
            log.warning("ignored a malformed job")
            continue
        result = media_check.run_job(settings, tmp, job)
        log.info("job %s: %s in %s ms", job["id"][:8], "ok" if result.get("ok") else result.get("code"), result.get("ms"))
        key = RESULT + job["id"]
        r.lpush(key, json.dumps(result))
        r.expire(key, 3600)
    r.delete(HEARTBEAT)


if __name__ == "__main__":
    sys.exit(main())
