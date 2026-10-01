"""Anti-spam limits, blocking, reporting, TTL cleanup, realtime, admin."""

from __future__ import annotations

import json

from sqlalchemy import select

from app import clock
from app.models import Block, Conversation, Message, Report, User
from tests.conftest import reply, send

# ----------------------------------------------------------------- limits


def test_messages_per_minute_limit(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_MINUTE=3, MAX_CONSECUTIVE_MESSAGES=50)
    a, b = hx.user(), hx.user()
    cid = send(a, "1").json()["conversation"]["id"]
    assert reply(a, cid, "2").status_code == 201
    assert reply(a, cid, "3").status_code == 201
    r = reply(a, cid, "4")
    assert r.status_code == 429 and r.headers["Retry-After"]
    clock.advance(61)
    assert reply(a, cid, "4").status_code == 201
    assert b  # B unaffected


def test_messages_per_hour_limit(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_HOUR=2, MAX_CONSECUTIVE_MESSAGES=50)
    a, _b = hx.user(), hx.user()
    cid = send(a, "1").json()["conversation"]["id"]
    assert reply(a, cid, "2").status_code == 201
    clock.advance(120)
    assert reply(a, cid, "3").status_code == 429
    clock.advance(3600)
    assert reply(a, cid, "3").status_code == 201


def test_new_conversations_per_hour_limit(make_harness):
    hx = make_harness(MAX_NEW_CONVERSATIONS_PER_HOUR=2)
    a = hx.user()
    for _ in range(4):
        hx.user()
    assert send(a, "أ").status_code == 201
    assert send(a, "ب").status_code == 201
    r = send(a, "ج")
    assert r.status_code == 429
    clock.advance(3601)
    assert send(a, "ج").status_code == 201


def test_new_conversations_per_day_limit(make_harness):
    hx = make_harness(MAX_NEW_CONVERSATIONS_PER_HOUR=10, MAX_NEW_CONVERSATIONS_PER_DAY=2)
    a = hx.user()
    for _ in range(4):
        hx.user()
    send(a, "أ")
    send(a, "ب")
    assert send(a, "ج").status_code == 429


def test_duplicate_anonymous_message_rejected(make_harness):
    hx = make_harness(MATCHING_RULES="inbound_capacity")
    a = hx.user()
    hx.user()
    hx.user()
    assert send(a, "انضموا إلى قناتي").status_code == 201
    r = send(a, "  انضموا   إلى قناتي ")
    assert r.status_code == 429 and "مؤخرًا" in r.json()["error"]["message"]


def test_consecutive_messages_limit(make_harness):
    hx = make_harness(MAX_CONSECUTIVE_MESSAGES=3)
    a, b = hx.user(), hx.user()
    cid = send(a, "1").json()["conversation"]["id"]
    reply(a, cid, "2")
    reply(a, cid, "3")
    r = reply(a, cid, "4")
    assert r.status_code == 429 and r.json()["error"]["code"] == "wait_for_reply"
    assert reply(b, cid, "رد").status_code == 201
    assert reply(a, cid, "4").status_code == 201


def test_rejected_request_does_not_consume_quota(make_harness):
    hx = make_harness(MAX_MESSAGES_PER_MINUTE=2, MAX_CONSECUTIVE_MESSAGES=50)
    a, _b = hx.user(), hx.user()
    cid = send(a, "1").json()["conversation"]["id"]
    for _ in range(3):
        assert reply(a, cid, "<b>x</b>").status_code == 400  # invalid, never counted
    assert reply(a, cid, "2").status_code == 201


# ----------------------------------------------------------------- blocking


def test_block_stops_messages_and_matching(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "مرحبا").json()["conversation"]["id"]
    assert b.post(f"/api/conversations/{cid}/block").status_code == 200

    # The blocker no longer sees it; the blocked side sees a closed conversation.
    assert b.get("/api/conversations").json()["conversations"] == []
    conv_a = a.get("/api/conversations").json()["conversations"][0]
    assert conv_a["status"] == "closed" and conv_a["can_reply"] is False
    r = reply(a, cid, "لماذا؟")
    assert r.status_code == 403 and r.json()["error"]["code"] == "conversation_closed"
    assert reply(b, cid, "x").status_code == 404

    # They are never matched together again, in either direction.
    clock.advance(10)
    assert send(a, "رسالة جديدة").json()["error"]["code"] == "no_recipient"
    assert send(b, "رسالة أخرى").json()["error"]["code"] == "no_recipient"


def test_block_list_and_unblock(make_harness):
    hx = make_harness(MATCHING_RULES="")  # only the mandatory rules: isolate the block rule
    a, b = hx.user(), hx.user()
    cid = send(a).json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/block")
    blocks = b.get("/api/blocks").json()["blocks"]
    assert len(blocks) == 1 and blocks[0]["peer"] == "dzplay"
    assert set(blocks[0]) == {"id", "peer", "created_at"}  # no identity leaked
    assert a.delete(f"/api/blocks/{blocks[0]['id']}").status_code == 404  # not A's block
    assert b.delete(f"/api/blocks/{blocks[0]['id']}").status_code == 200
    assert b.get("/api/blocks").json()["blocks"] == []
    assert send(b, "عدنا").status_code == 201  # matchable again


def test_block_survives_conversation_expiry(hx):
    a, b = hx.user(), hx.user()
    cid = send(a).json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/block")
    clock.advance(hx.settings.CONVERSATION_IDLE_TTL + 10)
    with hx.db() as db:
        from app.services.cleanup import run_cleanup
        run_cleanup(db, hx.settings)
    with hx.db() as db:
        assert db.get(Conversation, cid) is None
        assert db.scalar(select(Block)) is not None
    assert send(a, "مرة أخرى").json()["error"]["code"] == "no_recipient"


# ----------------------------------------------------------------- reporting


def test_report_message_keeps_minimal_evidence(hx):
    a, b = hx.user(), hx.user()
    r = send(a, "رسالة مسيئة")
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
    cid = send(a, "أولى").json()["conversation"]["id"]
    reply(a, cid, "ثانية")
    reply(b, cid, "ردي")
    assert b.post(f"/api/conversations/{cid}/report", json={"reason": "threat"}).status_code == 201
    with hx.db() as db:
        snapshot = json.loads(db.scalar(select(Report)).snapshot)
    assert [s["content"] for s in snapshot] == ["أولى", "ثانية"]  # only the reported side's messages


def test_auto_suspend_after_many_distinct_reporters(make_harness):
    hx = make_harness(REPORT_AUTO_SUSPEND_THRESHOLD=2, MATCHING_RULES="", MAX_NEW_CONVERSATIONS_PER_HOUR=50)
    spammer = hx.user("spam@example.com")
    victims = [hx.user(), hx.user(), hx.user()]
    reported = 0
    for i in range(12):
        r = send(spammer, f"إعلان {i}")
        if r.status_code != 201:
            continue
        cid = r.json()["conversation"]["id"]
        for v in victims:
            if any(c["id"] == cid for c in v.get("/api/conversations").json()["conversations"]):
                if v.post(f"/api/conversations/{cid}/report", json={"reason": "spam"}).status_code == 201:
                    reported += 1
                break
        with hx.db() as db:
            if db.scalar(select(User.status).where(User.email == "spam@example.com")) == "suspended":
                break
    with hx.db() as db:
        assert db.scalar(select(User.status).where(User.email == "spam@example.com")) == "suspended"
    assert send(spammer, "المزيد").json()["error"]["code"] == "account_suspended"


# ----------------------------------------------------------------- TTL


def test_read_messages_expire_sooner_and_cleanup_purges(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "سر").json()["conversation"]["id"]
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
    cid = send(a, "لم تُقرأ بعد").json()["conversation"]["id"]
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
    cid = send(a).json()["conversation"]["id"]
    clock.advance(hx.settings.CONVERSATION_IDLE_TTL + 1)
    assert b.get("/api/conversations").json()["conversations"] == []
    assert b.get(f"/api/conversations/{cid}").status_code == 404
    from app.services.cleanup import run_cleanup
    with hx.db() as db:
        counts = run_cleanup(db, hx.settings)
    assert counts["conversations"] == 1
    # A and B can be matched again later.
    assert send(a, "من جديد").status_code == 201


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
        send(a, "هل يصلك هذا؟")
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


def test_admin_api_requires_token_and_hides_content(make_harness):
    hx = make_harness(ADMIN_API_TOKEN="admin-secret-token")
    a, b = hx.user(), hx.user()
    r = send(a, "محتوى خاص جدًا")
    cid = r.json()["conversation"]["id"]
    c = hx.client()
    assert c.get("/api/admin/stats").status_code == 401
    assert c.get("/api/admin/stats", headers={"Authorization": "Bearer wrong"}).status_code == 401
    auth = {"Authorization": "Bearer admin-secret-token"}
    stats = c.get("/api/admin/stats", headers=auth)
    assert stats.status_code == 200
    data = stats.json()
    assert data["users"]["total"] == 2 and data["messages"]["stored_now"] == 1
    assert "محتوى خاص" not in stats.text and "@example.com" not in stats.text

    # Content becomes visible only through an explicit report.
    b.post(f"/api/conversations/{cid}/report", json={"reason": "spam"})
    reports = c.get("/api/admin/reports", headers=auth).json()["reports"]
    assert reports[0]["evidence"][0]["content"] == "محتوى خاص جدًا"
    assert "@" not in json.dumps(reports)
    res = c.post(f"/api/admin/reports/{reports[0]['id']}/resolve", json={"action": "ban"}, headers=auth)
    assert res.json()["resolution"] == "ban"
    assert a.get("/api/me").status_code == 401  # banned + sessions revoked


def test_admin_api_disabled_without_token(hx):
    assert hx.client().get("/api/admin/stats", headers={"Authorization": "Bearer "}).status_code == 404


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


def test_oversized_body_rejected(hx):
    c = hx.user()
    r = c.post("/api/messages", content=b'{"content": "' + b"a" * 70000 + b'"}', headers={"Content-Type": "application/json"})
    assert r.status_code == 413


def test_challenge_endpoint_rate_limited(hx):
    c = hx.client(ip="203.0.113.99")
    codes = [c.post("/api/auth/challenge", json={"purpose": "login"}).status_code for _ in range(32)]
    assert codes[:30] == [200] * 30 and codes[-1] == 429
