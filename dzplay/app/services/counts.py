"""Displayed reaction counts.

`likes_count` / `dislikes_count` on posts and reels are the REAL counters (one
row per user in PostReaction / ReelReaction). The team may add a boost from
the admin panel (`boost_likes` / `boost_dislikes`, may be negative after a
"set to N"). Users always see max(0, real + boost); the panel shows both.
"""

from __future__ import annotations

from sqlalchemy import case, func


def shown(real: int | None, boost: int | None) -> int:
    return max(0, (real or 0) + (boost or 0))


def shown_expr(real_col, boost_col):
    """SQL expression for max(0, real + boost) (portable: SQLite and PostgreSQL)."""
    total = real_col + func.coalesce(boost_col, 0)
    return case((total < 0, 0), else_=total)
