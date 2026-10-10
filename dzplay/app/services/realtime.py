"""Realtime notifications over WebSocket.

Design: the socket only carries tiny "something changed" signals
({"type": "sync", "reason": ...}); the client then calls /api/sync. The
server-side state therefore stays the single source of truth, and reconnecting
after being offline is the same code path as a live update.

Events can be published from worker threads (sync endpoints); delivery to the
asyncio loop is thread-safe. With REDIS_URL set, events go through Redis
pub/sub so every app instance delivers to its own connected sockets.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections import defaultdict

log = logging.getLogger("dzplay.realtime")

_CHANNEL = "dz:events"


class Hub:
    def __init__(self, redis_url: str = "") -> None:
        # user id -> {queue: loop that owns the queue}
        self._queues: dict[str, dict[asyncio.Queue, asyncio.AbstractEventLoop]] = defaultdict(dict)
        self._lock = threading.Lock()
        self._redis_url = redis_url
        self._redis = None
        self._listener: asyncio.Task | None = None
        self.system_handler = None  # called with instance-wide events (e.g. settings changed in the panel)

    # lifecycle ---------------------------------------------------------------
    async def start(self) -> None:
        if self._redis_url:
            import redis

            self._redis = redis.Redis.from_url(self._redis_url)
            self._listener = asyncio.create_task(self._listen())

    async def stop(self) -> None:
        if self._listener:
            self._listener.cancel()
            # bounded: a Redis connection cancelled mid-operation must never block shutdown
            await asyncio.wait({self._listener}, timeout=5)
            self._listener = None
        if self._redis is not None:
            try:
                self._redis.close()
            except Exception:  # noqa: BLE001
                pass
            self._redis = None

    async def _listen(self) -> None:
        import redis.asyncio as aioredis

        client = aioredis.Redis.from_url(self._redis_url)
        pubsub = client.pubsub()
        await pubsub.subscribe(_CHANNEL)
        try:
            async for item in pubsub.listen():
                if item.get("type") != "message":
                    continue
                try:
                    data = json.loads(item["data"])
                    if "system" in data:
                        self._system_local(data["system"])
                        continue
                    self._deliver_local(data["users"], data["event"])
                except (ValueError, KeyError, TypeError):
                    log.warning("bad realtime payload")
        finally:
            for closing in (pubsub.aclose(), client.aclose()):
                try:
                    await asyncio.wait_for(closing, timeout=2)
                except (asyncio.TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass

    # connections -------------------------------------------------------------
    def connect(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        with self._lock:
            self._queues[user_id][q] = asyncio.get_running_loop()
        return q

    def disconnect(self, user_id: str, q: asyncio.Queue) -> None:
        with self._lock:
            conns = self._queues.get(user_id)
            if conns:
                conns.pop(q, None)
                if not conns:
                    del self._queues[user_id]

    def is_online(self, user_id: str) -> bool:
        with self._lock:
            return bool(self._queues.get(user_id))

    # publishing --------------------------------------------------------------
    def notify(self, user_ids: list[str], event: dict) -> None:
        """Thread-safe. Signals the given users on every instance."""
        if not user_ids:
            return
        if self._redis is not None:
            try:
                self._redis.publish(_CHANNEL, json.dumps({"users": user_ids, "event": event}))
                return
            except Exception:  # noqa: BLE001 - fall back to local delivery
                log.exception("redis publish failed")
        self._deliver_local(user_ids, event)

    def notify_system(self, event: dict) -> None:
        """Thread-safe. Tells every app instance (not users) that something changed, e.g. settings."""
        if self._redis is not None:
            try:
                self._redis.publish(_CHANNEL, json.dumps({"system": event}))
                return
            except Exception:  # noqa: BLE001 - fall back to this instance only
                log.exception("redis publish failed")
        self._system_local(event)

    def _system_local(self, event: dict) -> None:
        if self.system_handler is not None:
            try:
                self.system_handler(event)
            except Exception:  # noqa: BLE001
                log.exception("system event failed")

    def _deliver_local(self, user_ids: list[str], event: dict) -> None:
        with self._lock:
            targets = [(q, loop) for uid in user_ids for q, loop in self._queues.get(uid, {}).items()]
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        for q, loop in targets:
            if loop is running:
                _put(q, event)
            elif not loop.is_closed():
                loop.call_soon_threadsafe(_put, q, event)


def _put(q: asyncio.Queue, event: dict) -> None:
    if q.full():  # slow client: drop the oldest signal, a sync covers it anyway
        try:
            q.get_nowait()
        except asyncio.QueueEmpty:
            pass
    q.put_nowait(event)
