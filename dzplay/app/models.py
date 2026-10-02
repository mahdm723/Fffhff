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


class User(Base):
    __tablename__ = "users"

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
    reel_comment_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
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
    likes_count: Mapped[int] = mapped_column(Integer, default=0)
    dislikes_count: Mapped[int] = mapped_column(Integer, default=0)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    unseen_comments_count: Mapped[int] = mapped_column(Integer, default=0)  # for the author only


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


# ---------------------------------------------------------------------------
# Reels — platform content uploaded by the admins through Telegram. The media
# files stay in Telegram; we keep only references and a temporary disk cache.
# ---------------------------------------------------------------------------


class Reel(Base):
    __tablename__ = "reels"
    __table_args__ = (Index("ix_reel_status_time", "status", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    short_id: Mapped[str] = mapped_column(String(12), unique=True)  # shown in the bot (/hide <id>)
    kind: Mapped[str] = mapped_column(String(8))  # video|images
    caption: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="processing")  # processing|visible|hidden|failed
    error: Mapped[str | None] = mapped_column(String(255), nullable=True)
    media_group_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    pinned_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    likes_count: Mapped[int] = mapped_column(Integer, default=0)
    dislikes_count: Mapped[int] = mapped_column(Integer, default=0)
    comments_count: Mapped[int] = mapped_column(Integer, default=0)
    views_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class ReelAsset(Base):
    __tablename__ = "reel_assets"
    __table_args__ = (Index("ix_asset_reel_pos", "reel_id", "position"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    reel_id: Mapped[str] = mapped_column(ForeignKey("reels.id", ondelete="CASCADE"))
    position: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(8))  # video|image
    tg_file_id: Mapped[str] = mapped_column(String(255))
    tg_unique_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tg_message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    source_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    ready: Mapped[bool] = mapped_column(Boolean, default=False)


class ReelReaction(Base):
    __tablename__ = "reel_reactions"
    __table_args__ = (
        UniqueConstraint("user_id", "reel_id", name="uq_reel_reaction"),
        CheckConstraint("reaction IN ('like','dislike')", name="ck_reel_reaction"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reel_id: Mapped[str] = mapped_column(ForeignKey("reels.id", ondelete="CASCADE"), index=True)
    reaction: Mapped[str] = mapped_column(String(8))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class ReelComment(Base):
    """Public comments on Reels — a separate table from the owner-only Ideas comments."""

    __tablename__ = "reel_comments"
    __table_args__ = (Index("ix_reel_comment_time", "reel_id", "created_at"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_public_id)
    reel_id: Mapped[str] = mapped_column(ForeignKey("reels.id", ondelete="CASCADE"))
    author_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)


class ReelView(Base):
    """Light, temporary "seen" marks so a session does not replay recent reels first."""

    __tablename__ = "reel_views"
    __table_args__ = (UniqueConstraint("user_id", "reel_id", name="uq_reel_view"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reel_id: Mapped[str] = mapped_column(ForeignKey("reels.id", ondelete="CASCADE"))
    seen_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)


class MediaCacheEntry(Base):
    """Processed media files on the server's disk cache (LRU + size cap + TTL)."""

    __tablename__ = "media_cache"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)  # "<asset_id>.<variant>"
    asset_id: Mapped[str] = mapped_column(String(32), index=True)
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    last_access: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow, index=True)


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


class CannedComment(Base):
    """Ready-made comments the admins can post from the official account."""

    __tablename__ = "canned_comments"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_internal_id)
    category: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=clock.utcnow)
