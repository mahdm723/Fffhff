"""Reels ordering for one viewing session (all weights in settings).

1. Pinned reels (pinned_until in the future) come first, newest first.
2. Then reels the viewer has NOT seen recently, in a weighted random order.
3. Then recently seen reels, also weighted random — they only come back
   once everything else has been shown.

Weight = max(0.5 ** (age_hours / REELS_FRESHNESS_HALF_LIFE_HOURS), REELS_OLD_MIN_WEIGHT):
new content is more likely to come early, old content less likely but never
disappears. The shuffle is Efraimidis–Spirakis (key = u ** (1 / weight))
seeded per session, so the order is random across sessions but stable while
paging through one session.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from app.config import Settings


@dataclass(frozen=True)
class Candidate:
    id: str
    created_at: datetime
    pinned_until: datetime | None


def weight(settings: Settings, created_at: datetime, now: datetime) -> float:
    age_h = max(0.0, (now - created_at).total_seconds() / 3600)
    return max(0.5 ** (age_h / max(settings.REELS_FRESHNESS_HALF_LIFE_HOURS, 0.01)), settings.REELS_OLD_MIN_WEIGHT)


def _uniform(seed: str, item_id: str) -> float:
    digest = hashlib.sha256(f"{seed}:{item_id}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") + 1) / (2 ** 64 + 2)  # in (0, 1)


def order(settings: Settings, candidates: list[Candidate], seen: set[str], seed: str, now: datetime) -> list[str]:
    pinned = sorted((c for c in candidates if c.pinned_until and c.pinned_until > now),
                    key=lambda c: c.created_at, reverse=True)
    pinned_ids = {c.id for c in pinned}

    def shuffled(group: list[Candidate]) -> list[str]:
        keyed = [(_uniform(seed, c.id) ** (1.0 / weight(settings, c.created_at, now)), c.id) for c in group]
        return [cid for _k, cid in sorted(keyed, reverse=True)]

    rest = [c for c in candidates if c.id not in pinned_ids]
    unseen = [c for c in rest if c.id not in seen]
    again = [c for c in rest if c.id in seen]
    return [c.id for c in pinned] + shuffled(unseen) + shuffled(again)
