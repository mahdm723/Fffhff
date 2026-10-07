"""V6 phase 1: Reels, the creator studio + earnings, calls and the V5 boost are gone — no route, no WebSocket
message, no front-end file — and the data of an old database is exported, then dropped, without touching
anything else (users and their blue stars, ideas, chats, settings)."""

from __future__ import annotations

import json
import re
import secrets
from pathlib import Path

import pytest
from sqlalchemy import inspect, text

from app import clock
from app.models import User
from tests.conftest import send
from tests.legacy_v5_schema import DDL, LEGACY_TABLES
from tests.test_admin_access import _all_routes

ROOT = Path(__file__).resolve().parents[1]
REMOVED = re.compile(r"reel|call|turn|studio|monetiz|money|ledger", re.I)
REMOVED_JS = ("js/views/reels.js", "js/views/studio.js", "js/views/money.js", "js/reels-prefetch.js", "js/call.js")


def test_no_route_ws_message_or_front_end_file_for_removed_features(hx):
    paths = [p for p, _ in _all_routes(hx.app)]
    assert paths and not [p for p in paths if REMOVED.search(p)]
    from app.api.ws import HANDLERS

    assert set(HANDLERS) == {"ping", "typing"}
    cfg = hx.client().get("/api/config").json()
    assert not [k for k in cfg if REMOVED.search(k) or k.startswith(("reels", "max_reel"))]
    for f in REMOVED_JS:
        assert not (ROOT / "static" / f).exists(), f
    shell = (ROOT / "static" / "sw.js").read_text() + (ROOT / "static" / "index.html").read_text()
    for f in REMOVED_JS:
        assert f.split("/")[-1] not in shell, f
    for js in (ROOT / "static" / "js").rglob("*.js"):
        body = js.read_text(encoding="utf-8")
        for f in REMOVED_JS:
            assert f"/{f.split('/')[-1]}'" not in body and f"./{f.split('/')[-1]}" not in body, (js.name, f)
    compose = (ROOT / "docker-compose.yml").read_text()
    assert "\n  coturn:" not in compose and "image: coturn" not in compose and "TURN_" not in compose
    assert not (ROOT / "deploy" / "coturn").exists()


# ----------------------------------------------------------------- an old (V5) database


def _insert(conn, table: str, **values) -> None:
    """INSERT with every NOT NULL column filled (legacy tables have no model class any more)."""
    now = clock.utcnow()
    for col in inspect(conn).get_columns(table):
        name, kind = col["name"], str(col["type"]).upper()
        if name in values or col["nullable"] or col.get("autoincrement") is True:
            continue
        if "INT" in kind:
            values[name] = 0
        elif "BOOL" in kind:
            values[name] = False
        elif "DATE" in kind or "TIME" in kind:
            values[name] = now
        elif "FLOAT" in kind or "NUMERIC" in kind or "REAL" in kind or "DOUBLE" in kind:
            values[name] = 0.0
        else:
            values[name] = "x"
    cols = ", ".join(f'"{k}"' for k in values)
    conn.execute(text(f'INSERT INTO "{table}" ({cols}) VALUES ({", ".join(":" + k for k in values)})'), values)


def _uid(hx, c) -> str:
    with hx.db() as db:
        return db.query(User).filter_by(email=c.email).one().id


def test_old_database_export_then_drop(make_harness, monkeypatch, capsysbinary):
    from app import admin_cli
    from app.config import get_settings

    hx = make_harness()
    a, b = hx.user(), hx.user()
    ua, ub = _uid(hx, a), _uid(hx, b)
    assert hx.admin().post(f"/api/admin/users/{ua}/verified", json={"verified": True}).status_code == 200
    pid = a.post("/api/posts", json={"content": "فكرة باقية"}).json()["id"]
    cid = send(a, b, "رسالة باقية").json()["conversation"]["id"]
    engine = hx.state.database.engine
    reel_media = secrets.token_hex(16)
    cache_file = Path(hx.settings.MEDIA_CACHE_DIR) / f"{reel_media}.mp4.mp4"
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_bytes(b"old video")
    with engine.begin() as conn:
        for stmt in DDL[engine.dialect.name]:
            conn.execute(text(stmt))
        _insert(conn, "reels", id="r1", short_id="ab12cd", kind="video", caption="مقطع", status="visible", owner_id=ua)
        _insert(conn, "reel_assets", id=secrets.token_hex(16), reel_id="r1", kind="video", tg_file_id="tg-file")
        _insert(conn, "reel_comments", id="rc1", reel_id="r1", author_id=ub, content="تعليق مقطع")
        _insert(conn, "reel_reactions", reel_id="r1", user_id=ub, reaction="like")
        _insert(conn, "reel_views", id="rv1", reel_id="r1", user_id=ub)
        _insert(conn, "calls", id="c1", caller_id=ua, callee_id=ub, kind="audio", state="ended", conversation_id=cid)
        _insert(conn, "monetization_applications", id="m1", user_id=ua, content_type="مقاطع", payout_email="p@example.com",
                status="accepted")
        _insert(conn, "ledger_entries", user_id=ua, kind="earning", amount_minor=1234, currency="USDT", created_by="admin:x")
        _insert(conn, "email_codes", id="e1", user_id=ua, purpose="payout", email="p@example.com", code_hash="h",
                expires_at=clock.utcnow())
        _insert(conn, "media_items", id=reel_media, owner_id=ua, purpose="reel", kind="video", state="attached")
        _insert(conn, "media_cache", key=cache_file.name, asset_id=reel_media, size=9)
        conn.execute(text("UPDATE posts SET boost_likes = 500 WHERE id = :p"), {"p": pid})
        _insert(conn, "engagement_jobs", id="j1", batch_id="b1", kind="boost", target_type="idea", target_id=pid,
                metric="likes", total=5, start_at=clock.utcnow(), end_at=clock.utcnow(), created_by="x")
        _insert(conn, "messages", id="msgcall", conversation_id=cid, sender_id=ua, recipient_id=ub, content="مكالمة فائتة",
                kind="system", meta=json.dumps({"event": "call", "state": "missed"}, ensure_ascii=False),
                expires_at=clock.utcnow())
        _insert(conn, "reports", id="rep1", reporter_id=ub, reported_user_id=ua, reason="spam", call_id="c1")
        _insert(conn, "app_settings", key="tun.CREATOR_REELS_ENABLED", value="true")
        _insert(conn, "app_settings", key="tun.UPLOADS_PER_HOUR", value="7")

    # the new code runs fine while the old tables are still there
    hx.state.database.create_all()
    assert a.get("/api/posts/feed").status_code == 200
    assert next(p for p in a.get("/api/posts/feed").json()["posts"] if p["id"] == pid)["likes"] == 0  # boost never shown
    msgs = b.get(f"/api/conversations/{cid}").json()["messages"]
    assert any(m["content"] == "رسالة باقية" for m in msgs)

    monkeypatch.setenv("DATABASE_URL", hx.settings.DATABASE_URL)
    monkeypatch.setenv("MEDIA_CACHE_DIR", hx.settings.MEDIA_CACHE_DIR)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as status:  # deploy/v6-cleanup.sh and install.sh ask this first
            admin_cli.main(["legacy-status"])
        assert status.value.code == 1 and '"reels": 1' in capsysbinary.readouterr().out.decode()
        admin_cli.main(["stars-count"])
        assert capsysbinary.readouterr().out.decode().strip() == "blue stars: 1"
        admin_cli.main(["export-legacy"])
        cap = capsysbinary.readouterr()
        export = json.loads(cap.out)
        sha = re.search(r"sha256=([0-9a-f]{64})", cap.err.decode()).group(1)
        t = export["tables"]
        for table in LEGACY_TABLES:
            assert len(t[table]) >= 1, table
        assert len(t["ledger_entries"]) == 1 and t["ledger_entries"][0]["amount_minor"] == 1234
        assert [m["id"] for m in t["messages"]] == ["msgcall"]  # the normal message is not part of it
        assert t["posts.boost"] == [{"id": pid, "boost_likes": 500, "boost_dislikes": None}]
        assert [r["key"] for r in t["app_settings"]] == ["tun.CREATOR_REELS_ENABLED"]
        assert len(t["media_items"]) == 1 and len(t["media_cache"]) == 1 and len(t["reports"]) == 1

        with pytest.raises(SystemExit, match="SHA-256"):
            admin_cli.main(["drop-legacy", "--sha", "0" * 64])
        assert "reels" in inspect(engine).get_table_names()  # refused: nothing happened
        admin_cli.main(["drop-legacy", "--sha", sha])
        assert "Removed" in capsysbinary.readouterr().out.decode()
        names = set(inspect(engine).get_table_names())
        assert not names & set(LEGACY_TABLES)
        with engine.connect() as conn:
            def one(sql, **kw):
                return conn.execute(text(sql), kw).scalar()

            assert one("SELECT COUNT(*) FROM ledger_entries") == 0
            assert one("SELECT COUNT(*) FROM email_codes") == 0
            assert one("SELECT COUNT(*) FROM media_items WHERE purpose = 'reel'") == 0
            assert one("SELECT COUNT(*) FROM media_cache") == 0
            assert one("SELECT COUNT(*) FROM engagement_jobs") == 0
            assert one("SELECT COUNT(*) FROM reports") == 0
            assert one("SELECT boost_likes FROM posts WHERE id = :p", p=pid) is None
            assert one("SELECT COUNT(*) FROM messages WHERE id = 'msgcall'") == 0
            assert one("SELECT COUNT(*) FROM messages") >= 1  # the chat itself stays
            assert one("SELECT value FROM app_settings WHERE key = 'tun.UPLOADS_PER_HOUR'") == "7"
            assert one("SELECT value FROM app_settings WHERE key = 'schema.v6_legacy_dropped'") == sha
        assert not cache_file.exists()
        admin_cli.main(["drop-legacy", "--sha", sha])
        assert "Already done" in capsysbinary.readouterr().out.decode()
        admin_cli.main(["legacy-status"])  # exit 0
        assert "Already done" in capsysbinary.readouterr().out.decode()
        admin_cli.main(["stars-count"])
        assert capsysbinary.readouterr().out.decode().strip() == "blue stars: 1"  # kept
    finally:
        get_settings.cache_clear()
    # and the app keeps working
    assert a.get("/api/me").json()["verified"] is True
    assert any(m["content"] == "رسالة باقية" for m in b.get(f"/api/conversations/{cid}").json()["messages"])


def test_a_new_install_has_nothing_to_remove(hx, monkeypatch, capsys):
    from app import admin_cli
    from app.config import get_settings

    hx.user()
    monkeypatch.setenv("DATABASE_URL", hx.settings.DATABASE_URL)
    get_settings.cache_clear()
    try:
        admin_cli.main(["legacy-status"])
        assert "Nothing to remove" in capsys.readouterr().out
    finally:
        get_settings.cache_clear()
