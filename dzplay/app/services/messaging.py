"""Conversations, messages, blocking and reporting.

Two kinds of conversation:
* anonymous (random matching, kind None): both sides appear as "dzplay" unless a side chose
  "كشف هويتي" — that reveals its own name + public ID to the other side only, for good;
* direct (V4, kind "direct"): started from someone's public ID; the peer's chosen name is shown.
  A new direct chat is a *message request* for the recipient (accept / ignore / block) and the
  sender may send at most DIRECT_MSG_BEFORE_REPLY_LIMIT messages until it is accepted or answered.

No internal user id, e-mail or IP ever leaves this module. Ownership is checked server-side on every call;
a conversation that is not yours is indistinguishable from one that does not
exist (404).

Functions collect side effects (realtime signals, push) in `Effects`; the API
layer fires them only after the database transaction committed.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, case, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app import clock
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, not_found, rate_limited
from app.models import Block, Conversation, Message, Report, User
from app.services import matching, names
from app.services.auth import log_event
from app.services.content import clean_message, preview
from app.services.moderation import flag_content
from app.services.rate_limit import Limit

PEER_NAME = "dzplay"
REPORT_REASONS = ("spam", "harassment", "threat", "inappropriate", "minor", "other")  # minor: V5 (content involving a minor)
HOUR, DAY = 3600, 86400


@dataclass
class Effects:
    signals: list[tuple[list[str], str]] = field(default_factory=list)  # (user ids, reason)
    push_to: list[str] = field(default_factory=list)
    events: list[tuple[list[str], dict]] = field(default_factory=list)  # realtime events with a payload
    tasks: list[tuple] = field(default_factory=list)  # V5: (fn, args) run in the background after the commit

    def later(self, fn, *args) -> None:
        self.tasks.append((fn, args))

    def signal(self, user_ids: list[str] | str, reason: str) -> None:
        self.signals.append(([user_ids] if isinstance(user_ids, str) else list(user_ids), reason))

    def event(self, user_ids: list[str] | str, payload: dict) -> None:
        self.events.append(([user_ids] if isinstance(user_ids, str) else list(user_ids), payload))


def iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="milliseconds") + "Z" if dt else None


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise AppError(400, "invalid_cursor", "طلب غير صالح.") from None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


# ---------------------------------------------------------------------------
# serialisation (the only place data leaves the domain)
# ---------------------------------------------------------------------------


def _message_status(m: Message) -> str:
    if m.read_at:
        return "read"
    if m.delivered_at:
        return "delivered"
    return "sent"


def serialize_message(m: Message, viewer_id: str) -> dict:
    mine = m.sender_id == viewer_id
    system = m.kind == "system"
    if m.kind == "image":  # V5: ephemeral picture (never a URL here: the recipient opens it explicitly)
        from sqlalchemy.orm import object_session

        from app.services.media_items import message_media

        db = object_session(m)
        return {"id": m.id, "conversation_id": m.conversation_id, "mine": mine, "author": "me" if mine else PEER_NAME,
                "content": "", "created_at": iso(m.created_at), "status": _message_status(m) if mine else None,
                "client_id": m.client_id if mine else None, "kind": "image", "meta": None,
                "media": message_media(db, m, viewer_id) if db is not None else {"kind": "image", "state": "expired"}}
    return {
        "id": m.id,
        "conversation_id": m.conversation_id,
        "mine": mine,
        "author": "me" if mine else PEER_NAME,
        "content": m.content,
        "created_at": iso(m.created_at),
        "status": _message_status(m) if mine and not system else None,
        "client_id": m.client_id if mine else None,
        "kind": "system" if system else "text",
        "meta": json.loads(m.meta) if system and m.meta else None,
    }


# "Active now" = a request seen within this window (sessions are touched every 5 minutes).
PRESENCE_WINDOW = timedelta(minutes=6)


def peer_card(c: Conversation, viewer_id: str, peer: User | None) -> dict:
    """How the other participant appears to the viewer (never internal ids or e-mail)."""
    peer_id = c.peer_of(viewer_id)
    shown = c.is_direct or c.revealed(peer_id)
    if peer is None or not shown:
        return {"name": names.default_name(), "public_id": None, "gender": None, "profile_ref": None, "anonymous": True,
                "active": False, "verified": False}
    active = peer.last_active_at is not None and clock.utcnow() - peer.last_active_at < PRESENCE_WINDOW
    # V5: the blue star is shown in direct chats only, never in anonymous ones (even after a reveal)
    return {"name": names.shown_name(peer), "public_id": peer.public_id, "gender": names.public_gender(peer),
            "profile_ref": None, "anonymous": False, "active": bool(active),
            "verified": bool(c.is_direct and peer.verified_at is not None)}


def _request_view(c: Conversation, viewer_id: str) -> dict | None:
    if not c.is_direct:
        return None
    state = c.request_state or "accepted"
    incoming = viewer_id == c.recipient_id
    if state == "ignored" and not incoming:
        state = "pending"  # the sender is never told that a request was ignored
    return {"state": state, "incoming": incoming, "pending_for_me": incoming and state == "pending"}


def serialize_conversation(c: Conversation, viewer_id: str, last: Message | None, unread: int,
                           peer: User | None = None) -> dict:
    peer_read = c.recipient_last_read_at if viewer_id == c.initiator_id else c.initiator_last_read_at
    peer_id = c.peer_of(viewer_id)
    card = peer_card(c, viewer_id, peer)
    return {
        "id": c.id,
        "kind": "direct" if c.is_direct else "anonymous",
        "peer": card["name"],
        "peer_card": card,
        "me_revealed": c.revealed(viewer_id),
        "muted": c.muted_for(viewer_id),
        "request": _request_view(c, viewer_id),
        "peer_has_replied": c.has_sent(peer_id),
        "status": c.status,
        "closed_by_me": c.status == "closed" and c.closed_by_id == viewer_id,
        "started_by_me": c.initiator_id == viewer_id,
        "created_at": iso(c.created_at),
        "last_message_at": iso(c.last_message_at),
        "expires_at": iso(c.expires_at),
        "unread": unread,
        "peer_read_at": iso(peer_read),
        "can_reply": c.status == "active",
        "last_message": (
            {"preview": "📷 صورة" if last.kind == "image" else preview(last.content), "mine": last.sender_id == viewer_id,
             "created_at": iso(last.created_at), "kind": last.kind if last.kind in ("system", "image") else "text"}
            if last
            else None
        ),
    }


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _require_can_send(user: User) -> None:
    if user.status == "suspended":
        raise AppError(403, "account_suspended", "حسابك موقوف مؤقتًا للمراجعة بسبب بلاغات. لا يمكنك إرسال رسائل حاليًا.")
    if user.status != "active":
        raise AppError(403, "account_banned", "تم إيقاف هذا الحساب.")
    if user.onboarding_required:
        raise AppError(403, "onboarding_required", "أكمل إنشاء حسابك أولًا (الجنس وتأكيد العمر).")


def _get_visible_conversation(db: Session, user: User, conversation_id: str) -> Conversation:
    if not isinstance(conversation_id, str) or len(conversation_id) > 32:
        raise not_found()
    conv = db.get(Conversation, conversation_id)
    if conv is None or not conv.is_participant(user.id) or conv.hidden_for(user.id) or conv.expires_at <= clock.utcnow():
        raise not_found()
    return conv


def _blocked_between(db: Session, a: str, b: str) -> bool:
    return bool(
        db.scalar(
            select(Block.id).where(
                or_(and_(Block.blocker_id == a, Block.blocked_id == b), and_(Block.blocker_id == b, Block.blocked_id == a))
            ).limit(1)
        )
    )


def _check_limits(limiter, limits: list[Limit]) -> None:
    decision = limiter.check_and_hit(limits)
    if decision.allowed:
        return
    messages = {
        "msg_min": "أنت ترسل بسرعة كبيرة. انتظر قليلًا ثم حاول مجددًا.",
        "msg_hour": "وصلت إلى الحد الأقصى للرسائل في الساعة. حاول لاحقًا.",
        "newconv_hour": "وصلت إلى الحد الأقصى للمحادثات الجديدة في الساعة. حاول لاحقًا.",
        "newconv_day": "وصلت إلى الحد الأقصى للمحادثات الجديدة اليوم. عُد غدًا.",
        "dup": "أرسلت هذا النص نفسه مؤخرًا. اكتب شيئًا مختلفًا.",
        "report": "أرسلت بلاغات كثيرة. حاول لاحقًا.",
        "post_hour": "نشرت عدة أفكار مؤخرًا. خذ استراحة قصيرة ثم شارك من جديد.",
        "post_day": "وصلت إلى الحد الأقصى للمنشورات اليوم. عُد غدًا.",
        "comment_min": "أنت تعلّق بسرعة كبيرة. انتظر قليلًا.",
        "comment_hour": "وصلت إلى الحد الأقصى للتعليقات في الساعة.",
        "react": "تفاعلات كثيرة بسرعة. انتظر قليلًا.",
        "upload_hour": "رفعت ملفات كثيرة خلال ساعة. حاول لاحقًا.",
        "support_day": "فتحت تذاكر كثيرة اليوم. أضف ردك إلى تذكرة مفتوحة أو عُد غدًا.",
        "support_hour": "أرسلت رسائل كثيرة إلى الدعم. حاول بعد قليل.",
        "verify": "محاولات كثيرة. حاول لاحقًا.",
    }
    kind = (decision.key or "").split(":", 1)[0]
    raise rate_limited(decision.retry_after, messages.get(kind, "محاولات كثيرة. حاول لاحقًا."))


def _message_limits(settings: Settings, user_id: str) -> list[Limit]:
    return [
        Limit(f"msg_min:{user_id}", settings.MAX_MESSAGES_PER_MINUTE, 60),
        Limit(f"msg_hour:{user_id}", settings.MAX_MESSAGES_PER_HOUR, HOUR),
    ]


def _clean_client_id(client_id: object) -> str | None:
    if client_id is None:
        return None
    if not isinstance(client_id, str) or not (8 <= len(client_id) <= 64) or not client_id.replace("-", "").replace("_", "").isalnum():
        raise AppError(400, "invalid_client_id", "طلب غير صالح.")
    return client_id


def _existing_by_client_id(db: Session, user: User, client_id: str | None) -> Message | None:
    if not client_id:
        return None
    return db.scalar(select(Message).where(Message.sender_id == user.id, Message.client_id == client_id))


def _summaries(db: Session, user: User, conversations: list[Conversation]) -> list[dict]:
    if not conversations:
        return []
    ids = [c.id for c in conversations]
    ranked = (
        select(Message.id, func.row_number().over(partition_by=Message.conversation_id,
                                                   order_by=(Message.created_at.desc(), Message.id.desc())).label("rn"))
        .where(Message.conversation_id.in_(ids))
        .subquery()
    )
    last_by_conv = {m.conversation_id: m for m in db.execute(
        select(Message).join(ranked, ranked.c.id == Message.id).where(ranked.c.rn == 1)
    ).scalars()}
    unread = dict(db.execute(
        select(Message.conversation_id, func.count())
        .where(Message.conversation_id.in_(ids), Message.recipient_id == user.id, Message.read_at.is_(None))
        .group_by(Message.conversation_id)
    ).all())
    peer_ids = {c.peer_of(user.id) for c in conversations}
    peers = {u.id: u for u in db.execute(select(User).where(User.id.in_(peer_ids))).scalars()} if peer_ids else {}
    return [serialize_conversation(c, user.id, last_by_conv.get(c.id), unread.get(c.id, 0), peers.get(c.peer_of(user.id)))
            for c in conversations]


def _visible_conversations(db: Session, user: User, limit: int = 200) -> list[Conversation]:
    return list(db.execute(
        select(Conversation)
        .where(
            Conversation.expires_at > clock.utcnow(),
            or_(
                and_(Conversation.initiator_id == user.id, Conversation.initiator_hidden.is_(False)),
                and_(Conversation.recipient_id == user.id, Conversation.recipient_hidden.is_(False)),
            ),
        )
        .order_by(Conversation.last_message_at.desc())
        .limit(limit)
    ).scalars())


def _mark_delivered(db: Session, user: User, effects: Effects, conversation_ids: list[str] | None = None) -> None:
    cond = [Message.recipient_id == user.id, Message.delivered_at.is_(None)]
    if conversation_ids is not None:
        if not conversation_ids:
            return
        cond.append(Message.conversation_id.in_(conversation_ids))
    senders = set(db.execute(select(Message.sender_id).where(*cond).distinct()).scalars())
    if not senders:
        return
    db.execute(update(Message).where(*cond).values(delivered_at=clock.utcnow()))
    effects.signal(list(senders), "status")


# ---------------------------------------------------------------------------
# sending
# ---------------------------------------------------------------------------


def send_anonymous(db: Session, settings: Settings, limiter, user: User, *, content: object,
                   client_id: object, effects: Effects) -> dict:
    """Start a new conversation with a random person."""
    _require_can_send(user)
    text = clean_message(content, settings.MAX_MESSAGE_LENGTH, settings.LINK_POLICY)
    cid = _clean_client_id(client_id)
    existing = _existing_by_client_id(db, user, cid)
    if existing is not None:  # retried request (offline outbox): do not send twice
        conv = db.get(Conversation, existing.conversation_id)
        return {"conversation": _summaries(db, user, [conv])[0], "message": serialize_message(existing, user.id)}

    recipient_id = matching.pick_recipient(db, settings, user.id)
    if recipient_id is None:
        raise AppError(409, "no_recipient", "لا يوجد أشخاص متاحون الآن لاستقبال رسالتك. حاول بعد قليل.")

    digest = hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:32]
    _check_limits(limiter, _message_limits(settings, user.id) + [
        Limit(f"newconv_hour:{user.id}", settings.MAX_NEW_CONVERSATIONS_PER_HOUR, HOUR),
        Limit(f"newconv_day:{user.id}", settings.MAX_NEW_CONVERSATIONS_PER_DAY, DAY),
        Limit(f"dup:{user.id}:{digest}", 1, settings.DUPLICATE_MESSAGE_WINDOW),
    ])

    now = clock.utcnow()
    conv = Conversation(
        initiator_id=user.id, recipient_id=recipient_id, created_at=now, last_message_at=now, updated_at=now,
        expires_at=now + timedelta(seconds=settings.CONVERSATION_IDLE_TTL), last_sender_id=user.id, consecutive_count=1,
        initiator_sent=True,
    )
    db.add(conv)
    db.flush()
    msg = Message(conversation_id=conv.id, sender_id=user.id, recipient_id=recipient_id, content=text,
                  client_id=cid, created_at=now, expires_at=now + timedelta(seconds=settings.MESSAGE_TTL))
    db.add(msg)
    db.flush()
    flag_content(db, settings, target="message", text=text, offender_id=user.id, victim_id=recipient_id,
                 message_id=msg.id, conversation_id=conv.id)

    user.messages_sent += 1
    user.conversations_count += 1
    db.execute(update(User).where(User.id == recipient_id).values(
        messages_received=User.messages_received + 1, conversations_count=User.conversations_count + 1))
    db.flush()

    effects.signal(recipient_id, "message")
    effects.push_to.append(recipient_id)
    effects.signal(user.id, "sent")
    return {"conversation": _summaries(db, user, [conv])[0], "message": serialize_message(msg, user.id)}


def reply(db: Session, settings: Settings, limiter, user: User, conversation_id: str, *, content: object,
          client_id: object, effects: Effects) -> dict:
    _require_can_send(user)
    conv = _get_visible_conversation(db, user, conversation_id)
    text = clean_message(content, settings.MAX_MESSAGE_LENGTH, settings.LINK_POLICY)
    cid = _clean_client_id(client_id)
    existing = _existing_by_client_id(db, user, cid)
    if existing is not None:
        return {"message": serialize_message(existing, user.id)}

    peer_id = conv.peer_of(user.id)
    if conv.status != "active" or _blocked_between(db, user.id, peer_id):
        raise AppError(403, "conversation_closed", "هذه المحادثة لم تعد متاحة.")
    _check_direct_request(settings, conv, user.id)
    if conv.last_sender_id == user.id and conv.consecutive_count >= settings.MAX_CONSECUTIVE_MESSAGES:
        raise AppError(429, "wait_for_reply", "أرسلت عدة رسائل متتالية. انتظر رد الطرف الآخر.", retry_after=60)
    _check_limits(limiter, _message_limits(settings, user.id))

    now = clock.utcnow()
    msg = Message(conversation_id=conv.id, sender_id=user.id, recipient_id=peer_id, content=text, client_id=cid,
                  created_at=now, expires_at=now + timedelta(seconds=settings.MESSAGE_TTL))
    db.add(msg)
    db.flush()
    flag_content(db, settings, target="message", text=text, offender_id=user.id, victim_id=peer_id,
                 message_id=msg.id, conversation_id=conv.id)
    _after_send(db, settings, conv, user, peer_id, now, effects)
    return {"message": serialize_message(msg, user.id)}


def _check_direct_request(settings: Settings, conv: Conversation, sender_id: str) -> None:
    """Until the recipient accepts or answers a direct request, the sender may only send a few messages."""
    if not conv.is_direct or (conv.request_state or "accepted") == "accepted" or sender_id != conv.initiator_id:
        return
    if conv.last_sender_id == sender_id and conv.consecutive_count >= settings.DIRECT_MSG_BEFORE_REPLY_LIMIT:
        raise AppError(429, "request_pending", "أرسلت الحد الأقصى من الرسائل. انتظر حتى يقبل الطرف الآخر طلب المراسلة.",
                       retry_after=3600)


def _after_send(db: Session, settings: Settings, conv: Conversation, user: User, peer_id: str, now: datetime,
                effects: Effects) -> None:
    conv.consecutive_count = conv.consecutive_count + 1 if conv.last_sender_id == user.id else 1
    conv.last_sender_id = user.id
    conv.mark_sent(user.id)
    conv.last_message_at = now
    conv.updated_at = now
    conv.expires_at = now + timedelta(seconds=settings.CONVERSATION_IDLE_TTL)
    if conv.is_direct and user.id == conv.recipient_id and conv.request_state != "accepted":
        conv.request_state = "accepted"  # answering a message request accepts it
    ignored = conv.is_direct and conv.request_state == "ignored" and peer_id == conv.recipient_id
    if not ignored:
        conv.set_hidden(peer_id, False)  # a new message brings a deleted conversation back for the peer

    user.messages_sent += 1
    db.execute(update(User).where(User.id == peer_id).values(messages_received=User.messages_received + 1))
    db.flush()

    if not ignored:
        effects.signal(peer_id, "message")
        pending_request = conv.is_direct and conv.request_state == "pending" and peer_id == conv.recipient_id
        if not pending_request and not conv.muted_for(peer_id):
            effects.push_to.append(peer_id)
    effects.signal(user.id, "sent")


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


def list_conversations(db: Session, user: User, effects: Effects) -> list[dict]:
    convs = _visible_conversations(db, user)
    _mark_delivered(db, user, effects, [c.id for c in convs])
    return _summaries(db, user, convs)


def get_conversation(db: Session, user: User, conversation_id: str, *, before: str | None, limit: int,
                     effects: Effects) -> dict:
    conv = _get_visible_conversation(db, user, conversation_id)
    limit = max(1, min(limit, 100))
    q = select(Message).where(Message.conversation_id == conv.id, Message.expires_at > clock.utcnow())
    before_dt = parse_iso(before)
    if before_dt:
        q = q.where(Message.created_at < before_dt)
    rows = list(db.execute(q.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit + 1)).scalars())
    has_more = len(rows) > limit
    rows = list(reversed(rows[:limit]))
    _mark_delivered(db, user, effects, [conv.id])
    return {
        "conversation": _summaries(db, user, [conv])[0],
        "messages": [serialize_message(m, user.id) for m in rows],
        "has_more": has_more,
    }


def sync(db: Session, user: User, *, since: str | None, effects: Effects) -> dict:
    """Everything a (re)connecting client needs: all visible conversations plus
    messages created after `since` (clients send their last server_time minus a
    small overlap and de-duplicate by id)."""
    server_time = clock.utcnow()
    convs = _visible_conversations(db, user)
    ids = [c.id for c in convs]
    messages: list[Message] = []
    since_dt = parse_iso(since)
    if since_dt and ids:
        messages = list(db.execute(
            select(Message)
            .where(Message.conversation_id.in_(ids), Message.created_at > since_dt, Message.expires_at > server_time)
            .order_by(Message.created_at, Message.id)
            .limit(1000)
        ).scalars())
    _mark_delivered(db, user, effects, ids)
    # Status of my own messages may have changed (delivered/read) — send current statuses.
    return {
        "server_time": iso(server_time),
        "conversations": _summaries(db, user, convs),
        "messages": [serialize_message(m, user.id) for m in messages],
    }


def mark_read(db: Session, settings: Settings, user: User, conversation_id: str, effects: Effects) -> dict:
    conv = _get_visible_conversation(db, user, conversation_id)
    now = clock.utcnow()
    cutoff = now + timedelta(seconds=settings.MESSAGE_TTL_AFTER_READ)
    cond = [Message.conversation_id == conv.id, Message.recipient_id == user.id, Message.read_at.is_(None)]
    count = db.scalar(select(func.count()).select_from(Message).where(*cond)) or 0
    if count:
        # Once read, a message only needs to live long enough for the sender's devices to sync.
        db.execute(update(Message).where(*cond).values(
            read_at=now,
            delivered_at=func.coalesce(Message.delivered_at, now),
            expires_at=case((Message.expires_at > cutoff, cutoff), else_=Message.expires_at),
        ))
        if user.id == conv.initiator_id:
            conv.initiator_last_read_at = now
        else:
            conv.recipient_last_read_at = now
        conv.updated_at = now
        effects.signal(conv.peer_of(user.id), "status")
    return {"read": count}


def hide_conversation(db: Session, user: User, conversation_id: str, effects: Effects) -> None:
    conv = _get_visible_conversation(db, user, conversation_id)
    conv.set_hidden(user.id, True)
    if conv.initiator_hidden and conv.recipient_hidden:
        db.delete(conv)
    effects.signal(user.id, "conversation")


# ---------------------------------------------------------------------------
# blocking
# ---------------------------------------------------------------------------


def block_conversation(db: Session, user: User, conversation_id: str, effects: Effects,
                       settings: Settings | None = None) -> None:
    conv = _get_visible_conversation(db, user, conversation_id)
    peer_id = conv.peer_of(user.id)
    if not db.scalar(select(Block.id).where(Block.blocker_id == user.id, Block.blocked_id == peer_id)):
        db.add(Block(blocker_id=user.id, blocked_id=peer_id, created_at=clock.utcnow()))
    now = clock.utcnow()
    # Close every live conversation between the two people.
    for c in db.execute(select(Conversation).where(
        Conversation.status == "active",
        or_(and_(Conversation.initiator_id == user.id, Conversation.recipient_id == peer_id),
            and_(Conversation.initiator_id == peer_id, Conversation.recipient_id == user.id)),
    )).scalars():
        c.status = "closed"
        c.closed_by_id = user.id
        c.updated_at = now
        c.set_hidden(user.id, True)
    conv.set_hidden(user.id, True)
    log_event(db, "block", None, user.id)
    effects.signal([user.id, peer_id], "conversation")


def list_blocks(db: Session, user: User) -> list[dict]:
    rows = db.execute(select(Block).where(Block.blocker_id == user.id).order_by(Block.created_at.desc())).scalars()
    return [{"id": b.id, "peer": PEER_NAME, "created_at": iso(b.created_at)} for b in rows]


def unblock(db: Session, user: User, block_id: str) -> None:
    result = db.execute(delete(Block).where(Block.id == block_id, Block.blocker_id == user.id))
    if not result.rowcount:
        raise not_found()


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------


def _clean_details(details: object) -> str | None:
    if details is None or details == "":
        return None
    if not isinstance(details, str):
        raise AppError(400, "invalid_details", "تفاصيل البلاغ غير صالحة.")
    return clean_message(details, 500, "allow_plain")


def report(db: Session, settings: Settings, limiter, user: User, *, conversation_id: str | None,
           message_id: str | None, reason: object, details: object, effects: Effects | None = None) -> dict:
    if reason not in REPORT_REASONS:
        raise AppError(400, "invalid_reason", "اختر سبب البلاغ.")
    details_c = _clean_details(details)

    if message_id is not None:
        msg = db.get(Message, message_id) if isinstance(message_id, str) and len(message_id) <= 32 else None
        if msg is None:
            raise not_found()
        conv = _get_visible_conversation(db, user, msg.conversation_id)
        if msg.sender_id == user.id:
            raise AppError(400, "cannot_report_own", "لا يمكنك الإبلاغ عن رسالتك.")
        evidence = [msg]
    else:
        conv = _get_visible_conversation(db, user, conversation_id or "")
        evidence = list(reversed(db.execute(
            select(Message).where(Message.conversation_id == conv.id, Message.sender_id != user.id)
            .order_by(Message.created_at.desc()).limit(10)
        ).scalars().all()))
    reported_id = conv.peer_of(user.id)

    dup_q = select(Report.id).where(Report.reporter_id == user.id, Report.conversation_id == conv.id, Report.status == "open")
    dup_q = dup_q.where(Report.message_id == message_id) if message_id is not None else dup_q.where(Report.message_id.is_(None))
    dup = db.scalar(dup_q)
    if dup:
        return {"id": dup, "duplicate": True}

    _check_limits(limiter, [Limit(f"report:{user.id}", settings.MAX_REPORTS_PER_HOUR, HOUR)])
    now = clock.utcnow()
    rep = Report(
        reporter_id=user.id, reported_user_id=reported_id, conversation_id=conv.id, message_id=message_id,
        media_id=evidence[0].media_id if message_id is not None and evidence[0].kind == "image" else None,
        reason=reason, details=details_c,
        snapshot=json.dumps([{"content": "📷 صورة" if m.kind == "image" else m.content, "created_at": iso(m.created_at)}
                             for m in evidence], ensure_ascii=False),
        created_at=now, expires_at=now + timedelta(seconds=settings.REPORT_RETENTION),
    )
    db.add(rep)
    db.flush()
    if rep.media_id:  # V5: a reported chat picture is kept as evidence and sent to the moderators
        from app.models import MediaItem
        from app.services.media_moderation import on_report

        item = db.get(MediaItem, rep.media_id)
        if item is not None and (item.tg_message_id or item.tg_file_id):
            on_report(db, settings, item, str(reason), effects if effects is not None else Effects())
    log_event(db, "report", None, user.id, reason)
    _maybe_auto_suspend(db, settings, reported_id)
    return {"id": rep.id, "duplicate": False}


def _maybe_auto_suspend(db: Session, settings: Settings, user_id: str) -> None:
    threshold = settings.REPORT_AUTO_SUSPEND_THRESHOLD
    if threshold <= 0:
        return
    since = clock.utcnow() - timedelta(days=1)
    reporters = db.scalar(
        select(func.count(func.distinct(Report.reporter_id))).where(Report.reported_user_id == user_id, Report.created_at > since)
    ) or 0
    if reporters >= threshold:
        user = db.get(User, user_id)
        if user and user.status == "active":
            user.status = "suspended"
            log_event(db, "auto_suspended", None, user_id, f"{reporters} reporters/24h")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------


def profile(user: User, settings: Settings | None = None) -> dict:
    nxt = None
    if user.name_changed_at is not None and settings is not None:
        nxt = user.name_changed_at + timedelta(days=settings.NAME_CHANGE_COOLDOWN_DAYS)
    return {
        "display_name": names.shown_name(user),
        "has_custom_name": bool(user.display_name),
        "public_id": user.public_id,
        "gender": user.gender or "unspecified",
        "next_name_change_at": iso(nxt) if nxt and nxt > clock.utcnow() else None,
        "needs_gender": user.gender_asked_at is None,
        "needs_onboarding": bool(user.onboarding_required),
        "age_confirmed": user.age_confirmed_at is not None,
        "verified": user.verified_at is not None,
        "privacy": privacy_view(user),
        "status": user.status,
        "stats": {
            "messages_sent": user.messages_sent,
            "messages_received": user.messages_received,
            "conversations": user.conversations_count,
        },
        "sign_in_method": "google" if user.google_sub and not user.password_hash else "password",
        # True once for accounts that have not seen the current privacy notice yet.
        "privacy_notice": (user.privacy_ack_version or 0) < PRIVACY_VERSION,
    }


def privacy_view(user: User) -> dict:
    return {
        "accept_anonymous": user.accept_anonymous is not False,
        "accept_direct": user.accept_direct or "everyone",
        "searchable_by_name": user.searchable_by_name is not False,
    }


def set_privacy(user: User, body: dict) -> dict:
    """Server-side privacy switches (the UI is never trusted)."""
    for key in ("accept_anonymous", "searchable_by_name"):
        if key in body and body[key] is not None:
            if not isinstance(body[key], bool):
                raise AppError(400, "invalid_input", "طلب غير صالح.")
            setattr(user, key, body[key])
    if body.get("accept_direct") is not None:
        if body["accept_direct"] not in ("everyone", "nobody"):
            raise AppError(400, "invalid_input", "طلب غير صالح.")
        user.accept_direct = body["accept_direct"]
    return privacy_view(user)


# ---------------------------------------------------------------------------
# direct messages (V4)
# ---------------------------------------------------------------------------


def _direct_key(a: str, b: str) -> str:
    return ":".join(sorted((a, b)))


def find_person(db: Session, public_id: object) -> User | None:
    from app.models import PUBLIC_ID_RE

    import re

    pid = public_id.strip().upper() if isinstance(public_id, str) else ""
    if not re.match(PUBLIC_ID_RE, pid):
        return None
    u = db.scalar(select(User).where(User.public_id == pid))
    if u is None or u.status != "active" or u.is_system or u.is_official:
        return None
    return u


def require_adult(user: User) -> None:
    if user.age_confirmed_at is None:
        raise AppError(403, "age_required", "أكّد أن عمرك 18 سنة أو أكثر من الإعدادات أولًا.")


def direct_target(db: Session, user: User, public_id: object) -> User:
    target = find_person(db, public_id)
    if target is None or target.id == user.id or _blocked_between(db, user.id, target.id):
        raise not_found()  # unknown, blocked either way, suspended: indistinguishable
    return target


def existing_direct(db: Session, a: str, b: str) -> Conversation | None:
    conv = db.scalar(select(Conversation).where(Conversation.direct_key == _direct_key(a, b)))
    if conv is not None and conv.expires_at <= clock.utcnow():
        db.delete(conv)  # expired (TTL) but not cleaned yet: start a fresh one
        db.flush()
        return None
    return conv


def existing_direct_by_public_id(db: Session, viewer: User, public_id: object) -> Conversation | None:
    target = find_person(db, public_id)
    return existing_direct(db, viewer.id, target.id) if target is not None and target.id != viewer.id else None


def person_card(db: Session, viewer: User, public_id: object) -> dict:
    target = direct_target(db, viewer, public_id)
    from app.services import ideas

    conv = existing_direct(db, viewer.id, target.id)
    if conv is not None and conv.status == "active":
        can_message = True  # an existing chat keeps working even if they later close direct messages
    else:
        can_message = conv is None and (target.accept_direct or "everyone") == "everyone"
    visible = conv is not None and not conv.hidden_for(viewer.id)
    return {"name": names.shown_name(target), "public_id": target.public_id, "gender": names.public_gender(target),
            "profile_ref": ideas.profile_ref_for(db, target.id), "can_message": can_message,
            "conversation_id": conv.id if visible else None, "verified": target.verified_at is not None}


def send_direct(db: Session, settings: Settings, limiter, user: User, public_id: object, *, content: object,
                client_id: object, effects: Effects) -> dict:
    """First message to someone found by public ID: reuses the pair's direct chat or starts a request."""
    _require_can_send(user)
    require_adult(user)
    target = direct_target(db, user, public_id)
    text = clean_message(content, settings.MAX_MESSAGE_LENGTH, settings.LINK_POLICY)
    cid = _clean_client_id(client_id)
    existing_msg = _existing_by_client_id(db, user, cid)
    if existing_msg is not None:
        conv = db.get(Conversation, existing_msg.conversation_id)
        return {"conversation": _summaries(db, user, [conv])[0], "message": serialize_message(existing_msg, user.id)}
    conv = existing_direct(db, user.id, target.id)
    if conv is not None:
        if conv.status != "active":
            raise AppError(403, "conversation_closed", "هذه المحادثة لم تعد متاحة.")
        conv.set_hidden(user.id, False)
        result = reply(db, settings, limiter, user, conv.id, content=text, client_id=cid, effects=effects)
        return {"conversation": _summaries(db, user, [conv])[0], **result}
    if (target.accept_direct or "everyone") != "everyone":
        raise AppError(403, "direct_closed", "هذا الشخص لا يستقبل رسائل مباشرة.")
    _check_limits(limiter, _message_limits(settings, user.id) + [
        Limit(f"newconv_day:direct:{user.id}", settings.DIRECT_NEW_PER_DAY, DAY)])
    now = clock.utcnow()
    conv = Conversation(
        initiator_id=user.id, recipient_id=target.id, created_at=now, last_message_at=now, updated_at=now,
        expires_at=now + timedelta(seconds=settings.CONVERSATION_IDLE_TTL), last_sender_id=None, consecutive_count=0,
        kind="direct", direct_key=_direct_key(user.id, target.id), request_state="pending",
    )
    db.add(conv)
    db.flush()
    msg = Message(conversation_id=conv.id, sender_id=user.id, recipient_id=target.id, content=text, client_id=cid,
                  created_at=now, expires_at=now + timedelta(seconds=settings.MESSAGE_TTL))
    db.add(msg)
    db.flush()
    flag_content(db, settings, target="message", text=text, offender_id=user.id, victim_id=target.id,
                 message_id=msg.id, conversation_id=conv.id)
    user.conversations_count += 1
    db.execute(update(User).where(User.id == target.id).values(conversations_count=User.conversations_count + 1))
    _after_send(db, settings, conv, user, target.id, now, effects)
    return {"conversation": _summaries(db, user, [conv])[0], "message": serialize_message(msg, user.id)}


def answer_request(db: Session, user: User, conversation_id: str, action: str, effects: Effects) -> dict:
    """The recipient of a direct message request accepts or ignores it (block uses block_conversation)."""
    conv = _get_visible_conversation(db, user, conversation_id)
    if not conv.is_direct or user.id != conv.recipient_id:
        raise not_found()
    if action == "accept":
        conv.request_state = "accepted"
    elif action == "ignore":
        conv.request_state = "ignored"
        conv.set_hidden(user.id, True)
    else:
        raise AppError(400, "invalid_action", "طلب غير صالح.")
    conv.updated_at = clock.utcnow()
    effects.signal(user.id, "conversation")
    return {"state": conv.request_state}


def _system_message(db: Session, settings: Settings, conv: Conversation, actor_id: str, event: str, text: str,
                    meta: dict | None = None) -> Message:
    now = clock.utcnow()
    msg = Message(conversation_id=conv.id, sender_id=actor_id, recipient_id=conv.peer_of(actor_id), content=text,
                  created_at=now, expires_at=now + timedelta(seconds=settings.MESSAGE_TTL), kind="system",
                  meta=json.dumps({"event": event, **(meta or {})}, ensure_ascii=False))
    db.add(msg)
    conv.last_message_at = now
    conv.updated_at = now
    db.flush()
    return msg


def reveal(db: Session, settings: Settings, user: User, conversation_id: str, effects: Effects) -> dict:
    """Anonymous chat: show MY name and public ID to the other side (one-way, cannot be undone)."""
    conv = _get_visible_conversation(db, user, conversation_id)
    if conv.is_direct:
        raise AppError(400, "not_anonymous", "هذه محادثة مباشرة.")
    if conv.status != "active":
        raise AppError(403, "conversation_closed", "هذه المحادثة لم تعد متاحة.")
    if not conv.revealed(user.id):
        if user.id == conv.initiator_id:
            conv.initiator_revealed = True
        else:
            conv.recipient_revealed = True
        _system_message(db, settings, conv, user.id, "reveal", "كُشفت الهوية",
                        {"name": names.shown_name(user), "public_id": user.public_id})
        effects.signal([user.id, conv.peer_of(user.id)], "message")
    return {"me_revealed": True}


def set_muted(db: Session, user: User, conversation_id: str, muted: bool) -> dict:
    conv = _get_visible_conversation(db, user, conversation_id)
    if user.id == conv.initiator_id:
        conv.initiator_muted = bool(muted)
    else:
        conv.recipient_muted = bool(muted)
    return {"muted": bool(muted)}
