"""Anti-spam limits, blocking, reporting, TTL cleanup, realtime, admin."""

from __future__ import annotations

import json

from sqlalchemy import select

from app import clock
from app.models import Block, Conversation, Message, Report, User
from tests.conftest import ADMIN_PATH, chat, reply, send

# ----------------------------------------------------------------- limits


def test_messages_per_minute_limit(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_MINUTE=3, MAX_CONSECUTIVE_MESSAGES=50)
    a, b = hx.user(), hx.user()
    cid = chat(a, b, "1")
    assert reply(a, cid, "2").status_code == 201
    assert reply(a, cid, "3").status_code == 201
    r = reply(a, cid, "4")
    assert r.status_code == 429 and r.headers["Retry-After"]
    clock.advance(61)
    assert reply(a, cid, "4").status_code == 201
    assert b  # B unaffected


def test_messages_per_hour_limit(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_HOUR=2, MAX_CONSECUTIVE_MESSAGES=50)
    a, b = hx.user(), hx.user()
    cid = chat(a, b, "1")
    assert reply(a, cid, "2").status_code == 201
    clock.advance(120)
    assert reply(a, cid, "3").status_code == 429
    clock.advance(3600)
    assert reply(a, cid, "3").status_code == 201


def test_new_direct_conversations_per_day_limit(make_harness):
    hx = make_harness(DIRECT_NEW_PER_DAY=2)
    a = hx.user()
    others = [hx.user() for _ in range(3)]
    assert send(a, others[0], "أ").status_code == 201
    assert send(a, others[1], "ب").status_code == 201
    assert send(a, others[2], "ج").status_code == 429
    clock.advance(86401)
    assert send(a, others[2], "ج").status_code == 201


def test_consecutive_messages_limit(make_harness):
    hx = make_harness(MAX_CONSECUTIVE_MESSAGES=3)
    a, b = hx.user(), hx.user()
    cid = chat(a, b, "1")
    reply(a, cid, "2")
    reply(a, cid, "3")
    r = reply(a, cid, "4")
    assert r.status_code == 429 and r.json()["error"]["code"] == "wait_for_reply"
    assert reply(b, cid, "رد").status_code == 201
    assert reply(a, cid, "4").status_code == 201


def test_rejected_request_does_not_consume_quota(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_MINUTE=2, MAX_CONSECUTIVE_MESSAGES=50)
    a, b = hx.user(), hx.user()
    cid = chat(a, b, "1")
    for _ in range(3):
        assert reply(a, cid, "<b>x</b>").status_code == 400  # invalid, never counted
    assert reply(a, cid, "2").status_code == 201


# ----------------------------------------------------------------- blocking


def test_block_stops_messages_and_new_chats(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "مرحبا").json()["conversation"]["id"]
    assert b.post(f"/api/conversations/{cid}/block").status_code == 200

    # The blocker no longer sees it; the blocked side sees a closed conversation.
    assert b.get("/api/conversations").json()["conversations"] == []
    conv_a = a.get("/api/conversations").json()["conversations"][0]
    assert conv_a["status"] == "closed" and conv_a["can_reply"] is False
    r = reply(a, cid, "لماذا؟")
    assert r.status_code == 403 and r.json()["error"]["code"] == "conversation_closed"
    assert reply(b, cid, "x").status_code == 404

    # Neither can start a new chat with the other (indistinguishable from an unknown ID).
    clock.advance(10)
    assert send(a, b, "رسالة جديدة").status_code == 404
    assert send(b, a, "رسالة أخرى").status_code == 404


def test_block_list_and_unblock(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    cid = send(a, b).json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/block")
    blocks = b.get("/api/blocks").json()["blocks"]
    assert len(blocks) == 1 and blocks[0]["peer"] == "dzplay"
    assert set(blocks[0]) == {"id", "peer", "created_at"}  # no identity leaked
    assert a.delete(f"/api/blocks/{blocks[0]['id']}").status_code == 404  # not A's block
    assert b.delete(f"/api/blocks/{blocks[0]['id']}").status_code == 200
    assert b.get("/api/blocks").json()["blocks"] == []
    assert b.get(f"/api/people/{a.get('/api/me').json()['public_id']}").status_code == 200  # findable again


def test_block_survives_conversation_expiry(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b).json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/block")
    clock.advance(hx.settings.CONVERSATION_IDLE_TTL + 10)
    with hx.db() as db:
        from app.services.cleanup import run_cleanup
        run_cleanup(db, hx.settings)
    with hx.db() as db:
        assert db.get(Conversation, cid) is None
        assert db.scalar(select(Block)) is not None
    assert send(a, b, "مرة أخرى").status_code == 404


# ----------------------------------------------------------------- reporting


def test_report_message_keeps_minimal_evidence(hx):
    a, b = hx.user(), hx.user()
    r = send(a, b, "رسالة مسيئة")
    cid, mid = r.json()["conversation"]["id"], r.json()["message"]["id"]
    assert a.post(f"/api/messages/{mid}/report", json={"reason": "spam"}).status_code == 400  # own message
    r = b.post(f"/api/messages/{mid}/report", json={"reason": "harassment", "details": "يزعجني"})
    assert r.status_code == 201 and r.json()["duplicate"] is False
    assert b.post(f"/api/messages/{mid}/report", json={"reason": "harassment"}).json()["duplicate"] is True
    assert b.post(f"/api/messages/{mid}/report", json={"reason": "nonsense"}).status_code == 400

    # Evidence survives the message TTL.
    clock.advance(hx.settings.MESSAGE_TTL + 10)
    with hx.db() as db:
        from app.services.cleanup import run_cleanup
        run_cleanup(db, hx.settings)
    with hx.db() as db:
        assert db.get(Message, mid) is None
        rep = db.scalar(select(Report))
        assert json.loads(rep.snapshot)[0]["content"] == "رسالة مسيئة"
        assert rep.reason == "harassment" and rep.conversation_id == cid


def test_report_conversation(hx):
    a, b = hx.user(), hx.user()
    cid = chat(a, b, "أولى")
    reply(a, cid, "ثانية")
    reply(b, cid, "ردي")
    assert b.post(f"/api/conversations/{cid}/report", json={"reason": "threat"}).status_code == 201
    with hx.db() as db:
        snapshot = json.loads(db.scalar(select(Report)).snapshot)
    assert [s["content"] for s in snapshot] == ["أولى", "ثانية"]  # only the reported side's messages


def test_auto_suspend_after_many_distinct_reporters(make_harness):
    hx = make_harness(REPORT_AUTO_SUSPEND_THRESHOLD=2)
    spammer = hx.user("spam@example.com")
    victims = [hx.user(), hx.user(), hx.user()]
    for v in victims[:2]:
        cid = send(spammer, v, "إعلان مزعج").json()["conversation"]["id"]
        assert v.post(f"/api/conversations/{cid}/report", json={"reason": "spam"}).status_code == 201
    with hx.db() as db:
        assert db.scalar(select(User.status).where(User.email == "spam@example.com")) == "suspended"
    assert send(spammer, victims[2], "المزيد").json()["error"]["code"] == "account_suspended"


# ----------------------------------------------------------------- TTL


def test_read_messages_expire_sooner_and_cleanup_purges(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "سر").json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/read")
    with hx.db() as db:
        msg = db.scalar(select(Message))
        assert (msg.expires_at - msg.read_at).total_seconds() <= hx.settings.MESSAGE_TTL_AFTER_READ + 1

    clock.advance(hx.settings.MESSAGE_TTL_AFTER_READ + 5)
    # Expired messages are no longer served even before the cleanup job runs.
    assert b.get(f"/api/conversations/{cid}").json()["messages"] == []
    with hx.db() as db:
        from app.services.cleanup import run_cleanup
        counts = run_cleanup(db, hx.settings)
    assert counts["messages"] == 1
    # The conversation itself stays usable (not idle long enough) — replying still works.
    assert reply(b, cid, "ما زلت هنا").status_code == 201
    # Statistics are counters, they survive deletion.
    assert a.get("/api/profile").json()["stats"]["messages_sent"] == 1


def test_unread_message_kept_until_max_ttl(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "لم تُقرأ بعد").json()["conversation"]["id"]
    clock.advance(hx.settings.MESSAGE_TTL - 60)
    assert len(b.get(f"/api/conversations/{cid}").json()["messages"]) == 1
    clock.advance(120)
    from app.services.cleanup import run_cleanup
    with hx.db() as db:
        run_cleanup(db, hx.settings)
    with hx.db() as db:
        assert db.scalars(select(Message)).all() == []


def test_idle_conversation_expires(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b).json()["conversation"]["id"]
    clock.advance(hx.settings.CONVERSATION_IDLE_TTL + 1)
    assert b.get("/api/conversations").json()["conversations"] == []
    assert b.get(f"/api/conversations/{cid}").status_code == 404
    from app.services.cleanup import run_cleanup
    with hx.db() as db:
        counts = run_cleanup(db, hx.settings)
    assert counts["conversations"] == 1
    # A can write to B again later (a new request).
    assert send(a, b, "من جديد").status_code == 201


def test_cleanup_purges_auth_data(make_harness):
    hx = make_harness(SESSION_DURATION=60)
    hx.user("a@example.com")
    hx.login(hx.client(), "a@example.com", "wrong")
    clock.advance(hx.settings.LOGIN_FAILURE_WINDOW + hx.settings.POW_CHALLENGE_TTL + 120)
    from app.services.cleanup import run_cleanup
    with hx.db() as db:
        counts = run_cleanup(db, hx.settings)
    assert counts["sessions"] >= 1 and counts["challenges"] >= 1 and counts["auth_throttle"] >= 1


# ----------------------------------------------------------------- realtime


def test_websocket_signals_new_message(hx):
    a, b = hx.user(), hx.user()
    with b.websocket_connect("/api/ws", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json() == {"type": "hello"}
        send(a, b, "هل يصلك هذا؟")
        event = ws.receive_json()
        assert event == {"type": "sync", "reason": "message"}  # a signal only, no content
        sync = b.get("/api/sync").json()
        assert sync["conversations"][0]["last_message"]["preview"] == "هل يصلك هذا؟"


def test_websocket_requires_session_and_same_origin(hx):
    import pytest
    from starlette.websockets import WebSocketDisconnect

    b = hx.user()
    with pytest.raises(WebSocketDisconnect):
        with hx.client().websocket_connect("/api/ws", headers={"Origin": "http://testserver"}) as ws:
            ws.receive_json()
    with pytest.raises(WebSocketDisconnect):
        with b.websocket_connect("/api/ws", headers={"Origin": "https://evil.example"}) as ws:
            ws.receive_json()


# ----------------------------------------------------------------- admin


def test_admin_api_requires_admin_session_and_hides_content(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    r = send(a, b, "محتوى خاص جدًا")
    cid = r.json()["conversation"]["id"]
    anon = hx.client()
    assert anon.get(f"{ADMIN_PATH}/api/admin/stats").status_code == 401
    assert anon.get("/api/admin/stats").status_code == 404  # nothing at the old, guessable path
    c = hx.admin()
    stats = c.get("/api/admin/stats")
    assert stats.status_code == 200
    data = stats.json()
    assert data["users"]["total"] == 2 and data["messages"]["stored_now"] == 1
    assert "محتوى خاص" not in stats.text and "@example.com" not in stats.text

    # Content becomes visible only through an explicit report.
    b.post(f"/api/conversations/{cid}/report", json={"reason": "spam"})
    reports = c.get("/api/admin/reports").json()["reports"]
    assert reports[0]["evidence"][0]["content"] == "محتوى خاص جدًا"
    assert "@" not in json.dumps(reports)
    res = c.post(f"/api/admin/reports/{reports[0]['id']}/resolve", json={"action": "ban"})
    assert res.json()["resolution"] == "ban"
    assert a.get("/api/me").status_code == 401  # banned + sessions revoked


def test_admin_panel_disabled_without_admin_path(make_harness):
    hx = make_harness(ADMIN_PATH="")
    c = hx.client()
    assert c.get(f"{ADMIN_PATH}/api/admin/stats").status_code == 404
    assert c.get(ADMIN_PATH).status_code == 404


# ----------------------------------------------------------------- misc


def test_security_headers_and_static(hx):
    c = hx.client()
    r = c.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    csp = r.headers["content-security-policy"]
    assert "script-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe-inline" not in csp
    assert r.headers["x-content-type-options"] == "nosniff"
    js = c.get("/js/app.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert c.get("/api/me").headers["cache-control"] == "no-store"
    assert c.get("/api/nope").json()["error"]["code"] == "not_found"
    assert r.headers["cross-origin-resource-policy"] == "same-origin"
    assert r.headers["referrer-policy"] == "no-referrer" and r.headers["x-frame-options"] == "DENY"


def test_secrets_and_source_are_never_served(hx):
    """Only static/ is public: no .env, git data, source, docs or directory listings."""
    c = hx.client()
    for path in ("/.env", "/.env.example", "/.git/config", "/.git/HEAD", "/app/config.py", "/app/main.py", "/Dockerfile",
                 "/docker-compose.yml", "/Caddyfile", "/requirements.txt", "/deploy/install.sh", "/%2e%2e/.env",
                 "/..%2f.env", "/js/..%2f..%2f.env", "/static/../app/main.py", "/docs", "/redoc", "/openapi.json",
                 "/js/", "/css/", "/icons/", "/download/"):
        r = c.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
        assert "SECRET_KEY" not in r.text and "TELEGRAM" not in r.text


def test_cors_is_closed(hx):
    c = hx.user()
    r = c.options("/api/me", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in r.headers
    r = c.get("/api/me", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in r.headers
    # a cross-site write is refused even with the custom header (Origin check)
    r = c.post("/api/posts", json={"content": "x" * 20}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_oversized_body_rejected(hx):
    c = hx.user()
    r = c.post("/api/posts", content=b'{"content": "' + b"a" * 70000 + b'"}', headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_challenge_endpoint_rate_limited(hx):
    c = hx.client(ip="203.0.113.99")
    codes = [c.post("/api/auth/challenge", json={"purpose": "login"}).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and codes[-1] == 429


def test_android_asset_links(hx):
    r = hx.client().get("/.well-known/assetlinks.json")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
    target = r.json()[0]["target"]
    assert target["namespace"] == "android_app" and target["package_name"] == "io.dzplay.app"
    assert all(len(fp.split(":")) == 32 for fp in target["sha256_cert_fingerprints"])


def test_android_apk_download(hx, tmp_path, monkeypatch):
    import app.api.auth as auth_api
    from app import main as main_mod

    c = hx.client()
    apk_dir = main_mod.STATIC_DIR / "download"
    present = (apk_dir / "dzplay.apk").is_file()
    r = c.get("/download/dzplay.apk")
    assert r.status_code == (200 if present else 404)
    assert c.get("/api/config").json()["android_apk_url"] == ("/download/dzplay.apk" if present else None)
    if present:
        assert r.headers["content-type"] == "application/vnd.android.package-archive"
        assert 'filename="DZPLAY.apk"' in r.headers["content-disposition"]
    assert auth_api._APK == apk_dir / "dzplay.apk"


def test_push_endpoints_limited_to_browser_push_services():
    """Web Push subscriptions are URLs the server POSTs to: never internal hosts (SSRF)."""
    from app.services.push import push_endpoint_allowed
    from app.config import Settings

    s = Settings(SECRET_KEY="x")
    for ok in ("https://fcm.googleapis.com/fcm/send/abc", "https://updates.push.services.mozilla.com/wpush/v2/x",
               "https://wns2-par02p.notify.windows.com/w/?token=x", "https://web.push.apple.com/QGx",
               "https://fcm.googleapis.com:443/fcm/send/abc"):
        assert push_endpoint_allowed(s, ok), ok
    for bad in ("http://fcm.googleapis.com/fcm/send/abc", "https://127.0.0.1/x", "https://db:5432/x", "https://169.254.169.254/latest",
                "https://localhost/x", "https://fcm.googleapis.com.evil.example/x", "https://evilfcm.googleapis.com.attacker.io/",
                "https://user:pw@fcm.googleapis.com/x", "https://fcm.googleapis.com:8443/x", "https://[::1]/x", "file:///etc/passwd",
                "https://notfcm.googleapis.co/x", "https:///x", "https://fcm.googleapis.com:99999/x"):
        assert not push_endpoint_allowed(s, bad), bad
