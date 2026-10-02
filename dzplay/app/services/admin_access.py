"""Full read access for the admin panel (disclosed to users in the privacy policy v3).

Users, their e-mails and activity, every Idea with all its comments (including
the owner-only ones), every Reel with its comments, every stored conversation
with both real participants, text search, deletion of any content, and the
system status. Every call is audited by the API layer.

Never exposed: password hashes, TOTP secrets, session tokens, raw IP addresses
(only short network fingerprints exist).
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from app import clock
from app.errors import AppError, not_found
from app.models import (
    AuthSession,
    Comment,
    ContentFlag,
    Conversation,
    Message,
    PasswordReset,
    Post,
    PostReaction,
    ProfileRef,
    Reel,
    ReelComment,
    ReelReaction,
    Report,
    SecurityEvent,
    User,
)
from app.security.sessions import revoke_all_sessions
from app.services.messaging import iso, parse_iso

PAGE_MAX = 100


def _like(q: str) -> str:
    """LIKE pattern with the user's text escaped (no wildcard injection)."""
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _team_label(u: User | None) -> str | None:
    if u is None:
        return None
    return "official" if u.is_official else "system" if u.is_system else None


def _who(u: User | None) -> dict | None:
    if u is None:
        return None
    return {"id": u.id, "email": None if (u.is_official or u.is_system) else u.email, "team": _team_label(u),
            "status": u.status}


def _users_by_id(db: Session, ids) -> dict[str, User]:
    ids = {i for i in ids if i}
    return {u.id: u for u in db.execute(select(User).where(User.id.in_(ids))).scalars()} if ids else {}


def networks_of(db: Session, user_id: str, days: int = 30) -> list[str]:
    since = clock.utcnow() - timedelta(days=days)
    refs = set(db.execute(select(SecurityEvent.ip_hash).where(
        SecurityEvent.user_id == user_id, SecurityEvent.ip_hash.is_not(None), SecurityEvent.created_at > since)).scalars())
    reg = db.scalar(select(User.registration_ip_hash).where(User.id == user_id))
    if reg:
        refs.add(reg)
    return sorted(refs)


# ---------------------------------------------------------------------------
# users
# ---------------------------------------------------------------------------


def users_search(db: Session, *, q: str = "", status: str = "", method: str = "", flagged: bool = False,
                 has_posts: bool = False, created_from: str | None = None, created_to: str | None = None,
                 include_team: bool = False, page: int = 0, size: int = 50) -> dict:
    size = max(1, min(size, PAGE_MAX))
    stmt = select(User)
    if not include_team:
        stmt = stmt.where(User.is_official.is_not(True), User.is_system.is_not(True))
    q = (q or "").strip()
    if q:
        ref_owner = select(ProfileRef.user_id).where(ProfileRef.ref == q)
        stmt = stmt.where(or_(User.email.ilike(_like(q.lower()), escape="\\"), User.id == q, User.id.in_(ref_owner)))
    if status in ("active", "suspended", "banned"):
        stmt = stmt.where(User.status == status)
    if method == "google":
        stmt = stmt.where(User.google_sub.is_not(None))
    elif method == "email":
        stmt = stmt.where(User.password_hash.is_not(None))
    if flagged:
        stmt = stmt.where(or_(User.id.in_(select(Report.reported_user_id)), User.id.in_(select(ContentFlag.offender_id))))
    if has_posts:
        stmt = stmt.where(User.id.in_(select(Post.author_id)))
    if parse_iso(created_from):
        stmt = stmt.where(User.created_at >= parse_iso(created_from))
    if parse_iso(created_to):
        stmt = stmt.where(User.created_at <= parse_iso(created_to))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.execute(stmt.order_by(User.created_at.desc()).offset(max(0, page) * size).limit(size)).scalars())
    ids = [u.id for u in rows]
    posts = dict(db.execute(select(Post.author_id, func.count()).where(Post.author_id.in_(ids)).group_by(Post.author_id)).all()) if ids else {}
    reports = dict(db.execute(select(Report.reported_user_id, func.count()).where(Report.reported_user_id.in_(ids))
                              .group_by(Report.reported_user_id)).all()) if ids else {}
    flags = dict(db.execute(select(ContentFlag.offender_id, func.count()).where(ContentFlag.offender_id.in_(ids))
                            .group_by(ContentFlag.offender_id)).all()) if ids else {}
    return {"total": total, "page": page, "size": size, "users": [{
        "id": u.id, "email": u.email, "method": "google" if u.google_sub and not u.password_hash else
        ("google+email" if u.google_sub else "email"),
        "status": u.status, "created_at": iso(u.created_at), "last_active_at": iso(u.last_active_at),
        "team": _team_label(u), "networks": [n[:12] for n in networks_of(db, u.id)][:5],
        "stats": {"posts": posts.get(u.id, 0), "messages_sent": u.messages_sent, "messages_received": u.messages_received,
                  "conversations": u.conversations_count, "reports_against": reports.get(u.id, 0), "flags": flags.get(u.id, 0)},
    } for u in rows]}


def user_detail(db: Session, user_id: str) -> dict:
    u = db.get(User, user_id[:32])
    if u is None:
        raise not_found()
    posts = list(db.execute(select(Post).where(Post.author_id == u.id).order_by(Post.created_at.desc()).limit(100)).scalars())
    comments = db.execute(select(Comment).where(Comment.author_id == u.id).order_by(Comment.created_at.desc()).limit(100)).scalars()
    rcomments = db.execute(select(ReelComment).where(ReelComment.author_id == u.id).order_by(ReelComment.created_at.desc()).limit(100)).scalars()
    reactions = db.execute(select(PostReaction.post_id, PostReaction.reaction_type, PostReaction.created_at)
                           .where(PostReaction.user_id == u.id).order_by(PostReaction.created_at.desc()).limit(100)).all()
    rreactions = db.execute(select(ReelReaction.reel_id, ReelReaction.reaction, ReelReaction.created_at)
                            .where(ReelReaction.user_id == u.id).order_by(ReelReaction.created_at.desc()).limit(100)).all()
    convs = list(db.execute(select(Conversation).where(or_(Conversation.initiator_id == u.id, Conversation.recipient_id == u.id))
                            .order_by(Conversation.last_message_at.desc()).limit(100)).scalars())
    peers = _users_by_id(db, [c.peer_of(u.id) for c in convs])
    msg_counts = dict(db.execute(select(Message.conversation_id, func.count()).where(
        Message.conversation_id.in_([c.id for c in convs])).group_by(Message.conversation_id)).all()) if convs else {}
    rep_against = db.execute(select(Report).where(Report.reported_user_id == u.id).order_by(Report.created_at.desc()).limit(50)).scalars()
    rep_by = db.execute(select(Report).where(Report.reporter_id == u.id).order_by(Report.created_at.desc()).limit(50)).scalars()
    events = db.execute(select(SecurityEvent).where(SecurityEvent.user_id == u.id)
                        .order_by(SecurityEvent.created_at.desc()).limit(100)).scalars()
    nets = networks_of(db, u.id)
    shared = []
    if nets:
        others = set(db.execute(select(SecurityEvent.user_id).where(SecurityEvent.ip_hash.in_(nets),
                                                                    SecurityEvent.user_id.is_not(None), SecurityEvent.user_id != u.id)).scalars())
        others |= set(db.execute(select(User.id).where(User.registration_ip_hash.in_(nets), User.id != u.id)).scalars())
        shared = [_who(x) for x in _users_by_id(db, others).values()][:30]
    ref = db.scalar(select(ProfileRef.ref).where(ProfileRef.user_id == u.id))
    sessions = db.scalar(select(func.count()).select_from(AuthSession).where(
        AuthSession.user_id == u.id, AuthSession.expires_at > clock.utcnow())) or 0
    return {
        "user": {"id": u.id, "email": u.email, "team": _team_label(u), "status": u.status,
                 "method": "google" if u.google_sub and not u.password_hash else ("google+email" if u.google_sub else "email"),
                 "created_at": iso(u.created_at), "last_active_at": iso(u.last_active_at), "profile_ref": ref,
                 "sessions_active": sessions, "stats": {"messages_sent": u.messages_sent, "messages_received": u.messages_received,
                                                        "conversations": u.conversations_count}},
        "networks": [n[:12] for n in nets],
        "shared_network_accounts": shared,
        "posts": [_post(p) for p in posts],
        "idea_comments": [{"id": c.id, "post_id": c.post_id, "content": c.content, "created_at": iso(c.created_at)} for c in comments],
        "reel_comments": [{"id": c.id, "reel_id": c.reel_id, "content": c.content, "created_at": iso(c.created_at)} for c in rcomments],
        "reactions": [{"target": "idea", "id": i, "reaction": r, "at": iso(t)} for i, r, t in reactions]
        + [{"target": "reel", "id": i, "reaction": r, "at": iso(t)} for i, r, t in rreactions],
        "conversations": [{"id": c.id, "peer": _who(peers.get(c.peer_of(u.id))), "started_by_user": c.initiator_id == u.id,
                           "status": c.status, "messages_stored": msg_counts.get(c.id, 0), "created_at": iso(c.created_at),
                           "last_message_at": iso(c.last_message_at)} for c in convs],
        "reports_against": [_report(r) for r in rep_against],
        "reports_by": [_report(r) for r in rep_by],
        "security_events": [{"type": e.type, "ip_ref": (e.ip_hash or "")[:12] or None, "detail": e.detail,
                             "at": iso(e.created_at)} for e in events],
    }


def _report(r: Report) -> dict:
    return {"id": r.id, "reason": r.reason, "status": r.status, "resolution": r.resolution, "created_at": iso(r.created_at),
            "reporter_id": r.reporter_id, "reported_user_id": r.reported_user_id}


def _post(p: Post) -> dict:
    return {"id": p.id, "content": p.content, "status": p.status, "created_at": iso(p.created_at),
            "real": {"likes": p.likes_count, "dislikes": p.dislikes_count},
            "boost": {"likes": p.boost_likes or 0, "dislikes": p.boost_dislikes or 0},
            "comments": p.comments_count}


def revoke_sessions(db: Session, user_id: str) -> int:
    u = db.get(User, user_id[:32])
    if u is None:
        raise not_found()
    n = db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.user_id == u.id)) or 0
    revoke_all_sessions(db, u.id)
    return n


def recount_posts(db: Session, post_ids) -> None:
    for pid in set(post_ids):
        likes = db.scalar(select(func.count()).select_from(PostReaction).where(PostReaction.post_id == pid, PostReaction.reaction_type == "like")) or 0
        dislikes = db.scalar(select(func.count()).select_from(PostReaction).where(PostReaction.post_id == pid, PostReaction.reaction_type == "dislike")) or 0
        comments = db.scalar(select(func.count()).select_from(Comment).where(Comment.post_id == pid)) or 0
        db.execute(update(Post).where(Post.id == pid).values(likes_count=likes, dislikes_count=dislikes, comments_count=comments))


def recount_reels(db: Session, reel_ids) -> None:
    for rid in set(reel_ids):
        likes = db.scalar(select(func.count()).select_from(ReelReaction).where(ReelReaction.reel_id == rid, ReelReaction.reaction == "like")) or 0
        dislikes = db.scalar(select(func.count()).select_from(ReelReaction).where(ReelReaction.reel_id == rid, ReelReaction.reaction == "dislike")) or 0
        comments = db.scalar(select(func.count()).select_from(ReelComment).where(ReelComment.reel_id == rid)) or 0
        db.execute(update(Reel).where(Reel.id == rid).values(likes_count=likes, dislikes_count=dislikes, comments_count=comments))


def delete_account(db: Session, user_id: str) -> None:
    """Delete a user and everything they own; counters on other people's content are recomputed."""
    u = db.get(User, user_id[:32])
    if u is None:
        raise not_found()
    if u.is_official:
        raise AppError(400, "protected", "لا يمكن حذف الحساب الرسمي.")
    posts_touched = set(db.execute(select(PostReaction.post_id).where(PostReaction.user_id == u.id)).scalars())
    posts_touched |= set(db.execute(select(Comment.post_id).where(Comment.author_id == u.id)).scalars())
    reels_touched = set(db.execute(select(ReelReaction.reel_id).where(ReelReaction.user_id == u.id)).scalars())
    reels_touched |= set(db.execute(select(ReelComment.reel_id).where(ReelComment.author_id == u.id)).scalars())
    db.execute(delete(PasswordReset).where(PasswordReset.user_id == u.id))
    db.execute(update(SecurityEvent).where(SecurityEvent.user_id == u.id).values(user_id=None))
    db.delete(u)  # FK cascades: sessions, posts (+their comments/reactions), comments, reactions, conversations…
    db.flush()
    own = set(db.execute(select(Post.id).where(Post.id.in_(posts_touched))).scalars())
    recount_posts(db, own)
    recount_reels(db, reels_touched)


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


def ideas(db: Session, q: str = "", status: str = "", page: int = 0, size: int = 30) -> dict:
    size = max(1, min(size, PAGE_MAX))
    stmt = select(Post)
    if q.strip():
        stmt = stmt.where(Post.content.ilike(_like(q.strip()), escape="\\"))
    if status in ("visible", "removed"):
        stmt = stmt.where(Post.status == status)
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.execute(stmt.order_by(Post.created_at.desc()).offset(page * size).limit(size)).scalars())
    authors = _users_by_id(db, [p.author_id for p in rows])
    return {"total": total, "ideas": [{**_post(p), "author": _who(authors.get(p.author_id))} for p in rows]}


def idea_detail(db: Session, post_id: str) -> dict:
    p = db.get(Post, post_id[:32])
    if p is None:
        raise not_found()
    comments = list(db.execute(select(Comment).where(Comment.post_id == p.id).order_by(Comment.created_at)).scalars())
    people = _users_by_id(db, [p.author_id] + [c.author_id for c in comments])
    return {"idea": {**_post(p), "author": _who(people.get(p.author_id))},
            "comments": [{"id": c.id, "content": c.content, "created_at": iso(c.created_at),
                          "author": _who(people.get(c.author_id))} for c in comments]}


def reel_detail(db: Session, reel_id: str) -> dict:
    r = db.get(Reel, reel_id[:32])
    if r is None:
        raise not_found()
    comments = list(db.execute(select(ReelComment).where(ReelComment.reel_id == r.id).order_by(ReelComment.created_at.desc())).scalars())
    people = _users_by_id(db, [c.author_id for c in comments])
    return {"reel": {"id": r.id, "short_id": r.short_id, "kind": r.kind, "caption": r.caption, "status": r.status,
                     "real": {"likes": r.likes_count, "dislikes": r.dislikes_count},
                     "boost": {"likes": r.boost_likes or 0, "dislikes": r.boost_dislikes or 0},
                     "views": r.views_count, "comments": r.comments_count, "created_at": iso(r.created_at)},
            "comments": [{"id": c.id, "content": c.content, "created_at": iso(c.created_at),
                          "author": _who(people.get(c.author_id))} for c in comments]}


def conversations(db: Session, q: str = "", user_id: str = "", page: int = 0, size: int = 30) -> dict:
    size = max(1, min(size, PAGE_MAX))
    stmt = select(Conversation)
    if user_id:
        stmt = stmt.where(or_(Conversation.initiator_id == user_id, Conversation.recipient_id == user_id))
    if q.strip():
        stmt = stmt.where(Conversation.id.in_(select(Message.conversation_id).where(Message.content.ilike(_like(q.strip()), escape="\\"))))
    total = db.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    rows = list(db.execute(stmt.order_by(Conversation.last_message_at.desc()).offset(page * size).limit(size)).scalars())
    people = _users_by_id(db, [c.initiator_id for c in rows] + [c.recipient_id for c in rows])
    counts = dict(db.execute(select(Message.conversation_id, func.count()).where(Message.conversation_id.in_([c.id for c in rows]))
                             .group_by(Message.conversation_id)).all()) if rows else {}
    return {"total": total, "conversations": [{
        "id": c.id, "initiator": _who(people.get(c.initiator_id)), "recipient": _who(people.get(c.recipient_id)),
        "status": c.status, "messages_stored": counts.get(c.id, 0), "created_at": iso(c.created_at),
        "last_message_at": iso(c.last_message_at), "expires_at": iso(c.expires_at)} for c in rows]}


def conversation_detail(db: Session, conversation_id: str) -> dict:
    c = db.get(Conversation, conversation_id[:32])
    if c is None:
        raise not_found()
    msgs = list(db.execute(select(Message).where(Message.conversation_id == c.id).order_by(Message.created_at)).scalars())
    people = _users_by_id(db, [c.initiator_id, c.recipient_id])
    flagged = set(db.execute(select(ContentFlag.message_id).where(ContentFlag.conversation_id == c.id,
                                                                  ContentFlag.message_id.is_not(None))).scalars())
    return {"conversation": {"id": c.id, "initiator": _who(people.get(c.initiator_id)), "recipient": _who(people.get(c.recipient_id)),
                             "status": c.status, "created_at": iso(c.created_at), "expires_at": iso(c.expires_at)},
            "messages": [{"id": m.id, "sender_id": m.sender_id, "content": m.content, "created_at": iso(m.created_at),
                          "read_at": iso(m.read_at), "expires_at": iso(m.expires_at), "flagged": m.id in flagged} for m in msgs]}


def search(db: Session, q: str, limit: int = 30) -> dict:
    q = (q or "").strip()
    if len(q) < 2:
        raise AppError(400, "query_too_short", "اكتب حرفين على الأقل.")
    pat, lim = _like(q), max(1, min(limit, PAGE_MAX))

    def rows(model, col, *extra):
        return list(db.execute(select(model).where(col.ilike(pat, escape="\\"), *extra)
                               .order_by(model.created_at.desc()).limit(lim)).scalars())

    return {
        "ideas": [{"id": p.id, "content": p.content, "author_id": p.author_id, "created_at": iso(p.created_at)}
                  for p in rows(Post, Post.content)],
        "idea_comments": [{"id": c.id, "post_id": c.post_id, "content": c.content, "author_id": c.author_id,
                           "created_at": iso(c.created_at)} for c in rows(Comment, Comment.content)],
        "reel_comments": [{"id": c.id, "reel_id": c.reel_id, "content": c.content, "author_id": c.author_id,
                           "created_at": iso(c.created_at)} for c in rows(ReelComment, ReelComment.content)],
        "reels": [{"id": r.id, "short_id": r.short_id, "caption": r.caption, "created_at": iso(r.created_at)}
                  for r in rows(Reel, Reel.caption)],
        "messages": [{"id": m.id, "conversation_id": m.conversation_id, "content": m.content, "sender_id": m.sender_id,
                      "created_at": iso(m.created_at)} for m in rows(Message, Message.content)],
    }


def delete_content(db: Session, kind: str, item_id: str) -> None:
    item_id = item_id[:32]
    if kind == "idea":
        p = db.get(Post, item_id)
        if p is None:
            raise not_found()
        db.delete(p)
    elif kind == "idea_comment":
        c = db.get(Comment, item_id)
        if c is None:
            raise not_found()
        db.delete(c)
        db.flush()
        recount_posts(db, [c.post_id])
    elif kind == "reel_comment":
        c = db.get(ReelComment, item_id)
        if c is None:
            raise not_found()
        db.delete(c)
        db.flush()
        recount_reels(db, [c.reel_id])
    elif kind == "message":
        m = db.get(Message, item_id)
        if m is None:
            raise not_found()
        db.delete(m)
    elif kind == "conversation":
        c = db.get(Conversation, item_id)
        if c is None:
            raise not_found()
        db.delete(c)
    else:
        raise AppError(400, "invalid_kind", "invalid kind")


# ---------------------------------------------------------------------------
# system
# ---------------------------------------------------------------------------


def failed_logins(db: Session, limit: int = 100) -> list[dict]:
    rows = db.execute(select(SecurityEvent).where(SecurityEvent.type.in_(
        ("login_failed", "login_blocked_ip", "login_blocked_ip_account", "account_locked", "admin_login_failed")))
        .order_by(SecurityEvent.created_at.desc()).limit(limit)).scalars()
    return [{"type": e.type, "user_id": e.user_id, "ip_ref": (e.ip_hash or "")[:12] or None, "at": iso(e.created_at)} for e in rows]


def reset_requests(db: Session, limit: int = 100) -> list[dict]:
    rows = list(db.execute(select(PasswordReset).order_by(PasswordReset.created_at.desc()).limit(limit)).scalars())
    people = _users_by_id(db, [r.user_id for r in rows])
    return [{"id": r.short_id, "user": _who(people.get(r.user_id)), "status": r.status, "attempts": r.attempts,
             "created_at": iso(r.created_at), "expires_at": iso(r.expires_at)} for r in rows]

