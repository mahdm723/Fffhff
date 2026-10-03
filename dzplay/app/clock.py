"""Single source of time so tests can move the clock (TTL, blocks, rate limits).

All timestamps are naive UTC datetimes.
"""

from __future__ import annotations

import time as _time
from datetime import datetime, timezone

_offset_seconds = 0.0


def utcnow() -> datetime:
    return datetime.fromtimestamp(_time.time() + _offset_seconds, tz=timezone.utc).replace(tzinfo=None)


def timestamp() -> float:
    return _time.time() + _offset_seconds


def advance(seconds: float) -> None:
    """Test helper: jump the application clock forward."""
    global _offset_seconds
    _offset_seconds += seconds


def reset() -> None:
    global _offset_seconds
    _offset_seconds = 0.0
