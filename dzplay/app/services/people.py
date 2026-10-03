"""People search (V4) — from the profile page only.

* "DZ-XXXXXX" → exact public-ID match (works even for people hidden from name search);
* anything else (2+ characters) → partial match on the normalized name
  (أ/إ/آ→ا, ة↔ه, ى↔ي, no tashkeel, case-insensitive).

Never returned: blocked people (either direction), suspended/banned, team accounts, people who
still use the default name (ID search only), people who turned "visible in name search" off
(ID search only), e-mails or internal ids. Anti-scraping: per-user rate limits, a small page
size and a hard page cap.
"""

from __future__ import annotations

import re

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app.config import Settings
from app.errors import AppError, rate_limited
from app.models import PUBLIC_ID_RE, Block, User
from app.services import ideas, names
from app.services.rate_limit import Limit


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def card(db: Session, u: User) -> dict:
    return {"name": names.shown_name(u), "public_id": u.public_id, "gender": names.public_gender(u),
            "profile_ref": ideas.profile_ref_for(db, u.id)}


def check_lookup(settings: Settings, limiter, viewer: User) -> None:
    """Every people lookup (search, opening a DZ-ID, first message by DZ-ID) counts against the same
    per-user limits: walking the ID space is as slow as searching."""
    decision = limiter.check_and_hit([Limit(f"people_min:{viewer.id}", settings.SEARCH_PER_MINUTE, 60),
                                      Limit(f"people_day:{viewer.id}", settings.SEARCH_PER_DAY, 86400)])
    if not decision.allowed:
        raise rate_limited(decision.retry_after, "عمليات بحث كثيرة. انتظر قليلًا.")


def search(db: Session, settings: Settings, limiter, viewer: User, q: object, page: int = 0) -> dict:
    if not isinstance(q, str):
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    q = q.strip()
    page = max(0, int(page or 0))
    if page >= settings.SEARCH_MAX_PAGES:
        raise AppError(400, "too_many_pages", "نتائج كثيرة: اكتب بحثًا أدق.")
    check_lookup(settings, limiter, viewer)

    blocked = exists().where(or_(and_(Block.blocker_id == viewer.id, Block.blocked_id == User.id),
                                 and_(Block.blocker_id == User.id, Block.blocked_id == viewer.id)))
    base = [User.status == "active", User.is_system.is_not(True), User.is_official.is_not(True), ~blocked]
    pid = q.upper()
    if re.match(PUBLIC_ID_RE, pid):
        rows = list(db.execute(select(User).where(User.public_id == pid, *base)).scalars())
        return {"by": "id", "results": [card(db, u) for u in rows], "has_more": False}

    key = names.search_key(q)
    if len(key.replace(" ", "")) < 2:
        raise AppError(400, "query_too_short", "اكتب حرفين على الأقل، أو الـID كاملًا (DZ-XXXXXX).")
    size = settings.SEARCH_MAX_RESULTS
    rows = list(db.execute(
        select(User).where(*base, User.id != viewer.id, User.display_name.is_not(None), User.searchable_by_name.is_not(False),
                           User.name_norm.like(_like(key), escape="\\"))
        .order_by(User.last_active_at.desc()).offset(page * size).limit(size + 1)
    ).scalars())
    has_more = len(rows) > size and page + 1 < settings.SEARCH_MAX_PAGES
    return {"by": "name", "results": [card(db, u) for u in rows[:size]], "has_more": has_more}
