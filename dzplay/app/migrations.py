"""Minimal additive schema migration.

`Base.metadata.create_all` creates new tables but never changes existing
ones. When a release only *adds nullable columns* to an existing table (e.g.
reports.post_id), this adds them in place, so a deployed database keeps all
its data across updates. Anything more complex should use a real migration
tool (Alembic).
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from app import clock
from app.db import Base

log = logging.getLogger("dzplay.migrations")


def add_missing_columns(engine: Engine) -> list[str]:
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                if not column.nullable:
                    raise RuntimeError(f"Cannot auto-add NOT NULL column {table.name}.{column.name}; write a migration.")
                col_type = column.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'))
                added.append(f"{table.name}.{column.name}")
    for name in added:
        log.info("schema: added column %s", name)
    return added


def backfill(engine: Engine) -> dict:
    """Idempotent data steps for existing databases (V4): unique indexes on new columns and a
    public ID for every account that has none. Safe to run on every start."""
    from app.models import new_public_user_id

    done = {"public_ids": 0}
    with engine.begin() as conn:
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_users_public_id ON users (public_id)"))
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_conv_direct_key ON conversations (direct_key)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_users_name_norm ON users (name_norm)"))
        # V6 phase 5: ledger idempotency (one entry per source and kind) on databases created before it
        conn.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ux_ledger_source ON ledger_entries "
                          "(account, source_type, source_id, kind)"))
        # Conversations from before V4: the initiator always wrote first; the recipient replied if a
        # message of theirs is still stored or they were the last sender.
        conn.execute(text("UPDATE conversations SET initiator_sent = :t WHERE initiator_sent IS NULL"), {"t": True})
        conn.execute(text(
            "UPDATE conversations SET recipient_sent = :t WHERE recipient_sent IS NULL AND (last_sender_id = recipient_id "
            "OR EXISTS (SELECT 1 FROM messages m WHERE m.conversation_id = conversations.id AND m.sender_id = conversations.recipient_id))"),
            {"t": True})
        # V6 phase 1b: the moment old anonymous chats became read-only (their deletion is counted from it)
        if conn.execute(text("SELECT 1 FROM app_settings WHERE key = 'schema.v6_anon_closed_at'")).first() is None:
            now = clock.utcnow()
            conn.execute(text("INSERT INTO app_settings (key, value, updated_at, updated_by) "
                              "VALUES ('schema.v6_anon_closed_at', :v, :t, 'system')"), {"v": now.isoformat(), "t": now})
        missing = [r[0] for r in conn.execute(text("SELECT id FROM users WHERE public_id IS NULL"))]
        if missing:
            taken = {r[0] for r in conn.execute(text("SELECT public_id FROM users WHERE public_id IS NOT NULL"))}
            for uid in missing:
                pid = new_public_user_id()
                while pid in taken:
                    pid = new_public_user_id()
                taken.add(pid)
                conn.execute(text("UPDATE users SET public_id = :p WHERE id = :i"), {"p": pid, "i": uid})
            done["public_ids"] = len(missing)
    if done["public_ids"]:
        log.info("backfill: public IDs for %d accounts", done["public_ids"])
    return done
