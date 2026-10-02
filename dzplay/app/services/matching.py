"""Anonymous recipient selection.

Candidates are filtered by a list of named rules (SQL conditions) and then
picked with a weighted random draw that favours people who received fewer new
conversations recently (balanced distribution) and who were active recently.

Rules (configure optional ones with MATCHING_RULES):
  mandatory  not_self, active_status, not_blocked
  optional   no_open_conversation  – never two live conversations with the same person
             not_recent_partner    – skip the last N people the sender wrote to / heard from
             inbound_capacity      – skip people who already got MATCH_MAX_INBOUND_NEW_PER_DAY
                                     new conversations in the last 24h

To add a rule: write a function (ctx) -> SQL condition and register it in RULES.
"""

from __future__ import annotations

import secrets
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, func, or_, select, true
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import Block, Conversation, User

_rng = secrets.SystemRandom()


@dataclass
class MatchContext:
    db: Session
    settings: Settings
    sender_id: str
    now: datetime


def _not_self(ctx: MatchContext):
    return User.id != ctx.sender_id


def _active_status(ctx: MatchContext):
    return User.status == "active"


def _not_blocked(ctx: MatchContext):
    return ~exists().where(
        or_(
            and_(Block.blocker_id == ctx.sender_id, Block.blocked_id == User.id),
            and_(Block.blocker_id == User.id, Block.blocked_id == ctx.sender_id),
        )
    )


def _no_open_conversation(ctx: MatchContext):
    return ~exists().where(
        Conversation.status == "active",
        or_(
            and_(Conversation.initiator_id == ctx.sender_id, Conversation.recipient_id == User.id),
            and_(Conversation.initiator_id == User.id, Conversation.recipient_id == ctx.sender_id),
        ),
    )


def _not_recent_partner(ctx: MatchContext):
    n = ctx.settings.MATCH_EXCLUDE_RECENT_PARTNERS
    if n <= 0:
        return true()
    rows = ctx.db.execute(
        select(Conversation.initiator_id, Conversation.recipient_id)
        .where(or_(Conversation.initiator_id == ctx.sender_id, Conversation.recipient_id == ctx.sender_id))
        .order_by(Conversation.created_at.desc())
        .limit(n)
    ).all()
    recent = {b if a == ctx.sender_id else a for a, b in rows}
    return User.id.not_in(recent) if recent else true()


def _inbound_capacity(ctx: MatchContext):
    since = ctx.now - timedelta(days=1)
    inbound = (
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.recipient_id == User.id, Conversation.created_at > since)
        .correlate(User)
        .scalar_subquery()
    )
    return inbound < ctx.settings.MATCH_MAX_INBOUND_NEW_PER_DAY


RULES: dict[str, Callable[[MatchContext], object]] = {
    "not_self": _not_self,
    "active_status": _active_status,
    "not_blocked": _not_blocked,
    "no_open_conversation": _no_open_conversation,
    "not_recent_partner": _not_recent_partner,
    "inbound_capacity": _inbound_capacity,
}


def pick_recipient(db: Session, settings: Settings, sender_id: str) -> str | None:
    """Return the internal id of a suitable random recipient, or None."""
    ctx = MatchContext(db=db, settings=settings, sender_id=sender_id, now=clock.utcnow())
    conditions = [RULES[name](ctx) for name in settings.matching_rules]
    # team accounts (official + system) never receive anonymous messages
    conditions.extend([User.is_official.is_not(True), User.is_system.is_not(True)])

    since = ctx.now - timedelta(days=1)
    inbound_today = (
        select(func.count())
        .select_from(Conversation)
        .where(Conversation.recipient_id == User.id, Conversation.created_at > since)
        .correlate(User)
        .scalar_subquery()
    )
    rows = db.execute(
        select(User.id, User.last_active_at, inbound_today.label("inbound"))
        .where(*conditions)
        .order_by(func.random())
        .limit(max(1, settings.MATCH_CANDIDATE_POOL))
    ).all()
    if not rows:
        return None

    weights = []
    for _uid, last_active, inbound in rows:
        w = 1.0 / (1 + (inbound or 0))  # balance load across recipients
        if last_active and ctx.now - last_active < timedelta(days=1):
            w *= settings.MATCH_RECENT_ACTIVITY_BOOST
        weights.append(w)
    return _rng.choices([r[0] for r in rows], weights=weights, k=1)[0]
