"""V6 phase 1b: random anonymous messages are gone (no sending, no matching, no "reveal", no setting), the old
anonymous chats are read-only, and once LEGACY_ANON_RETENTION_DAYS have passed they are exported then deleted
(the same SHA-256-guarded way as phase 1). Direct chats are untouched."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import clock
from app.errors import AppError
from app.models import Conversation, Message, Report, User
from tests.conftest import chat, legacy_anonymous, reply, send
from tests.test_admin_access import _all_routes

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = {"Origin": "http://testserver"}


def _closed_at(hx) -> datetime:
    from app.services import legacy_v6

    with hx.db() as db:
        return legacy_v6.anon_closed_at(db)


def test_no_route_setting_or_front_end_code_for_random_messages(hx):
    routes = {(p, m) for p, r in _all_routes(hx.app) for m in getattr(r, "methods", None) or ()}
    assert ("/api/messages", "POST") not in routes
    assert not [p for p, _m in routes if p.endswith("/reveal")]
    a = hx.user()
    assert a.post("/api/messages", json={"content": "مرحبا"}).status_code in (404, 405)
    assert "accept_anonymous" not in a.get("/api/me").json()["privacy"]
    assert "accept_anonymous" not in a.patch("/api/me/privacy", json={"accept_anonymous": False}).json()
    assert not [k for k in hx.settings.model_dump() if k.startswith(("MATCH", "MAX_NEW_CONVERSATIONS"))]
    assert not (ROOT / "app" / "services" / "matching.py").exists()
    for js in (ROOT / "static" / "js").rglob("*.js"):
        body = js.read_text(encoding="utf-8")
        assert "'/api/messages'" not in body and "/reveal" not in body and "accept_anonymous" not in body, js.name


def test_old_anonymous_chat_is_read_only(hx):
    a, b = hx.user(), hx.user()
    cid = legacy_anonymous(hx, a, b, "رسالة مجهولة قديمة")
    conv = b.get("/api/conversations").json()["conversations"][0]
    assert conv["id"] == cid and conv["kind"] == "anonymous" and conv["read_only"] is True and conv["can_reply"] is False
    assert conv["peer"] == "dzplay" and conv["peer_card"]["public_id"] is None
    due = _closed_at(hx) + timedelta(days=hx.settings.LEGACY_ANON_RETENTION_DAYS)
    assert conv["deleted_after"] == due.isoformat(timespec="milliseconds") + "Z"
    assert [m["content"] for m in b.get(f"/api/conversations/{cid}").json()["messages"]] == ["رسالة مجهولة قديمة"]
    for c in (a, b):  # nothing new, from either side
        r = reply(c, cid, "رد جديد")
        assert r.status_code == 403 and r.json()["error"]["code"] == "anonymous_closed"
    with hx.db() as db:  # no chat picture either
        from app.services import media_items

        user = db.scalar(select(User).where(User.email == b.email))
        with pytest.raises(AppError) as err:
            media_items.chat_target(db, user, cid)
        assert err.value.code == "anonymous_closed"
    # safety stays: report, mute, block
    assert b.post(f"/api/conversations/{cid}/report", json={"reason": "harassment"}).status_code == 201
    assert b.post(f"/api/conversations/{cid}/mute", json={"muted": True}).status_code == 200
    # a direct chat between the same two people works normally
    did = chat(a, b, "مرحبا مباشرة")
    assert reply(b, did, "أهلًا").status_code == 201
    direct = next(c for c in a.get("/api/conversations").json()["conversations"] if c["id"] == did)
    assert direct["read_only"] is False and direct["can_reply"] is True and direct["deleted_after"] is None
    assert b.post(f"/api/conversations/{cid}/block").status_code == 200


def test_no_typing_in_an_old_anonymous_chat(hx):
    a, b = hx.user(), hx.user()
    cid = legacy_anonymous(hx, a, b)
    with b.websocket_connect("/api/ws", headers=ORIGIN) as wb, a.websocket_connect("/api/ws", headers=ORIGIN) as wa:
        assert wb.receive_json() == {"type": "hello"} and wa.receive_json() == {"type": "hello"}
        wa.send_json({"type": "typing", "conversation_id": cid, "on": True})
        send(a, b, "رسالة مباشرة")
        assert wb.receive_json() == {"type": "sync", "reason": "message"}  # the typing never reached b


def _cli_env(hx, monkeypatch):
    from app.config import get_settings

    monkeypatch.setenv("DATABASE_URL", hx.settings.DATABASE_URL)
    monkeypatch.setenv("MEDIA_CACHE_DIR", hx.settings.MEDIA_CACHE_DIR)
    get_settings.cache_clear()


def _status(admin_cli, capsysbinary) -> tuple[int, str]:
    try:
        admin_cli.main(["legacy-status", "--anon"])
        code = 0
    except SystemExit as exc:
        code = exc.code
    return code, capsysbinary.readouterr().out.decode()


def test_old_anonymous_chats_exported_then_deleted_after_retention(hx, monkeypatch, capsysbinary):
    from app import admin_cli
    from app.config import get_settings
    from app.services.moderation import flag_content

    a, b = hx.user(), hx.user()
    cid = legacy_anonymous(hx, a, b, "راني نعرف وين تسكن، نقتلك")
    with hx.db() as db:
        ua, ub = (db.scalar(select(User.id).where(User.email == c.email)) for c in (a, b))
        mid = db.scalar(select(Message.id).where(Message.conversation_id == cid))
        assert flag_content(db, hx.settings, target="message", text="راني نعرف وين تسكن، نقتلك", offender_id=ua,
                            victim_id=ub, message_id=mid, conversation_id=cid) is not None
        db.commit()
    assert b.post(f"/api/conversations/{cid}/report", json={"reason": "threat"}).status_code == 201
    did = chat(a, b, "محادثة مباشرة باقية")
    _cli_env(hx, monkeypatch)
    try:
        code, out = _status(admin_cli, capsysbinary)
        assert code == 2 and "deletion allowed after" in out  # not yet
        admin_cli.main(["export-legacy", "--anon"])
        cap = capsysbinary.readouterr()
        sha = re.search(r"sha256=([0-9a-f]{64})", cap.err.decode()).group(1)
        with pytest.raises(SystemExit, match="too early"):
            admin_cli.main(["drop-legacy", "--anon", "--sha", sha])

        clock.advance(hx.settings.LEGACY_ANON_RETENTION_DAYS * 86400 + 60)
        code, out = _status(admin_cli, capsysbinary)
        assert code == 1 and "ready" in out
        admin_cli.main(["export-legacy", "--anon"])
        cap = capsysbinary.readouterr()
        export = json.loads(cap.out)["tables"]
        sha = re.search(r"sha256=([0-9a-f]{64})", cap.err.decode()).group(1)
        assert [c["id"] for c in export["conversations"]] == [cid]
        assert [m["content"] for m in export["messages"]] == ["راني نعرف وين تسكن، نقتلك"]
        assert len(export["reports"]) == 1 and len(export["content_flags"]) == 1
        with pytest.raises(SystemExit, match="SHA-256"):
            admin_cli.main(["drop-legacy", "--anon", "--sha", "0" * 64])
        admin_cli.main(["drop-legacy", "--anon", "--sha", sha])
        assert "Removed" in capsysbinary.readouterr().out.decode()
        with hx.db() as db:
            assert db.scalar(select(Conversation).where(Conversation.kind.is_(None))) is None
            assert db.scalar(select(Message).where(Message.conversation_id == cid)) is None
            assert db.scalar(select(Report)) is None
            assert db.get(Conversation, did) is not None  # the direct chat stays
        code, out = _status(admin_cli, capsysbinary)
        assert code == 0 and "Already done" in out
        admin_cli.main(["drop-legacy", "--anon", "--sha", sha])
        assert "Already done" in capsysbinary.readouterr().out.decode()
    finally:
        get_settings.cache_clear()
    assert [c["id"] for c in b.get("/api/conversations").json()["conversations"]] == [did]


def test_retention_can_be_changed_from_the_panel(hx, monkeypatch, capsysbinary):
    from app import admin_cli
    from app.config import get_settings

    a, b = hx.user(), hx.user()
    cid = legacy_anonymous(hx, a, b)
    r = hx.admin().put("/api/admin/settings", json={"changes": {"LEGACY_ANON_RETENTION_DAYS": 0}})
    assert r.status_code == 200, r.text
    conv = b.get(f"/api/conversations/{cid}").json()["conversation"]
    assert conv["deleted_after"] == _closed_at(hx).isoformat(timespec="milliseconds") + "Z"
    _cli_env(hx, monkeypatch)
    try:
        code, _out = _status(admin_cli, capsysbinary)
        assert code == 1  # due now: the CLI reads the panel value too
    finally:
        get_settings.cache_clear()


def test_a_new_install_has_no_old_anonymous_chats(hx, monkeypatch, capsysbinary):
    from app import admin_cli
    from app.config import get_settings

    hx.user()
    _cli_env(hx, monkeypatch)
    try:
        code, out = _status(admin_cli, capsysbinary)
        assert code == 0 and "Nothing to remove" in out
    finally:
        get_settings.cache_clear()
