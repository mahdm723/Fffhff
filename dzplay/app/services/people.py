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

import base64
import json
import re
from datetime import timedelta

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session

from app import clock
from app.config import HOUR, Settings
from app.errors import AppError, not_found, rate_limited
from app.models import PUBLIC_ID_RE, Block, MediaItem, Report, User
from app.services import ideas, names
from app.services.media_items import avatar_url
from app.services.rate_limit import Limit


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def card(db: Session, u: User) -> dict:
    return {"name": names.shown_name(u), "public_id": u.public_id, "gender": names.public_gender(u),
            "profile_ref": ideas.profile_ref_for(db, u.id), "verified": u.verified_at is not None,
            "avatar_url": avatar_url(u)}


def list_card(u: User) -> dict:
    """«المستخدمون»: the public card only."""
    return {"public_id": u.public_id, "name": names.shown_name(u), "avatar_url": avatar_url(u),
            "verified": u.verified_at is not None, "gender": names.public_gender(u)}


def _base_filters(viewer: User) -> list:
    blocked = exists().where(or_(and_(Block.blocker_id == viewer.id, Block.blocked_id == User.id),
                                 and_(Block.blocker_id == User.id, Block.blocked_id == viewer.id)))
    return [User.status == "active", User.is_system.is_not(True), User.is_official.is_not(True), ~blocked]


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

    base = _base_filters(viewer)
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


def _cursor_out(u: User) -> str:
    raw = json.dumps([u.last_active_at.isoformat(), u.id]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _cursor_in(cursor: str):
    from datetime import datetime

    try:
        at, uid = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        return datetime.fromisoformat(at), str(uid)[:32]
    except (ValueError, TypeError):
        raise AppError(400, "invalid_input", "طلب غير صالح.") from None


def listing(db: Session, settings: Settings, limiter, viewer: User, q: object, cursor: object) -> dict:
    """V6 phase 3: everyone who is listed (chose a name, visible in search), most recently active first.
    With q: the same search as /api/people/search (pages instead of a cursor). Same per-user limits."""
    if isinstance(q, str) and q.strip():
        page = int(cursor) if isinstance(cursor, str) and cursor.isdigit() else 0
        found = search(db, settings, limiter, viewer, q, page)
        rows = [db.scalar(select(User).where(User.public_id == r["public_id"])) for r in found["results"]]
        return {"results": [list_card(u) for u in rows if u is not None],
                "next_cursor": str(page + 1) if found["has_more"] else None}
    check_lookup(settings, limiter, viewer)
    size = settings.SEARCH_MAX_RESULTS
    query = select(User).where(*_base_filters(viewer), User.id != viewer.id, User.display_name.is_not(None),
                               User.searchable_by_name.is_not(False))
    if isinstance(cursor, str) and cursor:
        at, uid = _cursor_in(cursor)
        query = query.where(or_(User.last_active_at < at, and_(User.last_active_at == at, User.id < uid)))
    rows = list(db.execute(query.order_by(User.last_active_at.desc(), User.id.desc()).limit(size + 1)).scalars())
    page = rows[:size]
    return {"results": [list_card(u) for u in page], "next_cursor": _cursor_out(page[-1]) if len(rows) > size else None}


def report_avatar(db: Session, settings: Settings, limiter, user: User, public_id: str, reason: object, details: object,
                  effects) -> dict:
    """V6 phase 3: report someone's profile picture (kept as evidence; hidden at once for "minor")."""
    from app.services import media_moderation
    from app.services.messaging import REPORT_REASONS, _check_limits, _clean_details

    target = db.scalar(select(User).where(User.public_id == str(public_id).upper(), *_base_filters(user)))
    item = db.get(MediaItem, target.avatar_media_id) if target is not None and target.avatar_media_id else None
    if target is None or target.id == user.id or item is None or item.state != "attached":
        raise not_found()
    if reason not in REPORT_REASONS:
        raise AppError(400, "invalid_reason", "اختر سبب البلاغ.")
    dup = db.scalar(select(Report.id).where(Report.reporter_id == user.id, Report.media_id == item.id,
                                            Report.status == "open"))
    if dup:
        return {"id": dup, "duplicate": True}
    _check_limits(limiter, [Limit(f"report:{user.id}", settings.MAX_REPORTS_PER_HOUR, HOUR)])
    now = clock.utcnow()
    rep = Report(reporter_id=user.id, reported_user_id=target.id, media_id=item.id, reason=reason,
                 details=_clean_details(details), snapshot=json.dumps([{"content": "صورة شخصية"}], ensure_ascii=False),
                 created_at=now, expires_at=now + timedelta(seconds=settings.REPORT_RETENTION))
    db.add(rep)
    db.flush()
    media_moderation.on_report(db, settings, item, str(reason), effects)
    return {"id": rep.id, "duplicate": False}
