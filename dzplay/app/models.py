"""Database schema.

Two logically separate areas:

* Identity & safety (long-lived): users, sessions, blocks, reports, auth
  throttling, security events, push subscriptions.
* Temporary messaging data (TTL-bound): conversations and messages. Every row
  carries an `expires_at` and is purged by the cleanup job. Nothing in the
  identity area references these tables with a foreign key, so they can be
  moved to a separate store later without touching account data.

Identifiers exposed to clients (conversation / message / block ids) are random
128-bit tokens. Internal user ids are never sent to clients.
"""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app import clock
from app.db import Base


def new_internal_id() -> str:
    return uuid.uuid4().hex


def new_public_id() -> str:
    return secrets.token_urlsafe(16)


# ---------------------------------------------------------------------------
# Identity & safety
# ---------------------------------------------------------------------------


PUBLIC_ID_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"  # no 0/O, 1/I lookalikes
PUBLIC_ID_RE = r"^DZ-[23456789ABCDEFGHJKLMNPQRSTUVWXYZ]{6}$"


def new_public_user_id() -> str:
    """DZ-XXXXXX: random (not sequential, ~1e9 values), shown to people; never the internal id."""
    return "DZ-" + "".join(secrets.choice(PUBLIC_ID_ALPHABET) for _ in range(6))


class User(Base):
    __tablename__ = "users"
    __table_args__ = (Index("ux_users_public_id", "public_id", unique=True),
                      Index("ux_users_referral_code", "referral_code", unique=True))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    google_sub: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|suspended|banned
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_active_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    registration_ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Counters survive message TTL deletion (profile statistics).
    messages_sent: Mapped[int] = mapped_column(Integer, default=0)
    messages_received: Mapped[int] = mapped_column(Integer, default=0)
    conversations_count: Mapped[int] = mapped_column(Integer, default=0)
    # Last privacy notice version the user has seen (None = before notices existed).
    privacy_ack_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # The platform's own "DZPLAY الرسمي" account (no login; used by admins to comment).
    is_official: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Internal accounts the team posts comments from (shown as "dzplay"); cannot sign in.
    is_system: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- V4: identity (all nullable: added in place to existing databases) ---
    # Shown name; None = the default "dzplay". Not unique: people are told apart by public_id.
    display_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    name_norm: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)  # search key
    name_changed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Short random public ID (e.g. DZ-7K4M9Q): fixed for life, unrelated to the internal id.
    public_id: Mapped[str | None] = mapped_column(String(12), nullable=True, default=new_public_user_id)
    gender: Mapped[str | None] = mapped_column(String(12), nullable=True)  # male|female|unspecified (None = unspecified)
    gender_asked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    age_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # "18+ and terms"
    # Privacy switches (None = default)
    accept_anonymous: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # default on
    accept_direct: Mapped[str | None] = mapped_column(String(12), nullable=True)  # everyone|nobody (default everyone)
    accept_calls: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # unused since V6 (calls removed)
    searchable_by_name: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # default on
    # New Google accounts: gender + 18+ must be completed before messaging (old accounts stay NULL).
    onboarding_required: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- V5: blue star ("official, trusted account" — NOT identity verification). Written ONLY by admin
    # services (app.services.verification.grant / revoke): no user endpoint accepts these fields.
    verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    verified_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    # --- V6 phase 3: profile picture (a MediaItem with purpose "avatar", attached_type "user")
    avatar_media_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # --- V6 phase 4/5: membership (features only; set by app.services.membership when a payment is accepted)
    member_since: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    member_ended_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # --- V6 phase 5b: invitations (own code, created on first use) and a coarse device hash (abuse flags only)
    referral_code: Mapped[str | None] = mapped_column(String(12), nullable=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # V6 5c: confirmed by a code once
    device_hash: Mapped[str | None] = mapped_column(String(32), nullable=True)
    appearance: Mapped[str | None] = mapped_column(Text, nullable=True)  # V6 phase 8: JSON {mode, accent, font}


class NameHistory(Base):
    """Previous display names (shown in the admin panel only)."""

    __tablename__ = "name_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    old_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    new_name: Mapped[str | None] = mapped_column(String(40), nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class AuthSession(Base):
    __tablename__ = "sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class Block(Base):
    __tablename__ = "blocks"
    __table_args__ = (UniqueConstraint("blocker_id", "blocked_id", name="uq_block_pair"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    blocker_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    blocked_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class FcmToken(Base):
    """Firebase token of an installed Android app (rings it when closed). One row per device."""

    __tablename__ = "fcm_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token: Mapped[str] = mapped_column(String(512), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Report(Base):
    __tablename__ = "reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    reporter_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reported_user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Plain references (no FK): the conversation/message may expire before review.
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    reel_comment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # unused since V6
    call_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # unused since V6
    media_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # V5: reported picture / video
    reason: Mapped[str] = mapped_column(String(32))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Minimal evidence: only the reported content, copied so it survives TTL.
    snapshot: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|resolved|dismissed
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class ContentFlag(Base):
    """Private content flagged automatically for review (user protection).

    Holds a copy of the flagged text so it survives the message TTL, like a
    report snapshot. Plain references (no FK): the source may expire first.
    """

    __tablename__ = "content_flags"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    target: Mapped[str] = mapped_column(String(16))  # message|comment
    message_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    offender_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    victim_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    categories: Mapped[str] = mapped_column(String(128))
    terms: Mapped[str] = mapped_column(Text, default="[]")
    snapshot: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)  # open|resolved|dismissed
    resolution: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AuthThrottle(Base):
    """Failure counters / blocks for login protection.

    key examples: "ip:<iphash>", "ipacct:<iphash>:<accthash>", "acct:<accthash>".
    """

    __tablename__ = "auth_throttle"

    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    first_failure_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_failure_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class SecurityEvent(Base):
    __tablename__ = "security_events"
    __table_args__ = (Index("ix_secevent_type_ip_time", "type", "ip_hash", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    type: Mapped[str] = mapped_column(String(48))
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    detail: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)


class UsedChallenge(Base):
    """Anti-bot challenges already redeemed (prevents replay)."""

    __tablename__ = "used_challenges"

    challenge: Mapped[str] = mapped_column(String(64), primary_key=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1024), unique=True)
    p256dh: Mapped[str] = mapped_column(String(255))
    auth: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


# ---------------------------------------------------------------------------
# Temporary messaging data (TTL)
# ---------------------------------------------------------------------------


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (
        Index("ix_conv_initiator", "initiator_id", "created_at"),
        Index("ix_conv_recipient", "recipient_id", "created_at"),
        Index("ux_conv_direct_key", "direct_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    initiator_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    recipient_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    status: Mapped[str] = mapped_column(String(16), default="active")  # active|closed
    closed_by_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_message_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)

    initiator_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    recipient_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    initiator_last_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    recipient_last_read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Anti-spam: how many messages in a row the same side has sent.
    last_sender_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    consecutive_count: Mapped[int] = mapped_column(Integer, default=0)

    # --- V4 ---
    kind: Mapped[str | None] = mapped_column(String(12), nullable=True)  # anonymous (None) | direct
    direct_key: Mapped[str | None] = mapped_column(String(80), nullable=True)  # "a:b" sorted ids: one direct chat per pair
    request_state: Mapped[str | None] = mapped_column(String(12), nullable=True)  # direct: pending|accepted|ignored
    initiator_revealed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # anonymous: identity shown to the peer
    recipient_revealed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    initiator_muted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recipient_muted: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Each side has written at least once (calls unlock only after the other side replied).
    initiator_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    recipient_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    def is_participant(self, user_id: str) -> bool:
        return user_id in (self.initiator_id, self.recipient_id)

    def peer_of(self, user_id: str) -> str:
        return self.recipient_id if user_id == self.initiator_id else self.initiator_id

    def hidden_for(self, user_id: str) -> bool:
        return self.initiator_hidden if user_id == self.initiator_id else self.recipient_hidden

    def set_hidden(self, user_id: str, value: bool) -> None:
        if user_id == self.initiator_id:
            self.initiator_hidden = value
        else:
            self.recipient_hidden = value

    @property
    def is_direct(self) -> bool:
        return self.kind == "direct"

    def revealed(self, user_id: str) -> bool:
        return bool(self.initiator_revealed if user_id == self.initiator_id else self.recipient_revealed)

    def has_sent(self, user_id: str) -> bool:
        return bool(self.initiator_sent if user_id == self.initiator_id else self.recipient_sent)

    def mark_sent(self, user_id: str) -> None:
        if user_id == self.initiator_id:
            self.initiator_sent = True
        else:
            self.recipient_sent = True

    def muted_for(self, user_id: str) -> bool:
        return bool(self.initiator_muted if user_id == self.initiator_id else self.recipient_muted)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("sender_id", "client_id", name="uq_message_client_id"),
        Index("ix_msg_conv_time", "conversation_id", "created_at"),
        Index("ix_msg_recipient_unread", "recipient_id", "read_at"),
        Index("ix_msg_sender_time", "sender_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id", ondelete="CASCADE"))
    sender_id: Mapped[str] = mapped_column(String(32))
    recipient_id: Mapped[str] = mapped_column(String(32))
    content: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # idempotency key
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # V4: system notices inside a chat (identity revealed, missed call, call ended…)
    kind: Mapped[str | None] = mapped_column(String(12), nullable=True)  # text (None) | system | image (V5)
    meta: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON for system messages
    media_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # V5: ephemeral picture (MediaItem)


# ---------------------------------------------------------------------------
# Public ideas (posts) — a separate area from private anonymous messaging.
# Posts are public; comments are private to the post's author; reactions are
# one per user per post (enforced by a unique constraint).
# ---------------------------------------------------------------------------


class ProfileRef(Base):
    """Stable public reference used to open someone's ideas profile.

    It is NOT the internal user id and is never shown in anonymous messaging,
    so a person's public posts cannot be linked to their private conversations.
    """

    __tablename__ = "profile_refs"

    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    ref: Mapped[str] = mapped_column(String(32), unique=True, default=new_public_id)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Post(Base):
    __tablename__ = "posts"
    __table_args__ = (
        UniqueConstraint("author_id", "client_id", name="uq_post_client_id"),
        Index("ix_post_author_time", "author_id", "created_at"),
        Index("ix_post_status_time", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    content: Mapped[str] = mapped_column(Text)
    client_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # idempotency key
    status: Mapped[str] = mapped_column(String(16), default="visible")  # visible|removed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    # Denormalised counters: read on every feed request, updated atomically.
    likes_count: Mapped[int] = mapped_column(Integer, default=0)  # REAL reactions only (PostReaction rows)
    dislikes_count: Mapped[int] = mapped_column(Integer, default=0)
    # V5 team boosts. Removed in V6: zeroed by `admin_cli drop-legacy`, never read (counts are real only).
    boost_likes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    boost_dislikes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    unseen_comments_count: Mapped[int] = mapped_column(Integer, default=0)  # for the author only
    # V5: one picture (MediaItem). status may then also be "pending" (awaiting approval) or "hidden" (reported).
    media_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class PostReaction(Base):
    __tablename__ = "post_reactions"
    __table_args__ = (
        UniqueConstraint("post_id", "user_id", name="uq_reaction_post_user"),
        CheckConstraint("reaction_type IN ('like', 'dislike')", name="ck_reaction_type"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reaction_type: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (Index("ix_comment_post_time", "post_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    post_id: Mapped[str] = mapped_column(ForeignKey("posts.id", ondelete="CASCADE"))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    # --- V6 phase 4: public comments. NULL = a private comment from before (seen by the post owner and its
    # writer only; never made public by the code). New comments are "public".
    visibility: Mapped[str | None] = mapped_column(String(8), nullable=True)
    parent_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)  # one level of replies
    reply_to_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # "@name" inside a thread
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # soft delete (kept for reports)


class Notification(Base):
    """V6 phase 4: in-app notifications (comment, reply; later membership, rewards, withdrawals, giveaway)."""

    __tablename__ = "notifications"
    __table_args__ = (Index("ix_notif_user_time", "user_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(24))
    actor_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    post_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    comment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON (text shown, amounts…)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MediaCacheEntry(Base):
    """Processed media files on the server's disk cache (LRU + size cap + TTL)."""

    __tablename__ = "media_cache"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)  # "<asset_id>.<variant>"
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_access: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)


class SupportTicket(Base):
    """V5 support ticket. Its number (id) is shown to the user (#1024) and in the e-mail subject."""

    __tablename__ = "support_tickets"
    __table_args__ = (Index("ix_ticket_user_time", "user_id", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    category: Mapped[str] = mapped_column(String(16))  # technical|account|verification|payment|report|suggestion|other
    subject: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(10), default="open", index=True)  # open|answered|closed
    user_unread: Mapped[bool] = mapped_column(Boolean, default=False)  # a reply the user has not seen yet
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"
    __table_args__ = (Index("ix_support_msg_ticket", "ticket_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ticket_id: Mapped[int] = mapped_column(ForeignKey("support_tickets.id", ondelete="CASCADE"))
    author: Mapped[str] = mapped_column(String(8))  # user|support
    admin: Mapped[str | None] = mapped_column(String(80), nullable=True)  # who answered (panel only)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class VerificationRequest(Base):
    """V5 blue-star request with its (manual, crypto) payment. No personal data is asked."""

    __tablename__ = "verification_requests"
    __table_args__ = (Index("ux_verif_txid", "txid", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    account_type: Mapped[str] = mapped_column(String(12))  # writer|creator|page|other
    description: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    amount: Mapped[str] = mapped_column(String(32))  # decimal as text, e.g. "5.00"
    currency: Mapped[str] = mapped_column(String(16))
    network: Mapped[str] = mapped_column(String(16))
    wallet: Mapped[str] = mapped_column(String(128))  # the address shown when the user paid
    txid: Mapped[str] = mapped_column(String(160))  # unique across all requests (one payment = one request)
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending|accepted|rejected|needs_fix
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)  # reason for rejection / what to fix
    stats: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON snapshot of the conditions at submission
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class EmailCode(Base):
    """One-time e-mail code (V6: confirms sensitive account changes such as the refund wallet)."""

    __tablename__ = "email_codes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    purpose: Mapped[str] = mapped_column(String(16))  # V6: refund | withdraw | giveaway ("payout" was V5)
    email: Mapped[str] = mapped_column(String(254))
    code_hash: Mapped[str] = mapped_column(String(64))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class LedgerEntry(Base):
    """An IMMUTABLE money ledger. Balance = sum(amount). Nothing is ever updated or deleted;
    a mistake is corrected by a new entry. (V5 creator earnings were exported and removed in V6;
    the table is reused by memberships in V6 phase 5.)"""

    __tablename__ = "ledger_entries"
    __table_args__ = (Index("ix_ledger_user_time", "user_id", "created_at"),
                      Index("ux_ledger_reverses", "reverses_id", unique=True),
                      Index("ux_ledger_source", "account", "source_type", "source_id", "kind", unique=True))

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(12))  # earning|payout|adjustment|reversal
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # cents; earnings > 0, payouts < 0
    currency: Mapped[str] = mapped_column(String(12))
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    paid_on: Mapped[str | None] = mapped_column(String(10), nullable=True)  # payouts: YYYY-MM-DD of the Red Packet
    reverses_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    # --- V6 phase 5: written only by app.services.ledger.post (membership | rewards accounts)
    account: Mapped[str | None] = mapped_column(String(12), nullable=True, index=True)
    source_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    available_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # rewards on hold until then


class MembershipRequest(Base):
    """V6 phase 5: a membership payment (TXID) waiting for the admin. Kept if the account is deleted (accounting)."""

    __tablename__ = "membership_requests"
    __table_args__ = (Index("ux_membership_txid", "txid", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    user_public_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # the price asked when the request was sent (cents)
    currency: Mapped[str] = mapped_column(String(16))
    network: Mapped[str] = mapped_column(String(16))
    wallet: Mapped[str] = mapped_column(String(128))
    txid: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending|accepted|rejected
    admin_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class MembershipRefund(Base):
    """V6 phase 5: a refund the member asked for (within the window); the admin records the transfer TXID."""

    __tablename__ = "membership_refunds"
    __table_args__ = (Index("ux_refund_txid", "refund_txid", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    user_public_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    request_id: Mapped[str] = mapped_column(String(32))
    network: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(128))
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # sent back (price - fee)
    fee_minor: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(String(10), default="requested", index=True)  # requested|done|rejected
    refund_txid: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class Referral(Base):
    """V6 phase 5b: one-level invitation (an account made through someone's link). The reward is a ledger entry."""

    __tablename__ = "referrals"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    referrer_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    referee_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="joined", index=True)  # joined|review|rewarded|rejected|reversed
    flags: Mapped[str | None] = mapped_column(String(200), nullable=True)  # same_network,same_device,burst
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    rewarded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class RewardBatch(Base):
    """V6 phase 5b: a group reward from the panel (activity criteria only)."""

    __tablename__ = "reward_batches"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    amount_minor: Mapped[int] = mapped_column(BigInteger)
    criteria: Mapped[str] = mapped_column(Text)  # JSON
    note: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(10), default="running")
    count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    done_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WithdrawalRequest(Base):
    """V6 phase 5b: a withdrawal of rewards; the amount left the balance when it was asked for."""

    __tablename__ = "withdrawal_requests"
    __table_args__ = (Index("ux_withdraw_txid", "txid", unique=True),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    user_public_id: Mapped[str | None] = mapped_column(String(12), nullable=True)
    network: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(128))
    amount_minor: Mapped[int] = mapped_column(BigInteger)  # taken from the balance
    fee_minor: Mapped[int] = mapped_column(BigInteger)  # deducted from what is sent
    status: Mapped[str] = mapped_column(String(10), default="pending", index=True)  # pending|done|rejected
    txid: Mapped[str | None] = mapped_column(String(160), nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class GiveawayRound(Base):
    """V6 phase 5c: a «الظرف الأحمر» round."""

    __tablename__ = "giveaway_rounds"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    title: Mapped[str] = mapped_column(String(120))
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    winners_count: Mapped[int] = mapped_column(Integer)
    ends_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(10), default="open")  # open|drawn|sent|cancelled
    show_winners: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    draw_log: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON: entrants count + SHA-256, picks, time, admin
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    drawn_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GiveawayEntry(Base):
    __tablename__ = "giveaway_entries"
    __table_args__ = (UniqueConstraint("round_id", "user_id", name="uq_giveaway_user"),
                      UniqueConstraint("round_id", "email", name="uq_giveaway_email"))

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    round_id: Mapped[str] = mapped_column(ForeignKey("giveaway_rounds.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(254))
    email_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    flags: Mapped[str | None] = mapped_column(String(100), nullable=True)  # dup_network: excluded from the draw
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class GiveawayWinner(Base):
    __tablename__ = "giveaway_winners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    round_id: Mapped[str] = mapped_column(ForeignKey("giveaway_rounds.id", ondelete="CASCADE"), index=True)
    entry_id: Mapped[str] = mapped_column(ForeignKey("giveaway_entries.id", ondelete="CASCADE"), unique=True)
    code_sealed: Mapped[str | None] = mapped_column(Text, nullable=True)  # prize code, sealed with SECRET_KEY (admins only)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MediaItem(Base):
    """A picture or video a user uploaded (V5). The file itself lives in the private Telegram storage
    channel; we keep its file_id (never sent to clients) and a temporary prepared copy in the media cache.

    `id` is a random internal 128-bit hex id: it names cache files and appears in signed media URLs.
    """

    __tablename__ = "media_items"
    __table_args__ = (Index("ix_media_owner_purpose", "owner_id", "purpose", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    # Kept (NULL) when the account is deleted: reported media may be legal evidence.
    owner_id: Mapped[str | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_public_id: Mapped[str | None] = mapped_column(String(12), nullable=True)  # for moderators (DZ-XXXXXX)
    purpose: Mapped[str] = mapped_column(String(8))  # idea|chat
    kind: Mapped[str] = mapped_column(String(8))  # image|video
    # uploaded -> processing -> ready (stored in Telegram) | rejected (checks failed) | failed (error)
    # then: attached (in use) -> removed (moderation / owner) | expired (chat pictures)
    state: Mapped[str] = mapped_column(String(12), default="uploaded", index=True)
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    conversation_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # chat pictures: target chat
    attached_type: Mapped[str | None] = mapped_column(String(8), nullable=True)  # post|message
    attached_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    attached_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    review: Mapped[str | None] = mapped_column(String(10), nullable=True)  # pending|approved|rejected (None = not needed)
    hidden: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # hidden after reports, until reviewed
    legal_hold: Mapped[bool | None] = mapped_column(Boolean, nullable=True)  # reported: kept as evidence, never auto-deleted
    reports_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # stored (re-encoded) file
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    nsfw_score: Mapped[float | None] = mapped_column(Float, nullable=True)  # server check: max P(porn+hentai)
    blur: Mapped[str | None] = mapped_column(Text, nullable=True)  # tiny blurred preview (data: URI) for chat pictures
    tg_file_id: Mapped[str | None] = mapped_column(String(255), nullable=True)  # SERVER ONLY
    tg_unique_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # in the storage channel
    tg_mod_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # in the moderation chat
    ready: Mapped[bool] = mapped_column(Boolean, default=False)  # prepared copy present in the media cache
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)  # chat: unopened TTL
    viewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    view_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


# ---------------------------------------------------------------------------
# Password recovery (admin-assisted; the code reaches the user by e-mail)
# ---------------------------------------------------------------------------


class PasswordReset(Base):
    __tablename__ = "password_resets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    short_id: Mapped[str] = mapped_column(String(12), unique=True)  # shown to the admin in Telegram
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    code_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)  # argon2 hash, never the code
    # pending (waiting for admin) | sent (code e-mailed) | verified | used | cancelled | expired | failed
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    token_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)  # after a correct code
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    ip_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    code_set_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


# ---------------------------------------------------------------------------
# Admin panel: separate accounts with TOTP, sessions, tamper-evident audit log
# ---------------------------------------------------------------------------


class AdminUser(Base):
    __tablename__ = "admin_users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[str] = mapped_column(Text)  # encrypted with a key derived from SECRET_KEY
    last_totp_step: Mapped[int | None] = mapped_column(BigInteger, nullable=True)  # replay guard
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    role: Mapped[str | None] = mapped_column(String(32), nullable=True)  # None = super_admin
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AdminSession(Base):
    __tablename__ = "admin_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    admin_id: Mapped[str] = mapped_column(ForeignKey("admin_users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AdminAuditLog(Base):
    """Append-only: the panel has no update/delete path. Each row chains the
    previous row's hash, so silent edits in the database are detectable."""

    __tablename__ = "admin_audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    actor: Mapped[str] = mapped_column(String(80))  # admin username, "telegram" or "cli"
    action: Mapped[str] = mapped_column(String(64), index=True)
    target_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip_ref: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)
    prev_hash: Mapped[str] = mapped_column(String(64))
    row_hash: Mapped[str] = mapped_column(String(64))


class CommentCategory(Base):
    """Admin-managed categories of the comment library (ترحيب، تشجيع، دعم…)."""

    __tablename__ = "comment_categories"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class CannedComment(Base):
    """Ready-made comments the team can post from the panel."""

    __tablename__ = "canned_comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    category: Mapped[str] = mapped_column(String(32), index=True)  # CommentCategory.id
    text: Mapped[str] = mapped_column(Text)
    usage_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class EngagementJob(Base):
    """A batch of team comments applied gradually by the scheduler (V5 "boost" jobs were removed in V6).

    comment: `payload` is a JSON list of comments; `applied` of them are already posted.
    """

    __tablename__ = "engagement_jobs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    batch_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(8))  # comment
    target_type: Mapped[str] = mapped_column(String(8))  # idea
    target_id: Mapped[str] = mapped_column(String(32), index=True)
    metric: Mapped[str | None] = mapped_column(String(8), nullable=True)  # unused since V6
    total: Mapped[int] = mapped_column(Integer, default=0)
    applied: Mapped[int] = mapped_column(Integer, default=0)
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(12), default="running", index=True)  # running|done|cancelled|failed
    created_by: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class MarketSnapshot(Base):
    """V6 phase 2: the last good market list (gainers/losers from Bybit), shared by every app instance.

    A failed refresh keeps the previous row, so the app shows it with a "stale" notice."""

    __tablename__ = "market_snapshots"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)  # "latest"
    data: Mapped[str] = mapped_column(Text)  # JSON
    source: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime)  # when Bybit last answered
    checked_at: Mapped[datetime] = mapped_column(DateTime)  # last attempt (success or not)
    last_error: Mapped[str | None] = mapped_column(String(200), nullable=True)


class AppSetting(Base):
    """Settings changed from the admin panel at runtime (e.g. the Telegram bot).

    Secrets are stored sealed (Fernet, key derived from SECRET_KEY) and are never sent back to the
    browser. A value here takes precedence over the same setting in .env.
    """

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ContentBlock(Base):
    """V6 phase 8: an edited text of the content system (services/cms.py). No row = the default text in the code."""

    __tablename__ = "content_blocks"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body_md: Mapped[str] = mapped_column(Text, default="")
    major_version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(80), nullable=True)


class ContentRevision(Base):
    """V6 phase 8: every save of a text (and «back to the default»), for the history and restore."""

    __tablename__ = "content_revisions"
    __table_args__ = (Index("ix_content_revisions_key", "key", "id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    body_md: Mapped[str] = mapped_column(Text, default="")
    major_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    created_by: Mapped[str | None] = mapped_column(String(80), nullable=True)
    note: Mapped[str | None] = mapped_column(String(300), nullable=True)
