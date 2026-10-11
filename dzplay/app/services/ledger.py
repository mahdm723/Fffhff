"""V6 money ledger: the ONLY writer of amounts (memberships in phase 5, rewards and withdrawals in phase 5b).

* Immutable entries; a balance is the sum of its entries — no balance column exists anywhere.
* Two separate accounts per user: "membership" (paid / refunded membership) and "rewards" (promotional rewards
  and withdrawals). Membership money never enters any reward rule.
* Idempotency: one entry per (account, source_type, source_id, kind) — unique index ux_ledger_source. Posting the
  same thing twice (double click, retried Telegram button, two app instances) is a no-op.
* Callers lock the user first (`lock_user`): PostgreSQL holds the row lock until commit, so two withdrawals sent
  at the same moment cannot both pass the balance check.
* Kinds never reuse the V5 kinds removed by legacy_v6 (earning, payout, adjustment, reversal).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import clock
from app.models import LedgerEntry, User

ACCOUNTS = ("membership", "rewards")
KINDS = {
    "membership": ("mem_pay", "mem_refund", "mem_fee"),
    "rewards": ("referral", "referral_rev", "contest", "group", "activity", "correction", "withdraw", "withdraw_rev"),
}
CURRENCY = "USDT"


def to_minor(amount: float | int | str) -> int:
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(str(amount)) * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def from_minor(minor: int) -> str:
    sign = "-" if minor < 0 else ""
    minor = abs(int(minor))
    return f"{sign}{minor // 100}.{minor % 100:02d}"


def lock_user(db: Session, user_id: str) -> None:
    """Serialize money operations of one user (row lock until commit on PostgreSQL)."""
    db.execute(select(User.id).where(User.id == user_id).with_for_update())


def post(db: Session, *, user_id: str, account: str, kind: str, amount_minor: int, source_type: str, source_id: str,
         actor: str, note: str | None = None, available_at: datetime | None = None) -> tuple[LedgerEntry, bool]:
    """Write one entry (returns it and whether it is new). The same source twice returns the first entry."""
    if account not in ACCOUNTS or kind not in KINDS[account]:
        raise ValueError(f"invalid ledger kind {account}/{kind}")
    if not isinstance(amount_minor, int) or amount_minor == 0:
        raise ValueError("amount must be a non-zero integer (cents)")
    existing = _find(db, account, source_type, source_id, kind)
    if existing is not None:
        return existing, False
    entry = LedgerEntry(user_id=user_id, account=account, kind=kind, amount_minor=amount_minor, currency=CURRENCY,
                        source_type=source_type[:16], source_id=str(source_id)[:64], available_at=available_at,
                        note=(note or None) and note[:500], created_by=actor[:80], created_at=clock.utcnow())
    try:
        with db.begin_nested():
            db.add(entry)
            db.flush()
    except IntegrityError:  # another request wrote it first
        found = _find(db, account, source_type, source_id, kind)
        if found is None:
            raise
        return found, False
    return entry, True


def _find(db: Session, account: str, source_type: str, source_id: str, kind: str) -> LedgerEntry | None:
    return db.scalar(select(LedgerEntry).where(LedgerEntry.account == account, LedgerEntry.source_type == source_type,
                                               LedgerEntry.source_id == str(source_id), LedgerEntry.kind == kind))


def balances(db: Session, user_id: str, account: str) -> dict:
    """available / pending (rewards still on hold) / total, in cents."""
    now = clock.utcnow()
    total, pending = db.execute(select(
        func.coalesce(func.sum(LedgerEntry.amount_minor), 0),
        func.coalesce(func.sum(case(((LedgerEntry.available_at.is_not(None)) & (LedgerEntry.available_at > now)
                                     & (LedgerEntry.amount_minor > 0), LedgerEntry.amount_minor), else_=0)), 0),
    ).where(LedgerEntry.user_id == user_id, LedgerEntry.account == account)).one()
    total, pending = int(total or 0), int(pending or 0)
    return {"available": total - pending, "pending": pending, "total": total}


def entries(db: Session, user_id: str, account: str, limit: int = 100) -> list[dict]:
    rows = db.execute(select(LedgerEntry).where(LedgerEntry.user_id == user_id, LedgerEntry.account == account)
                      .order_by(LedgerEntry.id.desc()).limit(limit)).scalars().all()
    now = clock.utcnow()
    return [{"id": e.id, "kind": e.kind, "amount": from_minor(e.amount_minor), "amount_minor": e.amount_minor,
             "currency": e.currency, "source": e.source_type, "note": e.note,
             "pending": bool(e.available_at and e.available_at > now and e.amount_minor > 0),
             "available_at": e.available_at.isoformat(timespec="milliseconds") + "Z" if e.available_at else None,
             "created_at": e.created_at.isoformat(timespec="milliseconds") + "Z"} for e in rows]
