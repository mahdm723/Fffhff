"""Reels: platform content (uploaded by admins via Telegram), reactions, public comments.

Reel comments are PUBLIC (everyone signed in reads them) and live in their own
table/endpoints (ReelComment, /api/reels/.../comments). They share nothing with
Ideas comments, which stay visible to the post owner only.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import HOUR, Settings
from app.errors import AppError, not_found
from app.models import Reel, ReelAsset, ReelComment, ReelReaction, ReelView, Report, User
from app.services import reels_ranking
from app.services.auth import log_event
from app.services.content import clean_message
from app.services.counts import shown
from app.services.ideas import REACTIONS, _blocked_ids
from app.services.media import VARIANTS
from app.services.messaging import (
    PEER_NAME,
    REPORT_REASONS,
    _check_limits,
    _clean_details,
    _maybe_auto_suspend,
    _require_can_send,
    iso,
    parse_iso,
)
from app.services.moderation import flag_content
from app.services.rate_limit import Limit

OFFICIAL_NAME = "DZPLAY الرسمي"
_SHORT_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"


# ---------------------------------------------------------------------------
# signed media URLs (short-lived, bound to the viewer's session)
# ---------------------------------------------------------------------------


def session_key(token: str | None) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()[:32]


def _media_sig(settings: Settings, asset_id: str, variant: str, exp: int, skey: str) -> str:
    msg = f"media:{asset_id}:{variant}:{exp}:{skey}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()[:40]


def media_url(settings: Settings, asset_id: str, variant: str, skey: str) -> str:
    # Expiry rounded to half the TTL so URLs stay identical for a while (better HTTP/SW caching).
    half = max(60, settings.MEDIA_URL_TTL // 2)
    exp = (int(clock.timestamp()) // half + 2) * half
    return f"/media/{asset_id}/{variant}?e={exp}&s={_media_sig(settings, asset_id, variant, exp, skey)}"


def verify_media_sig(settings: Settings, asset_id: str, variant: str, exp: int, sig: str, skey: str) -> bool:
    if exp < clock.timestamp() or exp > clock.timestamp() + settings.MEDIA_URL_TTL * 2:
        return False
    return hmac.compare_digest(_media_sig(settings, asset_id, variant, exp, skey), sig or "")


# ---------------------------------------------------------------------------
# creation & admin operations (Telegram bot / admin panel)
# ---------------------------------------------------------------------------


def _new_short_id(db: Session) -> str:
    while True:
        sid = "".join(secrets.choice(_SHORT_ALPHABET) for _ in range(6))
        if not db.scalar(select(Reel.id).where(Reel.short_id == sid)):
            return sid


def create_reel(db: Session, settings: Settings, kind: str, caption: str | None, media_group_id: str | None = None) -> Reel:
    reel = Reel(short_id=_new_short_id(db), kind=kind, caption=clean_caption(settings, caption),
                media_group_id=media_group_id, status="processing")
    db.add(reel)
    db.flush()
    return reel


def clean_caption(settings: Settings, caption: str | None) -> str:
    if not caption:
        return ""
    # Same sanitizer as user content (no HTML, control chars stripped); links allowed as plain text.
    return clean_message(caption, settings.MAX_REEL_CAPTION_LENGTH, "allow_plain") if caption.strip() else ""


def add_asset(db: Session, reel: Reel, *, kind: str, file_id: str, unique_id: str | None, size: int | None,
              message_id: int | None, width: int | None = None, height: int | None = None) -> ReelAsset:
    position = db.scalar(select(func.count()).select_from(ReelAsset).where(ReelAsset.reel_id == reel.id)) or 0
    asset = ReelAsset(reel_id=reel.id, position=position, kind=kind, tg_file_id=file_id, tg_unique_id=unique_id,
                      source_size=size, tg_message_id=message_id, width=width, height=height)
    db.add(asset)
    db.flush()
    return asset


def assets_of(db: Session, reel_id: str) -> list[ReelAsset]:
    # Album items keep the order they were sent in (Telegram message ids increase).
    return list(db.execute(select(ReelAsset).where(ReelAsset.reel_id == reel_id)
                           .order_by(ReelAsset.tg_message_id, ReelAsset.position)).scalars())


def find(db: Session, ref: str) -> Reel | None:
    ref = (ref or "").strip()
    if not ref or len(ref) > 32:
        return None
    # short ids are lower-case (typed in Telegram); full ids are case-sensitive
    return db.scalar(select(Reel).where(or_(Reel.short_id == ref.lower(), Reel.id == ref)))


def set_status(db: Session, reel: Reel, status: str) -> None:
    if status not in ("visible", "hidden"):
        raise AppError(400, "invalid_status", "invalid status")
    reel.status = status
    reel.updated_at = clock.utcnow()


def pin(db: Session, settings: Settings, reel: Reel, hours: int | None = None) -> datetime:
    reel.pinned_until = clock.utcnow() + timedelta(hours=hours or settings.REELS_PIN_HOURS)
    return reel.pinned_until


def unpin(reel: Reel) -> None:
    reel.pinned_until = None


def delete_reel(db: Session, store, reel: Reel) -> None:
    for asset in assets_of(db, reel.id):
        if store is not None:
            store.forget_asset(db, asset.id)
    db.delete(reel)


def edit_caption(db: Session, settings: Settings, reel: Reel, caption: str) -> None:
    reel.caption = clean_caption(settings, caption)
    reel.updated_at = clock.utcnow()


def recent(db: Session, limit: int = 15) -> list[Reel]:
    return list(db.execute(select(Reel).order_by(Reel.created_at.desc()).limit(limit)).scalars())


def summary(db: Session) -> dict:
    day = clock.utcnow() - timedelta(days=1)

    def count(model, *where) -> int:
        return db.scalar(select(func.count()).select_from(model).where(*where)) or 0

    return {
        "total": count(Reel),
        "visible": count(Reel, Reel.status == "visible"),
        "hidden": count(Reel, Reel.status == "hidden"),
        "processing": count(Reel, Reel.status == "processing"),
        "failed": count(Reel, Reel.status == "failed"),
        "reactions_24h": count(ReelReaction, ReelReaction.created_at > day),
        "comments_24h": count(ReelComment, ReelComment.created_at > day),
        "views_24h": count(ReelView, ReelView.seen_at > day),
        "likes": int(db.scalar(select(func.coalesce(func.sum(Reel.likes_count), 0))) or 0),
        "dislikes": int(db.scalar(select(func.coalesce(func.sum(Reel.dislikes_count), 0))) or 0),
        "comments": count(ReelComment),
    }


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


def _encode_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        data = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
        if not (isinstance(data, dict) and isinstance(data.get("s"), str) and isinstance(data.get("o"), int)
                and isinstance(data.get("t"), str) and 0 <= data["o"] < 100_000 and len(data["s"]) <= 64):
            raise ValueError
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AppError(400, "invalid_cursor", "طلب غير صالح.") from None


def serialize(settings: Settings, reel: Reel, assets: list[ReelAsset], my_reaction: str | None, skey: str) -> dict:
    media = []
    for a in assets:
        item = {"type": a.kind, "width": a.width, "height": a.height}
        if a.kind == "video":
            item["src"] = media_url(settings, a.id, "mp4", skey)
            item["poster"] = media_url(settings, a.id, "poster", skey)
            item["duration"] = a.duration
        else:
            item["src"] = media_url(settings, a.id, "img", skey)
        media.append(item)
    return {
        "id": reel.id, "kind": reel.kind, "caption": reel.caption, "media": media,
        "likes": shown(reel.likes_count, reel.boost_likes), "dislikes": shown(reel.dislikes_count, reel.boost_dislikes),
        "comments": reel.comments_count,
        "my_reaction": my_reaction, "created_at": iso(reel.created_at),
    }


def feed(db: Session, settings: Settings, user: User, skey: str, cursor: str | None, limit: int | None = None) -> dict:
    limit = min(max(1, limit or settings.REELS_PAGE_SIZE), 50)
    if cursor:
        c = _decode_cursor(cursor)
        seed, offset, snapshot = c["s"], c["o"], parse_iso(c["t"])
    else:
        seed, offset, snapshot = secrets.token_hex(8), 0, clock.utcnow()
    rows = db.execute(select(Reel.id, Reel.created_at, Reel.pinned_until)
                      .where(Reel.status == "visible", Reel.created_at <= snapshot)).all()
    seen_since = snapshot - timedelta(seconds=settings.REELS_SEEN_TTL)
    seen = set(db.execute(select(ReelView.reel_id).where(
        ReelView.user_id == user.id, ReelView.seen_at > seen_since, ReelView.seen_at <= snapshot)).scalars())
    ordered = reels_ranking.order(settings, [reels_ranking.Candidate(*r) for r in rows], seen, seed, snapshot)
    page_ids = ordered[offset:offset + limit]
    reels = {r.id: r for r in db.execute(select(Reel).where(Reel.id.in_(page_ids))).scalars()} if page_ids else {}
    assets: dict[str, list[ReelAsset]] = {}
    if page_ids:
        for a in db.execute(select(ReelAsset).where(ReelAsset.reel_id.in_(page_ids))
                            .order_by(ReelAsset.tg_message_id, ReelAsset.position)).scalars():
            assets.setdefault(a.reel_id, []).append(a)
    mine = dict(db.execute(select(ReelReaction.reel_id, ReelReaction.reaction).where(
        ReelReaction.user_id == user.id, ReelReaction.reel_id.in_(page_ids))).all()) if page_ids else {}
    items = [serialize(settings, reels[i], assets.get(i, []), mine.get(i), skey) for i in page_ids if i in reels]
    next_offset = offset + limit
    return {
        "reels": items,
        "next_cursor": _encode_cursor({"s": seed, "o": next_offset, "t": iso(snapshot)}) if next_offset < len(ordered) else None,
        "prefetch": {"count": settings.PREFETCH_COUNT, "ahead": settings.PREFETCH_AHEAD,
                     "device_cache_mb": settings.DEVICE_MEDIA_CACHE_MB},
    }


def _visible_reel(db: Session, reel_id: str) -> Reel:
    if not isinstance(reel_id, str) or len(reel_id) > 32:
        raise not_found()
    reel = db.get(Reel, reel_id)
    if reel is None or reel.status != "visible":
        raise not_found()
    return reel


def mark_seen(db: Session, settings: Settings, limiter, user: User, reel_id: str) -> dict:
    reel = _visible_reel(db, reel_id)
    _check_limits(limiter, [Limit(f"reelview:{user.id}", 120, 60)])
    now = clock.utcnow()
    view = db.scalar(select(ReelView).where(ReelView.user_id == user.id, ReelView.reel_id == reel.id))
    if view is None:
        try:
            with db.begin_nested():
                db.add(ReelView(user_id=user.id, reel_id=reel.id, seen_at=now))
                db.flush()
            db.execute(update(Reel).where(Reel.id == reel.id).values(views_count=Reel.views_count + 1))
        except IntegrityError:
            pass
    elif now - view.seen_at > timedelta(minutes=30):
        view.seen_at = now
    return {"ok": True}


def purge_views(db: Session, settings: Settings) -> int:
    cutoff = clock.utcnow() - timedelta(seconds=settings.REELS_SEEN_TTL)
    return db.execute(delete(ReelView).where(ReelView.seen_at < cutoff)).rowcount or 0


# ---------------------------------------------------------------------------
# reactions (same rules as Ideas: one per user, switch or remove)
# ---------------------------------------------------------------------------


def _bump(db: Session, reel_id: str, old: str | None, new: str | None) -> None:
    delta = {"like": 0, "dislike": 0}
    if old:
        delta[old] -= 1
    if new:
        delta[new] += 1
    if delta["like"] or delta["dislike"]:
        db.execute(update(Reel).where(Reel.id == reel_id).values(
            likes_count=Reel.likes_count + delta["like"], dislikes_count=Reel.dislikes_count + delta["dislike"]))


def set_reaction(db: Session, settings: Settings, limiter, user: User, reel_id: str, reaction: object) -> dict:
    if reaction is not None and reaction not in REACTIONS:
        raise AppError(400, "invalid_reaction", "تفاعل غير صالح.")
    reel = _visible_reel(db, reel_id)
    if user.status == "banned":
        raise AppError(403, "account_banned", "تم إيقاف هذا الحساب.")
    _check_limits(limiter, [Limit(f"react:{user.id}", settings.MAX_REACTIONS_PER_MINUTE, 60)])
    for _attempt in range(2):
        existing = db.scalar(select(ReelReaction).where(ReelReaction.reel_id == reel.id, ReelReaction.user_id == user.id))
        old = existing.reaction if existing else None
        if old == reaction:
            break
        try:
            with db.begin_nested():
                if reaction is None:
                    db.delete(existing)
                elif existing is None:
                    db.add(ReelReaction(reel_id=reel.id, user_id=user.id, reaction=reaction))
                else:
                    existing.reaction = reaction
                db.flush()
                _bump(db, reel.id, old, reaction)
            break
        except IntegrityError:
            continue
    db.refresh(reel)
    return {"likes": shown(reel.likes_count, reel.boost_likes), "dislikes": shown(reel.dislikes_count, reel.boost_dislikes),
            "my_reaction": reaction}


# ---------------------------------------------------------------------------
# public comments
# ---------------------------------------------------------------------------


def _official_ids(db: Session) -> set[str]:
    return set(db.execute(select(User.id).where(User.is_official.is_(True))).scalars())


def serialize_comment(c: ReelComment, viewer_id: str | None, official: set[str]) -> dict:
    is_official = c.author_id in official
    return {
        "id": c.id, "content": c.content, "created_at": iso(c.created_at),
        "author": {"name": OFFICIAL_NAME if is_official else PEER_NAME, "official": is_official},
        "mine": c.author_id == viewer_id,
    }


def list_comments(db: Session, settings: Settings, user: User, reel_id: str, cursor: str | None) -> dict:
    reel = _visible_reel(db, reel_id)
    size = settings.REEL_COMMENTS_PAGE_SIZE
    q = select(ReelComment).where(ReelComment.reel_id == reel.id)
    blocked = _blocked_ids(db, user.id)
    if blocked:
        q = q.where(ReelComment.author_id.not_in(blocked))
    if cursor:
        try:
            raw = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)))
            before_t, before_id = parse_iso(raw["t"]), str(raw["i"])[:32]
        except (ValueError, KeyError, TypeError):
            raise AppError(400, "invalid_cursor", "طلب غير صالح.") from None
        q = q.where(or_(ReelComment.created_at < before_t, and_(ReelComment.created_at == before_t, ReelComment.id < before_id)))
    rows = list(db.execute(q.order_by(ReelComment.created_at.desc(), ReelComment.id.desc()).limit(size + 1)).scalars())
    more = len(rows) > size
    rows = rows[:size]
    official = _official_ids(db)
    nxt = None
    if more and rows:
        last = rows[-1]
        nxt = base64.urlsafe_b64encode(json.dumps({"t": iso(last.created_at), "i": last.id}).encode()).decode().rstrip("=")
    return {"comments": [serialize_comment(c, user.id, official) for c in rows], "next_cursor": nxt,
            "total": reel.comments_count}


def add_comment(db: Session, settings: Settings, limiter, user: User, reel_id: str, *, content: object,
                skip_limits: bool = False) -> dict:
    reel = _visible_reel(db, reel_id)
    if not user.is_official:
        _require_can_send(user)
    text = clean_message(content, settings.MAX_REEL_COMMENT_LENGTH, settings.LINK_POLICY)
    if not skip_limits:
        _check_limits(limiter, [
            Limit(f"reelcomment_min:{user.id}", settings.MAX_REEL_COMMENTS_PER_MINUTE, 60),
            Limit(f"reelcomment_hour:{user.id}", settings.MAX_REEL_COMMENTS_PER_HOUR, HOUR),
        ])
    comment = ReelComment(reel_id=reel.id, author_id=user.id, content=text, created_at=clock.utcnow())
    db.add(comment)
    db.execute(update(Reel).where(Reel.id == reel.id).values(comments_count=Reel.comments_count + 1))
    db.flush()
    if not user.is_official:
        flag_content(db, settings, target="reel_comment", text=text, offender_id=user.id, victim_id=None,
                     comment_id=comment.id, post_id=reel.id)
    return {"comment": serialize_comment(comment, user.id, _official_ids(db) if user.is_official else set())}


def _comment(db: Session, comment_id: str) -> ReelComment:
    if not isinstance(comment_id, str) or len(comment_id) > 32:
        raise not_found()
    c = db.get(ReelComment, comment_id)
    if c is None:
        raise not_found()
    return c


def remove_comment(db: Session, comment: ReelComment) -> None:
    db.execute(update(Reel).where(Reel.id == comment.reel_id, Reel.comments_count > 0)
               .values(comments_count=Reel.comments_count - 1))
    db.delete(comment)


def delete_comment(db: Session, user: User, comment_id: str) -> None:
    c = _comment(db, comment_id)
    if c.author_id != user.id:  # only the writer can delete their comment
        raise not_found()
    remove_comment(db, c)


def report_comment(db: Session, settings: Settings, limiter, user: User, comment_id: str, *, reason: object,
                   details: object) -> dict:
    c = _comment(db, comment_id)
    _visible_reel(db, c.reel_id)
    if c.author_id == user.id:
        raise AppError(400, "cannot_report_own", "لا يمكنك الإبلاغ عن تعليقك.")
    if reason not in REPORT_REASONS:
        raise AppError(400, "invalid_reason", "اختر سبب البلاغ.")
    details_c = _clean_details(details)
    dup = db.scalar(select(Report.id).where(Report.reporter_id == user.id, Report.reel_comment_id == c.id,
                                            Report.status == "open"))
    if dup:
        return {"id": dup, "duplicate": True}
    _check_limits(limiter, [Limit(f"report:{user.id}", settings.MAX_REPORTS_PER_HOUR, HOUR)])
    now = clock.utcnow()
    rep = Report(reporter_id=user.id, reported_user_id=c.author_id, reel_comment_id=c.id,
                 reason=reason, details=details_c,
                 snapshot=json.dumps([{"content": c.content, "created_at": iso(c.created_at)}], ensure_ascii=False),
                 created_at=now, expires_at=now + timedelta(seconds=settings.REPORT_RETENTION))
    db.add(rep)
    db.flush()
    log_event(db, "report", None, user.id, f"reel_comment:{reason}")
    _maybe_auto_suspend(db, settings, c.author_id)
    return {"id": rep.id, "duplicate": False}


def variants_for(kind: str) -> tuple[str, ...]:
    return VARIANTS.get(kind, ())
