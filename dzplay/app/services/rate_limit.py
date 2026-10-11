"""Sliding-window rate limiter with two interchangeable backends.

* MemoryRateLimiter — default; correct for a single server process.
* RedisRateLimiter  — enabled with REDIS_URL; shared across workers/instances.

`check_and_hit` evaluates several limits at once and records the hit only if
*all* of them allow it, so a rejected request never consumes quota.
"""

from __future__ import annotations

import threading
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass

from app import clock


@dataclass(frozen=True)
class Limit:
    key: str
    limit: int
    window: int  # seconds


@dataclass
class Decision:
    allowed: bool
    retry_after: float = 0.0
    key: str | None = None  # the limit that refused


class MemoryRateLimiter:
    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check_and_hit(self, limits: list[Limit]) -> Decision:
        now = clock.timestamp()
        with self._lock:
            for lim in limits:
                q = self._hits[lim.key]
                while q and q[0] <= now - lim.window:
                    q.popleft()
                if lim.limit <= 0 or len(q) >= lim.limit:
                    retry = (q[0] + lim.window - now) if q else lim.window
                    return Decision(False, retry, lim.key)
            for lim in limits:
                self._hits[lim.key].append(now)
        return Decision(True)

    def reset(self) -> None:
        with self._lock:
            self._hits.clear()


_LUA = """
local now = tonumber(ARGV[1])
local member = ARGV[2]
local n = #KEYS
for i = 1, n do
  local limit = tonumber(ARGV[2 + (i - 1) * 2 + 1])
  local window = tonumber(ARGV[2 + (i - 1) * 2 + 2])
  redis.call('ZREMRANGEBYSCORE', KEYS[i], '-inf', now - window)
  local count = redis.call('ZCARD', KEYS[i])
  if limit <= 0 or count >= limit then
    local oldest = redis.call('ZRANGE', KEYS[i], 0, 0, 'WITHSCORES')
    local retry = window
    if oldest[2] then retry = tonumber(oldest[2]) + window - now end
    return {0, tostring(retry), i}
  end
end
for i = 1, n do
  local window = tonumber(ARGV[2 + (i - 1) * 2 + 2])
  redis.call('ZADD', KEYS[i], now, member)
  redis.call('EXPIRE', KEYS[i], math.ceil(window) + 1)
end
return {1, '0', 0}
"""


class RedisRateLimiter:
    def __init__(self, url: str, prefix: str = "dz:rl:") -> None:
        import redis

        self._redis = redis.Redis.from_url(url)
        self._script = self._redis.register_script(_LUA)
        self._prefix = prefix

    def check_and_hit(self, limits: list[Limit]) -> Decision:
        if not limits:
            return Decision(True)
        keys = [self._prefix + lim.key for lim in limits]
        args: list = [clock.timestamp(), uuid.uuid4().hex]
        for lim in limits:
            args += [lim.limit, lim.window]
        allowed, retry, idx = self._script(keys=keys, args=args)
        if int(allowed) == 1:
            return Decision(True)
        return Decision(False, float(retry), limits[int(idx) - 1].key)

    def reset(self) -> None:
        for key in self._redis.scan_iter(f"{self._prefix}*"):
            self._redis.delete(key)


def make_rate_limiter(redis_url: str):
    return RedisRateLimiter(redis_url) if redis_url else MemoryRateLimiter()
