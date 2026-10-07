"""V6 phase 1: export, then remove, the data of the features V6 removed (Reels, the creator studio and
earnings, calls, the V5 "boost").

The code no longer knows these tables (their model classes are gone), so everything here is plain SQL by
table name, and only when the table exists. Nothing runs automatically at startup:

1. `admin_cli export-legacy`  → canonical JSON on stdout (deploy/v6-cleanup.sh encrypts it with the backup
   passphrase into BACKUP_DIR) + its SHA-256;
2. `admin_cli drop-legacy --sha <sha256>` → recomputes the export, refuses unless it is byte-for-byte what was
   saved, then deletes / drops in one transaction and records `schema.v6_legacy_dropped`.

Kept on purpose: users (blue stars included), ideas, comments, chats, tickets, star requests, pictures.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Engine, inspect, text

FLAG = "schema.v6_legacy_dropped"

# whole tables, children first (the order used to drop them)
DROP_TABLES = ("reel_views", "reel_reactions", "reel_comments", "reel_assets", "reels", "calls",
               "monetization_applications")
# rows inside tables that stay
PARTIAL = {
    "ledger_entries": "kind IN ('earning', 'payout', 'adjustment', 'reversal')",  # V5 creator earnings
    "email_codes": "purpose = 'payout'",  # V5 payout e-mail confirmation
    "media_items": "purpose = 'reel'",
    "engagement_jobs": "kind = 'boost' OR target_type = 'reel'",
    "messages": "kind = 'system' AND meta LIKE '%\"event\": \"call\"%'",  # "missed call" notices in chats
    "reports": "call_id IS NOT NULL OR reel_comment_id IS NOT NULL",
    "content_flags": "target = 'reel_comment'",
    "app_settings": ("key IN ('tun.CREATOR_REELS_ENABLED', 'tun.CREATOR_REEL_LIMIT_PER_24H', "
                     "'tun.REJECTED_COUNTS_TOWARD_LIMIT', 'tun.REELS_REQUIRE_APPROVAL', 'tun.CREATOR_REEL_MIN_SECONDS', "
                     "'tun.CREATOR_REEL_MAX_SECONDS', 'tun.CREATOR_REEL_MAX_MB', 'tun.CREATOR_UPLOAD_CHAT_TTL', "
                     "'tun.MONETIZE_ENABLED', 'tun.MONETIZE_MIN_REELS', 'tun.MONETIZE_MIN_LIKES', 'tun.VERIFY_ENABLED', "
                     "'tun.NSFW_VIDEO_FRAMES')"),
}
BOOST_COLUMNS = ("boost_likes", "boost_dislikes")


def _plain(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "base64:" + base64.b64encode(bytes(value)).decode()
    return value


def _rows(conn, table: str, where: str | None = None) -> list[dict]:
    sql = f'SELECT * FROM "{table}"' + (f" WHERE {where}" if where else "")
    result = conn.execute(text(sql))
    cols = list(result.keys())
    rows = [{c: _plain(v) for c, v in zip(cols, r)} for r in result]
    return sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=False, default=str))


def _media_cache_rows(conn, tables: set[str], asset_ids: set[str]) -> list[dict]:
    if "media_cache" not in tables or not asset_ids:
        return []
    return [r for r in _rows(conn, "media_cache") if r.get("asset_id") in asset_ids]


def collect(engine: Engine) -> dict:
    """Every legacy row, as plain JSON-able data (deterministic order)."""
    tables = set(inspect(engine).get_table_names())
    data: dict[str, list[dict]] = {}
    with engine.connect() as conn:
        for t in DROP_TABLES:
            if t in tables:
                data[t] = _rows(conn, t)
        for t, where in PARTIAL.items():
            if t in tables:
                data[t] = _rows(conn, t, where)
        if "posts" in tables:
            cols = {c["name"] for c in inspect(engine).get_columns("posts")}
            if set(BOOST_COLUMNS) <= cols:
                data["posts.boost"] = _rows(conn, "posts", "COALESCE(boost_likes, 0) <> 0 OR COALESCE(boost_dislikes, 0) <> 0")
                data["posts.boost"] = [{"id": r["id"], "boost_likes": r["boost_likes"], "boost_dislikes": r["boost_dislikes"]}
                                       for r in data["posts.boost"]]
        reel_assets = {r["id"] for r in data.get("reel_assets", [])} | {r["id"] for r in data.get("media_items", [])}
        data["media_cache"] = _media_cache_rows(conn, tables, reel_assets)
    return data


def export(engine: Engine) -> tuple[bytes, str, dict[str, int]]:
    """(canonical JSON bytes, sha256 hex, row counts)."""
    data = collect(engine)
    body = json.dumps({"format": "dzplay-v6-legacy-export/1", "tables": data}, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":")).encode()
    return body, hashlib.sha256(body).hexdigest(), {k: len(v) for k, v in data.items()}


def already_dropped(engine: Engine) -> bool:
    if "app_settings" not in set(inspect(engine).get_table_names()):
        return False
    with engine.connect() as conn:
        return conn.execute(text("SELECT 1 FROM app_settings WHERE key = :k"), {"k": FLAG}).first() is not None


def drop(engine: Engine, expected_sha: str, cache_dir: str | None = None) -> dict[str, int]:
    """Delete exactly what `export` returned (same SHA-256), in one transaction."""
    from app import clock

    _body, sha, counts = export(engine)
    if sha != (expected_sha or "").strip().lower():
        raise ValueError("the database changed since the export (SHA-256 differs): export again, then drop")
    data = collect(engine)
    tables = set(inspect(engine).get_table_names())
    asset_ids = {r["asset_id"] for r in data.get("media_cache", [])} | {r["id"] for r in data.get("media_items", [])} \
        | {r["id"] for r in data.get("reel_assets", [])}
    with engine.begin() as conn:
        if data.get("media_cache"):
            for key in [r["key"] for r in data["media_cache"]]:
                conn.execute(text("DELETE FROM media_cache WHERE key = :k"), {"k": key})
        for t, where in PARTIAL.items():
            if t in tables:
                conn.execute(text(f'DELETE FROM "{t}" WHERE {where}'))
        if "posts.boost" in data:
            conn.execute(text("UPDATE posts SET boost_likes = NULL, boost_dislikes = NULL "
                              "WHERE boost_likes IS NOT NULL OR boost_dislikes IS NOT NULL"))
        for t in DROP_TABLES:
            if t in tables:
                conn.execute(text(f'DROP TABLE "{t}"'))
        conn.execute(text("INSERT INTO app_settings (key, value, updated_at, updated_by) VALUES (:k, :v, :t, 'cli')"),
                     {"k": FLAG, "v": sha, "t": clock.utcnow()})
    if cache_dir:  # prepared copies on disk (videos, posters): best effort, ids are validated hex
        import re
        from pathlib import Path

        base = Path(cache_dir)
        for asset_id in asset_ids:
            if isinstance(asset_id, str) and re.fullmatch(r"[a-f0-9]{32}", asset_id) and base.is_dir():
                for f in base.glob(f"{asset_id}.*"):
                    f.unlink(missing_ok=True)
    return counts


def stars_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return int(conn.execute(text(
            "SELECT COUNT(*) FROM users WHERE verified_at IS NOT NULL "
            "AND (is_official IS NULL OR is_official = :f) AND (is_system IS NULL OR is_system = :f)"), {"f": False}).scalar() or 0)
