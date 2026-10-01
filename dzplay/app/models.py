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
    Boolean,
    DateTime,
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
    reason: Mapped[str] = mapped_column(String(32))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Minimal evidence: only the reported content, copied so it survives TTL.
    snapshot: Mapped[str] = mapped_column(Text, default="[]")
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
