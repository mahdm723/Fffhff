"""V6 phase 21: how fast the app answers, per endpoint — for the panel («صحة النظام»), the Telegram alerts
(admin_cli health-check, called by deploy/monitor.sh) and, in staging, the load-test collector.

Each process counts in memory (a raw ASGI middleware: one lock and a few additions per request): per route template
(never a real URL, never an address or a user) and per minute — the count, the 5xx answers, the total time and a
histogram of latencies. Every 10 s the counts go to Redis (one hash per minute, kept 3 hours), so several workers and
instances add up; without Redis the process keeps them itself. Percentiles come from the histogram.
Open WebSockets are counted per process (a key per process, refreshed at each flush).
"""

from __future__ import annotations

import bisect
import logging
import os
import socket
import threading
import time
from collections import defaultdict

log = logging.getLogger("dzplay.metrics")

BOUNDS_MS = (5, 10, 25, 50, 75, 100, 150, 200, 300, 400, 500, 750, 1000, 1500, 2000, 3000, 5000, 10000, 30000)
_N = len(BOUNDS_MS) + 1  # the last bucket is everything slower
_KEEP_MINUTES = 180
_SEP = "\x1f"


def _minute(ts: float | None = None) -> int:
    return int((ts if ts is not None else time.time()) // 60)


class _Cell:
    __slots__ = ("count", "errors", "total_ms", "hist")

    def __init__(self) -> None:
        self.count = 0
        self.errors = 0
        self.total_ms = 0.0
        self.hist = [0] * _N

    def add(self, ms: float, error: bool) -> None:
        self.count += 1
        self.errors += error
        self.total_ms += ms
        self.hist[bisect.bisect_left(BOUNDS_MS, ms)] += 1

    def merge(self, other: "_Cell") -> None:
        self.count += other.count
        self.errors += other.errors
        self.total_ms += other.total_ms
        for i, v in enumerate(other.hist):
            self.hist[i] += v


def percentile(hist: list[int], count: int, q: float) -> float:
    """From the histogram: the q-th latency (ms), interpolated inside its bucket."""
    if not count:
        return 0.0
    want, seen = q * count, 0
    for i, n in enumerate(hist):
        if n and seen + n >= want:
            low = BOUNDS_MS[i - 1] if i else 0
            high = BOUNDS_MS[i] if i < len(BOUNDS_MS) else BOUNDS_MS[-1] * 2
            return round(low + (high - low) * ((want - seen) / n), 1)
        seen += n
    return float(BOUNDS_MS[-1] * 2)


class Metrics:
    def __init__(self, redis_url: str = "", admin_prefix: str = "", namespace: str = "dz") -> None:
        self._key = namespace + ":metrics:{}"  # one hash per minute
        self._ws_key = namespace + ":ws:{}"  # one counter per process
        self._lock = threading.Lock()
        self._pending: dict[tuple[int, str], _Cell] = defaultdict(_Cell)  # not yet in Redis
        self._local: dict[tuple[int, str], _Cell] = defaultdict(_Cell)  # without Redis: the process's own history
        self._redis_url = redis_url
        self._redis = None
        self._admin_prefix = admin_prefix
        self.ws_open = 0
        self._me = f"{socket.gethostname()}:{os.getpid()}"

    # ------------------------------------------------------------------ recording (any thread / the event loop)
    def label(self, scope: dict) -> str:
        route = scope.get("route")
        path = getattr(route, "path", None)
        if path is None:
            return "other"
        if path in ("", "/") and getattr(route, "name", "") == "static":
            return "static"
        if self._admin_prefix and path.startswith(self._admin_prefix):
            path = "/<panel>" + path[len(self._admin_prefix):]  # the secret panel path never leaves the process
        return f"{scope.get('method', '')} {path}"

    def observe(self, label: str, status: int, ms: float) -> None:
        with self._lock:
            self._pending[(_minute(), label)].add(ms, status >= 500)

    def ws_changed(self, delta: int) -> None:
        with self._lock:
            self.ws_open += delta

    # ------------------------------------------------------------------ storage
    def _client(self):
        if not self._redis_url:
            return None
        if self._redis is None:
            import redis

            self._redis = redis.Redis.from_url(self._redis_url, socket_timeout=2)
        return self._redis

    def flush(self) -> None:
        """Move the pending counts to Redis (or to the process's history). Called every few seconds."""
        with self._lock:
            pending, self._pending = self._pending, defaultdict(_Cell)
            ws = self.ws_open
        r = self._client()
        if r is None:
            with self._lock:
                for k, cell in pending.items():
                    self._local[k].merge(cell)
                oldest = _minute() - _KEEP_MINUTES
                for k in [k for k in self._local if k[0] < oldest]:
                    del self._local[k]
            return
        try:
            p = r.pipeline(transaction=False)
            for (minute, label), cell in pending.items():
                key = self._key.format(minute)
                p.hincrby(key, f"{label}{_SEP}c", cell.count)
                if cell.errors:
                    p.hincrby(key, f"{label}{_SEP}e", cell.errors)
                p.hincrbyfloat(key, f"{label}{_SEP}t", round(cell.total_ms, 3))
                for i, v in enumerate(cell.hist):
                    if v:
                        p.hincrby(key, f"{label}{_SEP}{i}", v)
                p.expire(key, _KEEP_MINUTES * 60)
            p.set(self._ws_key.format(self._me), ws, ex=60)
            p.execute()
        except Exception as exc:  # noqa: BLE001 - metrics never break the app; the counts are put back
            log.warning("metrics flush failed: %s", type(exc).__name__)
            with self._lock:
                for k, cell in pending.items():
                    self._pending[k].merge(cell)

    # ------------------------------------------------------------------ reading
    def _cells(self, minutes: int) -> dict[str, _Cell]:
        now = _minute()
        wanted = range(now - minutes + 1, now + 1)
        out: dict[str, _Cell] = defaultdict(_Cell)
        r = self._client()
        if r is None:
            with self._lock:
                items = list(self._local.items()) + list(self._pending.items())
            for (minute, label), cell in items:
                if minute in wanted:
                    out[label].merge(cell)
            return out
        p = r.pipeline(transaction=False)
        for m in wanted:
            p.hgetall(self._key.format(m))
        for raw in p.execute():
            for field, value in raw.items():
                label, _, kind = field.decode().rpartition(_SEP)
                cell = out[label]
                if kind == "c":
                    cell.count += int(value)
                elif kind == "e":
                    cell.errors += int(value)
                elif kind == "t":
                    cell.total_ms += float(value)
                else:
                    cell.hist[int(kind)] += int(value)
        return out

    def websockets(self) -> int:
        r = self._client()
        if r is None:
            return self.ws_open
        total = 0
        for key in r.scan_iter(match=self._ws_key.format("*"), count=200):
            total += int(r.get(key) or 0)
        return total

    def snapshot(self, minutes: int = 15, top: int = 10) -> dict:
        """Totals and the slowest endpoints over the last `minutes` minutes."""
        minutes = max(1, min(minutes, _KEEP_MINUTES))
        cells = self._cells(minutes)
        total = _Cell()
        for cell in cells.values():
            total.merge(cell)
        secs = minutes * 60

        def view(label: str, c: _Cell) -> dict:
            return {"endpoint": label, "count": c.count, "rps": round(c.count / secs, 2),
                    "errors": c.errors, "error_pct": round(100 * c.errors / c.count, 2) if c.count else 0.0,
                    "avg_ms": round(c.total_ms / c.count, 1) if c.count else 0.0,
                    "p50_ms": percentile(c.hist, c.count, 0.50), "p95_ms": percentile(c.hist, c.count, 0.95),
                    "p99_ms": percentile(c.hist, c.count, 0.99)}

        endpoints = [view(label, c) for label, c in cells.items() if c.count]
        endpoints.sort(key=lambda e: (e["p95_ms"], e["count"]), reverse=True)
        busy = [e for e in endpoints if e["count"] >= max(5, minutes)]  # a single odd request is not "slow"
        return {"minutes": minutes, "totals": view("all", total), "slowest": (busy or endpoints)[:top],
                "endpoints": endpoints, "websockets": self.websockets()}


class MetricsMiddleware:
    """Raw ASGI (cheaper than BaseHTTPMiddleware): times every HTTP request until its last byte."""

    def __init__(self, app, metrics: Metrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self.metrics.observe(self.metrics.label(scope), status, (time.perf_counter() - start) * 1000)
