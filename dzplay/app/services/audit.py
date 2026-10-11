"""Append-only, hash-chained audit log of admin actions.

Every admin action (and every view of private content) is recorded with who,
when, what and why. Rows are never updated or deleted from the panel; each row
stores the hash of the previous one, so `verify_chain` detects edits made
directly in the database.
"""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.models import AdminAuditLog
from app.services.messaging import iso

GENESIS = "0" * 64


def _row_digest(prev_hash: str, row: AdminAuditLog) -> str:
    payload = json.dumps([
        prev_hash, row.actor, row.action, row.target_type, row.target_id, row.reason, row.detail, row.ip_ref,
        row.created_at.isoformat(timespec="microseconds"),
    ], ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def record(db: Session, actor: str, action: str, *, target_type: str | None = None, target_id: str | None = None,
           reason: str | None = None, detail: str | None = None, ip_ref: str | None = None) -> AdminAuditLog:
    last = db.execute(select(AdminAuditLog.row_hash).order_by(AdminAuditLog.id.desc()).limit(1)).scalar()
    row = AdminAuditLog(
        actor=actor[:80], action=action[:64], target_type=target_type, target_id=(target_id or None) and target_id[:64],
        reason=(reason or None) and reason[:255], detail=detail, ip_ref=ip_ref, created_at=clock.utcnow(),
        prev_hash=last or GENESIS, row_hash="",
    )
    row.row_hash = _row_digest(row.prev_hash, row)
    db.add(row)
    db.flush()
    return row


def serialize(row: AdminAuditLog) -> dict:
    return {
        "id": row.id, "actor": row.actor, "action": row.action, "target_type": row.target_type,
        "target_id": row.target_id, "reason": row.reason, "detail": row.detail, "ip_ref": row.ip_ref,
        "at": iso(row.created_at),
    }


def list_entries(db: Session, limit: int = 100, before_id: int | None = None, action: str | None = None) -> list[dict]:
    q = select(AdminAuditLog).order_by(AdminAuditLog.id.desc()).limit(min(limit, 500))
    if before_id:
        q = q.where(AdminAuditLog.id < before_id)
    if action:
        q = q.where(AdminAuditLog.action == action)
    return [serialize(r) for r in db.execute(q).scalars()]


def verify_chain(db: Session) -> dict:
    """Walk the chain; rows removed by the retention policy start a new segment."""
    prev = None
    count = 0
    for row in db.execute(select(AdminAuditLog).order_by(AdminAuditLog.id)).scalars():
        expected_prev = prev if prev is not None else row.prev_hash
        if row.prev_hash != expected_prev or _row_digest(row.prev_hash, row) != row.row_hash:
            return {"ok": False, "broken_at": row.id, "checked": count}
        prev = row.row_hash
        count += 1
    return {"ok": True, "checked": count}
