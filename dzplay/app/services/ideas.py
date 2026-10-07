"""Public ideas: posts, reactions, owner-only comments, public profiles.

Kept separate from private anonymous messaging (own tables, own API router).

Privacy rules enforced here (never in the frontend only):
* Every author appears as "dzplay". Posts carry an opaque profile `ref`
  (ProfileRef), never the internal user id or e-mail. The ref is never used in
  messaging, so public posts cannot be linked to private conversations.
* Comment *content* and comment counts are returned only to the post's
  author. Everyone else only learns that they can leave a comment.
* Feed ordering is computed on the server (weighted random draw).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError, not_found
from app.models import Block, Comment, Post, PostReaction, ProfileRef, Report, User
from app.services.auth import log_event
from app.services.content import clean_message
from app.services.messaging import (
    HOUR,
    DAY,
    PEER_NAME,
    REPORT_REASONS,
    Effects,
    _check_limits,
    _clean_client_id,
    _clean_details,
    _maybe_auto_suspend,
    _require_can_send,
    iso,
    parse_iso,
)
from app.services.moderation import flag_content
from app.services.rate_limit import Limit

REACTIONS = ("like", "dislike")
OFFICIAL_NAME = "DZPLAY الرسمي"


# ---------------------------------------------------------------------------
# profile references
# ---------------------------------------------------------------------------


def profile_ref_for(db: Session, user_id: str) -> str:
    ref = db.scalar(select(ProfileRef.ref).where(ProfileRef.user_id == user_id))
    if ref:
        return ref
    try:
        with db.begin_nested():
            row = ProfileRef(user_id=user_id)
            db.add(row)
        return row.ref
    except IntegrityError:  # created concurrently
        return db.scalar(select(ProfileRef.ref).where(ProfileRef.user_id == user_id))


def _refs_for(db: Session, user_ids: set[str]) -> dict[str, str]:
    if not user_ids:
        return {}
    found = dict(db.execute(select(ProfileRef.user_id, ProfileRef.ref).where(ProfileRef.user_id.in_(user_ids))).all())
    for uid in user_ids - found.keys():
        found[uid] = profile_ref_for(db, uid)
    return found


def _user_by_ref(db: Session, ref: str) -> User:
    if not isinstance(ref, str) or len(ref) > 32:
        raise not_found()
    user_id = db.scalar(select(ProfileRef.user_id).where(ProfileRef.ref == ref))
    user = db.get(User, user_id) if user_id else None
    if user is None or user.status == "banned":
        raise not_found()
    return user


# ---------------------------------------------------------------------------
# serialisation (the only place post data leaves the domain)
# ---------------------------------------------------------------------------


def _authors(db: Session, ids) -> dict[str, User]:
    ids = {i for i in ids if i}
    return {u.id: u for u in db.execute(select(User).where(User.id.in_(ids))).scalars()} if ids else {}


def _author_view(u: User | None) -> dict:
    from app.services import names

    from app.services.media_items import avatar_url

    return {"name": names.shown_name(u) if u is not None else PEER_NAME,
            "gender": names.public_gender(u) if u is not None else None,
            "verified": bool(u is not None and u.verified_at is not None),
            "avatar_url": avatar_url(u) if u is not None and not u.is_system else None}


def serialize_post(p: Post, viewer_id: str, author_ref: str, my_reaction: str | None, author: User | None = None,
                   media: dict | None = None) -> dict:
    mine = p.author_id == viewer_id
    return {
        "id": p.id,
        "content": p.content,
        "created_at": iso(p.created_at),
        "author": {**_author_view(author), "ref": author_ref},
        "mine": mine,
        "likes": p.likes_count or 0,
        "dislikes": p.dislikes_count or 0,
        "my_reaction": my_reaction,
        # Comment counts are private to the author (no public social signal).
        "comments": {"count": p.comments_count, "unseen": p.unseen_comments_count} if mine else None,
        "can_comment": not mine,
        "can_react": not mine,
        "media": media,  # V5: one picture (signed, session-bound URL) or None
        "status": p.status if mine and p.status != "visible" else None,  # pending (approval) / hidden (reports)
    }


def _serialize_many(db: Session, posts: list[Post], viewer_id: str) -> list[dict]:
    if not posts:
        return []
    refs = _refs_for(db, {p.author_id for p in posts})
    reactions = dict(db.execute(
        select(PostReaction.post_id, PostReaction.reaction_type)
        .where(PostReaction.user_id == viewer_id, PostReaction.post_id.in_([p.id for p in posts]))
    ).all())
    authors = _authors(db, {p.author_id for p in posts})
    from app.services.media_items import post_media

    return [serialize_post(p, viewer_id, refs[p.author_id], reactions.get(p.id), authors.get(p.author_id),
                           post_media(db, p, viewer_id)) for p in posts]


def _visible_post(db: Session, post_id: str) -> Post:
    if not isinstance(post_id, str) or len(post_id) > 32:
        raise not_found()
    post = db.get(Post, post_id)
    if post is None or post.status != "visible":
        raise not_found()
    return post


def _blocked_ids(db: Session, user_id: str) -> set[str]:
    rows = db.execute(select(Block.blocker_id, Block.blocked_id).where(
        or_(Block.blocker_id == user_id, Block.blocked_id == user_id))).all()
    return {b if a == user_id else a for a, b in rows}


# ---------------------------------------------------------------------------
# posts
# ---------------------------------------------------------------------------


def create_post(db: Session, settings: Settings, limiter, user: User, *, content: object, client_id: object,
                media_id: object = None, effects: Effects | None = None) -> dict:
    from app.services import media_items

    _require_can_send(user)
    cid = _clean_client_id(client_id)
    if cid:
        existing = db.scalar(select(Post).where(Post.author_id == user.id, Post.client_id == cid))
        if existing is not None:  # retried request
            return _serialize_many(db, [existing], user.id)[0]
    item = None
    if media_id is not None:  # V5: an idea with one picture; the text becomes its caption (may be empty)
        item = media_items.claim_for_idea(db, settings, user, media_id)
        text = "" if isinstance(content, str) and not content.strip() else clean_message(
            content, settings.MAX_POST_LENGTH, settings.LINK_POLICY)
        media_items.caption_check(settings, text)
    else:
        text = clean_message(content, settings.MAX_POST_LENGTH, settings.LINK_POLICY)
    digest = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:32]
    limits = [Limit(f"post_hour:{user.id}", settings.MAX_POSTS_PER_HOUR, HOUR),
              Limit(f"post_day:{user.id}", settings.MAX_POSTS_PER_DAY, DAY)]
    if text:  # a picture without a caption is not a duplicate text
        limits.append(Limit(f"dup:{user.id}:post:{digest}", 1, settings.DUPLICATE_MESSAGE_WINDOW))
    _check_limits(limiter, limits)
    now = clock.utcnow()
    post = Post(author_id=user.id, content=text, client_id=cid, created_at=now, updated_at=now,
                media_id=item.id if item is not None else None)
    db.add(post)
    db.flush()
    if item is not None:
        media_items.attach_to_post(db, settings, item, post, effects)
        db.flush()
    return _serialize_many(db, [post], user.id)[0]


def get_post(db: Session, user: User, post_id: str) -> dict:
    return _serialize_many(db, [_visible_post(db, post_id)], user.id)[0]


def delete_post(db: Session, user: User, post_id: str, effects: Effects | None = None) -> None:
    if not isinstance(post_id, str) or len(post_id) > 32:
        raise not_found()
    post = db.get(Post, post_id)
    if post is None or post.author_id != user.id or post.status == "removed":
        raise not_found()
    from app.services.media_items import remove_for_post

    remove_for_post(db, post, "owner", effects)  # V5: its picture leaves Telegram + the cache too
    db.delete(post)  # reactions + comments cascade


# ---------------------------------------------------------------------------
# feed
# ---------------------------------------------------------------------------


def _encode_cursor(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")


def _decode_cursor(cursor: str) -> dict:
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        data = json.loads(raw)
        if not (isinstance(data, dict) and isinstance(data.get("s"), str) and isinstance(data.get("o"), int)
                and isinstance(data.get("t"), str) and 0 <= data["o"] < 100_000 and len(data["s"]) <= 64):
            raise ValueError
        return data
    except (ValueError, TypeError, json.JSONDecodeError):
        raise AppError(400, "invalid_cursor", "طلب غير صالح.") from None


def feed_weight(settings: Settings, *, age_hours: float, likes: int, dislikes: int, own: bool, seen: bool) -> float:
    """How likely a post is to be drawn early. Tunable from config; randomness dominates."""
    half_life = max(0.1, settings.FEED_FRESHNESS_HALF_LIFE_HOURS)
    freshness = max(settings.FEED_FRESHNESS_FLOOR, 0.5 ** (max(0.0, age_hours) / half_life))
    net = max(0, likes - dislikes // 2)
    engagement = min(settings.FEED_ENGAGEMENT_CAP, 1.0 + settings.FEED_ENGAGEMENT_WEIGHT * math.log1p(net))
    weight = freshness * engagement
    if own:
        weight *= settings.FEED_OWN_POST_PENALTY
    if seen:
        weight *= settings.FEED_SEEN_PENALTY
    return max(weight, 1e-6)


def _draw_key(seed: str, post_id: str, weight: float) -> float:
    """Weighted random sampling key (Efraimidis–Spirakis): u ** (1 / w).

    `u` is derived from (seed, post id) so a feed session is reproducible
    across pages, while a new session (new seed) gives a new order.
    """
    h = hashlib.sha256(f"{seed}:{post_id}".encode()).digest()
    u = (int.from_bytes(h[:8], "big") + 1) / (2 ** 64 + 2)
    return u ** (1.0 / weight)


def feed(db: Session, settings: Settings, user: User, *, cursor: str | None, limit: int | None = None) -> dict:
    limit = max(1, min(limit or settings.FEED_PAGE_SIZE, 50))
    if cursor:
        c = _decode_cursor(cursor)
        seed, offset, snapshot = c["s"], c["o"], parse_iso(c["t"])
    else:
        seed, offset, snapshot = secrets.token_hex(8), 0, clock.utcnow()

    blocked = _blocked_ids(db, user.id)
    q = (
        select(Post.id, Post.author_id, Post.created_at, Post.likes_count.label("likes"),
               Post.dislikes_count.label("dislikes"))
        .where(Post.status == "visible", Post.created_at <= snapshot)
        .order_by(Post.created_at.desc())
        .limit(settings.FEED_CANDIDATE_POOL)
    )
    if blocked:
        q = q.where(Post.author_id.not_in(blocked))
    pool = db.execute(q).all()
    seen = set(db.execute(select(PostReaction.post_id).where(
        PostReaction.user_id == user.id, PostReaction.post_id.in_([r.id for r in pool]))).scalars()) if pool else set()

    keyed = []
    for r in pool:
        age_h = (snapshot - r.created_at).total_seconds() / 3600
        w = feed_weight(settings, age_hours=age_h, likes=r.likes, dislikes=r.dislikes,
                        own=r.author_id == user.id, seen=r.id in seen)
        keyed.append((_draw_key(seed, r.id, w), r.id))
    keyed.sort(reverse=True)
    page_ids = [pid for _, pid in keyed[offset:offset + limit]]

    posts_by_id = {p.id: p for p in db.execute(select(Post).where(Post.id.in_(page_ids), Post.status == "visible")).scalars()} if page_ids else {}
    page = [posts_by_id[pid] for pid in page_ids if pid in posts_by_id]
    next_offset = offset + limit
    return {
        "posts": _serialize_many(db, page, user.id),
        "next_cursor": _encode_cursor({"s": seed, "o": next_offset, "t": iso(snapshot)}) if next_offset < len(keyed) else None,
    }


# ---------------------------------------------------------------------------
# reactions
# ---------------------------------------------------------------------------


def _bump_counts(db: Session, post_id: str, old: str | None, new: str | None) -> None:
    delta = {"like": 0, "dislike": 0}
    if old:
        delta[old] -= 1
    if new:
        delta[new] += 1
    if delta["like"] or delta["dislike"]:
        db.execute(update(Post).where(Post.id == post_id).values(
            likes_count=Post.likes_count + delta["like"],
            dislikes_count=Post.dislikes_count + delta["dislike"],
        ))


def set_reaction(db: Session, settings: Settings, limiter, user: User, post_id: str, reaction: object) -> dict:
    if reaction is not None and reaction not in REACTIONS:
        raise AppError(400, "invalid_reaction", "تفاعل غير صالح.")
    post = _visible_post(db, post_id)
    if post.author_id == user.id:
        raise AppError(400, "own_post", "لا يمكنك التفاعل مع منشورك.")
    if user.status == "banned":
        raise AppError(403, "account_banned", "تم إيقاف هذا الحساب.")
    _check_limits(limiter, [Limit(f"react:{user.id}", settings.MAX_REACTIONS_PER_MINUTE, 60)])

    for _attempt in range(2):
        existing = db.scalar(select(PostReaction).where(PostReaction.post_id == post.id, PostReaction.user_id == user.id))
        old = existing.reaction_type if existing else None
        if old == reaction:
            break
        try:
            with db.begin_nested():
                if reaction is None:
                    db.delete(existing)
                elif existing is None:
                    db.add(PostReaction(post_id=post.id, user_id=user.id, reaction_type=reaction))
                else:
                    existing.reaction_type = reaction
                    existing.updated_at = clock.utcnow()
                db.flush()
                _bump_counts(db, post.id, old, reaction)
            break
        except IntegrityError:  # a concurrent request inserted first: re-read and apply as a change
            continue
    db.refresh(post)
    return {"likes": post.likes_count or 0, "dislikes": post.dislikes_count or 0,
            "my_reaction": reaction}


# ---------------------------------------------------------------------------
# comments (visible to the post author only)
# ---------------------------------------------------------------------------


def _is_blocked_pair(db: Session, a: str, b: str) -> bool:
    return bool(db.scalar(select(Block.id).where(
        or_(and_(Block.blocker_id == a, Block.blocked_id == b), and_(Block.blocker_id == b, Block.blocked_id == a))).limit(1)))


def add_comment(db: Session, settings: Settings, limiter, user: User, post_id: str, *, content: object,
                effects: Effects) -> dict:
    _require_can_send(user)
    post = _visible_post(db, post_id)
    if post.author_id == user.id:
        raise AppError(400, "own_post", "التعليقات تصلك أنت من الآخرين، لا يمكنك التعليق على منشورك.")
    text = clean_message(content, settings.MAX_COMMENT_LENGTH, settings.LINK_POLICY)
    if _is_blocked_pair(db, user.id, post.author_id):
        raise AppError(403, "comment_blocked", "لا يمكنك التعليق على هذا المنشور.")
    _check_limits(limiter, [
        Limit(f"comment_min:{user.id}", settings.MAX_COMMENTS_PER_MINUTE, 60),
        Limit(f"comment_hour:{user.id}", settings.MAX_COMMENTS_PER_HOUR, HOUR),
    ])
    comment = Comment(post_id=post.id, author_id=user.id, content=text, created_at=clock.utcnow())
    db.add(comment)
    db.execute(update(Post).where(Post.id == post.id).values(
        comments_count=Post.comments_count + 1, unseen_comments_count=Post.unseen_comments_count + 1))
    db.flush()
    flag_content(db, settings, target="comment", text=text, offender_id=user.id, victim_id=post.author_id,
                 comment_id=comment.id, post_id=post.id)
    effects.signal(post.author_id, "comment")
    # The writer gets no copy back: comments are for the author's eyes only.
    return {"ok": True, "visible_to": "author_only"}


def list_comments(db: Session, user: User, post_id: str, *, before: str | None = None, limit: int = 50) -> dict:
    post = _visible_post(db, post_id)
    if post.author_id != user.id:  # server-side owner check — the only way comments are ever returned
        raise AppError(403, "comments_private", "التعليقات مرئية لصاحب المنشور فقط.")
    limit = max(1, min(limit, 100))
    blocked = _blocked_ids(db, user.id)
    q = select(Comment).where(Comment.post_id == post.id)
    if blocked:
        q = q.where(Comment.author_id.not_in(blocked))
    before_dt = parse_iso(before)
    if before_dt:
        q = q.where(Comment.created_at < before_dt)
    rows = list(db.execute(q.order_by(Comment.created_at.desc(), Comment.id.desc()).limit(limit + 1)).scalars())
    if post.unseen_comments_count:
        post.unseen_comments_count = 0
    official = set(db.execute(select(User.id).where(User.is_official.is_(True))).scalars())
    authors = _authors(db, {c.author_id for c in rows[:limit]})
    return {
        "post_id": post.id,
        "comments": [{"id": c.id, "author": OFFICIAL_NAME if c.author_id in official else _author_view(authors.get(c.author_id))["name"],
                      "official": c.author_id in official, "content": c.content, "created_at": iso(c.created_at)}
                     for c in rows[:limit]],
        "has_more": len(rows) > limit,
    }


def _own_comment(db: Session, user: User, comment_id: str) -> tuple[Comment, Post]:
    comment = db.get(Comment, comment_id) if isinstance(comment_id, str) and len(comment_id) <= 32 else None
    post = db.get(Post, comment.post_id) if comment else None
    if comment is None or post is None or post.author_id != user.id:
        raise not_found()
    return comment, post


def delete_comment(db: Session, user: User, comment_id: str) -> None:
    comment, post = _own_comment(db, user, comment_id)
    db.delete(comment)
    db.execute(update(Post).where(Post.id == post.id).values(
        comments_count=case((Post.comments_count > 0, Post.comments_count - 1), else_=0)))


def block_commenter(db: Session, user: User, comment_id: str) -> None:
    comment, _post = _own_comment(db, user, comment_id)
    if comment.author_id != user.id and not db.scalar(
            select(Block.id).where(Block.blocker_id == user.id, Block.blocked_id == comment.author_id)):
        db.add(Block(blocker_id=user.id, blocked_id=comment.author_id, created_at=clock.utcnow()))
    log_event(db, "block_commenter", None, user.id)


# ---------------------------------------------------------------------------
# reports on public content
# ---------------------------------------------------------------------------


def report_post(db: Session, settings: Settings, limiter, user: User, post_id: str, *, reason: object, details: object,
                effects: Effects | None = None) -> dict:
    post = _visible_post(db, post_id)
    if post.author_id == user.id:
        raise AppError(400, "cannot_report_own", "لا يمكنك الإبلاغ عن منشورك.")
    result = _file_report(db, settings, limiter, user, reported_id=post.author_id, post_id=post.id, comment_id=None,
                          evidence=[(post.content, post.created_at)], reason=reason, details=details,
                          media_id=post.media_id)
    if post.media_id and not result["duplicate"]:
        from app.models import MediaItem
        from app.services.media_moderation import on_report

        item = db.get(MediaItem, post.media_id)
        if item is not None and item.state == "attached":
            on_report(db, settings, item, str(reason), effects if effects is not None else Effects())
    return result


def report_comment(db: Session, settings: Settings, limiter, user: User, comment_id: str, *, reason: object,
                   details: object) -> dict:
    comment, post = _own_comment(db, user, comment_id)  # only the post author can see (and so report) comments
    return _file_report(db, settings, limiter, user, reported_id=comment.author_id, post_id=post.id,
                        comment_id=comment.id, evidence=[(comment.content, comment.created_at)], reason=reason,
                        details=details)


def _file_report(db: Session, settings: Settings, limiter, user: User, *, reported_id: str, post_id: str,
                 comment_id: str | None, evidence: list[tuple[str, datetime]], reason: object, details: object,
                 media_id: str | None = None) -> dict:
    if reason not in REPORT_REASONS:
        raise AppError(400, "invalid_reason", "اختر سبب البلاغ.")
    details_c = _clean_details(details)
    dup_q = select(Report.id).where(Report.reporter_id == user.id, Report.post_id == post_id, Report.status == "open")
    dup_q = dup_q.where(Report.comment_id == comment_id) if comment_id else dup_q.where(Report.comment_id.is_(None))
    dup = db.scalar(dup_q)
    if dup:
        return {"id": dup, "duplicate": True}
    _check_limits(limiter, [Limit(f"report:{user.id}", settings.MAX_REPORTS_PER_HOUR, HOUR)])
    now = clock.utcnow()
    rep = Report(
        reporter_id=user.id, reported_user_id=reported_id, post_id=post_id, comment_id=comment_id, media_id=media_id,
        reason=reason, details=details_c,
        snapshot=json.dumps([{"content": c, "created_at": iso(t)} for c, t in evidence], ensure_ascii=False),
        created_at=now, expires_at=now + timedelta(seconds=settings.REPORT_RETENTION),
    )
    db.add(rep)
    db.flush()
    log_event(db, "report", None, user.id, f"{'comment' if comment_id else 'post'}:{reason}")
    _maybe_auto_suspend(db, settings, reported_id)
    return {"id": rep.id, "duplicate": False}


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def idea_stats(db: Session, user_id: str) -> dict:
    posts, likes, dislikes = db.execute(
        select(func.count(Post.id), func.coalesce(func.sum(Post.likes_count), 0),
               func.coalesce(func.sum(Post.dislikes_count), 0))
        .where(Post.author_id == user_id, Post.status == "visible")
    ).one()
    return {"posts": int(posts or 0), "likes": int(likes or 0), "dislikes": int(dislikes or 0)}


def unseen_comments(db: Session, user_id: str) -> int:
    return int(db.scalar(select(func.coalesce(func.sum(Post.unseen_comments_count), 0))
                         .where(Post.author_id == user_id, Post.status == "visible")) or 0)


def public_profile(db: Session, viewer: User, ref: str) -> dict:
    from app.services import names
    from app.services.media_items import avatar_url

    owner = _user_by_ref(db, ref)
    return {
        "ref": ref,
        "name": names.shown_name(owner),
        "gender": names.public_gender(owner),
        "public_id": owner.public_id,
        "verified": owner.verified_at is not None,
        "avatar_url": avatar_url(owner),
        "is_me": owner.id == viewer.id,
        "stats": idea_stats(db, owner.id),  # public stats only — never messaging stats or personal data
    }


def profile_posts(db: Session, viewer: User, ref: str, *, before: str | None, limit: int = 15) -> dict:
    owner = _user_by_ref(db, ref)
    limit = max(1, min(limit, 50))
    q = select(Post).where(Post.author_id == owner.id, Post.status == "visible")
    before_dt = parse_iso(before)
    if before_dt:
        q = q.where(Post.created_at < before_dt)
    rows = list(db.execute(q.order_by(Post.created_at.desc(), Post.id.desc()).limit(limit + 1)).scalars())
    page = rows[:limit]
    return {
        "posts": _serialize_many(db, page, viewer.id),
        "next_before": iso(page[-1].created_at) if len(rows) > limit and page else None,
    }


def own_profile(db: Session, user: User, settings=None) -> dict:
    """The signed-in user's own profile: public idea stats + private messaging stats."""
    from app.services.messaging import profile as messaging_profile

    data = messaging_profile(user, settings)
    data["ref"] = profile_ref_for(db, user.id)
    data["ideas"] = idea_stats(db, user.id)
    data["unseen_comments"] = unseen_comments(db, user.id)
    from app.services.support import unread_count

    data["support_unread"] = unread_count(db, user.id)  # V5: support replies not read yet
    return data
