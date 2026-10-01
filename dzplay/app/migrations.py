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
