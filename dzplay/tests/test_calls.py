"""V4 calls: TURN credentials, who may call whom, the state machine over REST + WebSocket,
relay-only signaling, busy / missed / cooldown / per-hour cap, block and suspension
ending a live call, the dead-call watchdog, quality metadata, reports and the admin log."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

from sqlalchemy import select, text

from app import clock
from app.models import Call, Message, User
from app.services import admin as admin_service
from app.services import calls
from app.services.messaging import Effects
from tests.conftest import reply, send

ORIGIN = {"Origin": "http://testserver"}
HOST_CAND = "candidate:1 1 udp 2122260223 192.168.1.20 54321 typ host generation 0"
# raddr = the caller's own public address as seen by TURN: must never reach the other side
RELAY_CAND = "candidate:3 1 udp 41885439 203.0.113.7 49170 typ relay raddr 198.51.100.23 rport 51111 generation 0"
RELAY_SCRUBBED = RELAY_CAND.replace("raddr 198.51.100.23 rport 51111", "raddr 0.0.0.0 rport 0")


def me(c) -> dict:
    return c.get("/api/me").json()


def pair(hx):
    """Two people in an anonymous chat where both have written (so both may call)."""
    a, b = hx.user(), hx.user()
    cid = send(a, "مرحبًا").json()["conversation"]["id"]
    assert reply(b, cid, "أهلًا").status_code == 201
    return a, b, cid


def uid(hx, c) -> str:
    pid = me(c)["public_id"]
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.public_id == pid))


def start(c, cid, kind="audio"):
    return c.post("/api/calls", json={"conversation_id": cid, "kind": kind})


# ----------------------------------------------------------------- TURN


def test_ice_credentials_are_temporary_hmac_and_relay_only(hx):
    a = hx.user()
    ice = a.get("/api/calls/ice").json()
    srv = ice["ice_servers"][0]
    assert ice["ice_transport_policy"] == "relay"
    assert srv["urls"] == ["turn:turn.example.test:3478?transport=udp", "turn:turn.example.test:3478?transport=tcp",
                           "turns:turn.example.test:5349?transport=tcp"]
    expiry, ref = srv["username"].split(":")
    assert abs(int(expiry) - (clock.timestamp() + hx.settings.TURN_TTL)) < 5
    assert uid(hx, a) not in srv["username"] and len(ref) == 16  # no internal id in the TURN username
    expected = base64.b64encode(hmac.new(b"test-turn-secret", srv["username"].encode(), hashlib.sha1).digest()).decode()
    assert srv["credential"] == expected
    assert hx.client().get("/api/calls/ice").status_code == 401


def test_calls_disabled_without_turn(make_harness):
    hx = make_harness(TURN_SECRET="")
    a, b, cid = pair(hx)
    assert a.get("/api/config").json()["calls_enabled"] is False
    assert a.get("/api/calls/ice").status_code == 503
    assert start(a, cid).json()["error"]["code"] == "calls_unavailable"


# ----------------------------------------------------------------- who may call


def test_eligibility_rules(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "مرحبًا").json()["conversation"]["id"]
    r = start(a, cid)
    assert r.status_code == 403 and r.json()["error"]["code"] == "call_not_allowed"  # b never replied
    reply(b, cid, "أهلًا")
    assert start(a, cid, kind="screen").status_code == 400
    stranger = hx.user()
    assert start(stranger, cid).status_code == 404
    # callee turned calls off
    b.patch("/api/me/privacy", json={"accept_calls": False})
    assert start(a, cid).json()["error"]["code"] == "calls_closed"
    b.patch("/api/me/privacy", json={"accept_calls": True})
    # callee never confirmed 18+: refused without saying why
    ua, ub = uid(hx, a), uid(hx, b)
    with hx.db() as db:
        db.execute(text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": ub})
    assert start(a, cid).json()["error"]["code"] == "call_unavailable"
    # caller never confirmed 18+
    with hx.db() as db:
        db.get(User, ub).age_confirmed_at = clock.utcnow()
        db.execute(text("UPDATE users SET age_confirmed_at = NULL WHERE id = :u"), {"u": ua})
    assert start(a, cid).json()["error"]["code"] == "age_required"


def test_direct_request_must_be_accepted_before_calls(hx):
    a, b = hx.user(), hx.user()
    a.patch("/api/me/profile", json={"display_name": "Amine"})
    cid = a.post(f"/api/people/{me(b)['public_id']}/messages", json={"content": "سلام"}).json()["conversation"]["id"]
    assert start(a, cid).json()["error"]["code"] == "call_not_allowed"
    assert start(b, cid).json()["error"]["code"] == "call_not_allowed"  # request still pending
    reply(b, cid, "وعليكم السلام")  # replying accepts
    assert start(a, cid).status_code == 201


# ----------------------------------------------------------------- full flow over WebSocket


def test_full_call_flow_over_websocket_relay_only(hx):
    a, b, cid = pair(hx)
    with a.websocket_connect("/api/ws", headers=ORIGIN) as wa, b.websocket_connect("/api/ws", headers=ORIGIN) as wb:
        assert wa.receive_json()["type"] == "hello" and wb.receive_json()["type"] == "hello"
        r = start(a, cid, "video")
        assert r.status_code == 201
        call = r.json()["call"]
        assert call["state"] == "calling" and call["outgoing"] is True and call["peer"]["name"] == "dzplay"
        inc = wb.receive_json()
        assert inc["type"] == "call.incoming" and inc["call"]["id"] == call["id"]
        assert inc["call"]["peer"] == {"name": "dzplay", "public_id": None, "gender": None, "anonymous": True}
        assert inc["call"]["kind"] == "video" and inc["call"]["outgoing"] is False

        wb.send_json({"type": "call.ringing", "call_id": call["id"]})
        assert wa.receive_json()["state"] == "ringing"
        # signaling before the call is answered is ignored
        wa.send_json({"type": "call.sdp", "call_id": call["id"], "sdp": {"type": "offer", "sdp": "v=0"}})
        wb.send_json({"type": "call.accept", "call_id": call["id"], "device": "tab-1"})
        assert wa.receive_json()["state"] == "connected"
        ev = wb.receive_json()
        assert ev["state"] == "connected" and ev["device"] == "tab-1"  # other devices of b stop ringing

        offer = "v=0\r\na=candidate:" + HOST_CAND[10:] + "\r\na=candidate:" + RELAY_CAND[10:] + "\r\na=end"
        wa.send_json({"type": "call.sdp", "call_id": call["id"], "sdp": {"type": "offer", "sdp": offer}})
        got = wb.receive_json()
        assert got["type"] == "call.sdp" and "192.168.1.20" not in got["sdp"]["sdp"] and "typ relay" in got["sdp"]["sdp"]
        assert "198.51.100.23" not in got["sdp"]["sdp"] and "raddr 0.0.0.0 rport 0" in got["sdp"]["sdp"]
        wa.send_json({"type": "call.ice", "call_id": call["id"], "candidate": {"candidate": HOST_CAND, "sdpMid": "0", "sdpMLineIndex": 0}})
        wa.send_json({"type": "call.ice", "call_id": call["id"], "candidate": {"candidate": RELAY_CAND, "sdpMid": "0", "sdpMLineIndex": 0}})
        got = wb.receive_json()
        assert got["type"] == "call.ice" and got["candidate"]["candidate"] == RELAY_SCRUBBED  # host dropped, raddr scrubbed
        wb.send_json({"type": "call.media", "call_id": call["id"], "video": False})
        assert wa.receive_json() == {"type": "call.media", "call_id": call["id"], "video": False}
        wa.send_json({"type": "call.quality", "call_id": call["id"],
                      "stats": {"rtt_ms": 80, "loss_pct": 1.5, "kbps_out": 900, "relay": True, "codec_video": "H264", "height": 720}})
        clock.advance(65)
        wa.send_json({"type": "call.hangup", "call_id": call["id"]})
        end_a = wa.receive_json()
        while end_a["type"] != "call.state":
            end_a = wa.receive_json()
        assert end_a["state"] == "ended" and end_a["duration"] >= 65

    with hx.db() as db:
        c = db.get(Call, call["id"])
        assert c.state == "ended" and c.end_reason == "hangup" and c.video_used is True
        sysmsg = db.scalar(select(Message).where(Message.conversation_id == cid, Message.kind == "system"))
        meta = json.loads(sysmsg.meta)
        assert meta["event"] == "call" and meta["outcome"] == "ended" and meta["duration"] >= 65
        assert "1:0" in sysmsg.content  # "مكالمة فيديو · 1:05"
        q = calls.quality_summary(c)["caller"]
        assert q["rtt_ms"] == 80 and q["relay"] is True and q["codec_video"] == "H264" and q["height_max"] == 720


def test_strangers_cannot_signal_someone_elses_call(hx):
    a, b, cid = pair(hx)
    call_id = start(a, cid).json()["call"]["id"]
    stranger = hx.user()
    us = uid(hx, stranger)
    with hx.db() as db:
        intruder = db.get(User, us)
        effects = Effects()
        for t in ("call.accept", "call.hangup", "call.decline", "call.sdp"):
            calls.handle_signal(db, hx.settings, intruder, {"type": t, "call_id": call_id, "sdp": {"type": "offer", "sdp": "x"}}, effects)
        assert db.get(Call, call_id).state == "calling" and not effects.events
    assert stranger.post(f"/api/calls/{call_id}/accept", json={}).status_code == 404
    assert a.post(f"/api/calls/{call_id}/accept", json={}).status_code == 409  # the caller cannot answer their own call


# ----------------------------------------------------------------- busy, missed, anti-spam


def test_busy_and_one_call_at_a_time(hx):
    a, b, cid = pair(hx)
    c = hx.user()
    c.patch("/api/me/profile", json={"display_name": "Rami"})
    cid2 = c.post(f"/api/people/{me(b)['public_id']}/messages", json={"content": "مرحبا"}).json()["conversation"]["id"]
    reply(b, cid2, "أهلًا رامي")
    assert start(a, cid).status_code == 201
    assert start(a, cid).json()["error"]["code"] == "in_call"
    busy = start(c, cid2).json()["call"]
    assert busy["state"] == "busy"
    with hx.db() as db:
        meta = json.loads(db.scalar(select(Message.meta).where(Message.conversation_id == cid2, Message.kind == "system")))
        assert meta["outcome"] == "busy"


def test_ring_timeout_makes_a_missed_call(hx):
    a, b, cid = pair(hx)
    call_id = start(a, cid).json()["call"]["id"]
    with hx.db() as db:
        assert calls.tick(db, hx.settings, Effects()) == 0
    clock.advance(hx.settings.CALL_RING_TIMEOUT + 1)
    effects = Effects()
    with hx.db() as db:
        assert calls.tick(db, hx.settings, effects) == 1
        assert db.get(Call, call_id).state == "missed"
    assert any(ev.get("state") == "missed" for _, ev in effects.events)
    msgs = b.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["content"] for m in msgs if m["kind"] == "system"] == ["مكالمة صوتية فائتة"]
    assert b.get("/api/calls/active").json()["call"] is None


def test_unanswered_calls_cooldown_and_hourly_cap(make_harness):
    hx = make_harness(CALL_MAX_UNANSWERED=3, CALL_COOLDOWN=600)
    a, b, cid = pair(hx)
    for _ in range(3):
        call_id = start(a, cid).json()["call"]["id"]
        assert b.post(f"/api/calls/{call_id}/decline").json()["call"]["state"] == "declined"
    r = start(a, cid)
    assert r.status_code == 429 and r.json()["error"]["code"] == "call_cooldown"
    assert start(b, cid).status_code == 201  # only a → b is cooling down
    hx2 = make_harness(CALL_MAX_PER_HOUR=2, CALL_MAX_UNANSWERED=0)
    a, b, cid = pair(hx2)
    for _ in range(2):
        call_id = start(a, cid).json()["call"]["id"]
        a.post(f"/api/calls/{call_id}/hangup", json={})
    assert start(a, cid).status_code == 429


def test_cooldown_ends(make_harness):
    hx = make_harness(CALL_MAX_UNANSWERED=2, CALL_COOLDOWN=600)
    a, b, cid = pair(hx)
    for _ in range(2):
        a.post(f"/api/calls/{start(a, cid).json()['call']['id']}/hangup", json={})
    assert start(a, cid).status_code == 429
    clock.advance(601)
    assert start(a, cid).status_code == 201


# ----------------------------------------------------------------- block / suspend / watchdog


def connect(hx, a, b, cid) -> str:
    call_id = start(a, cid).json()["call"]["id"]
    assert b.post(f"/api/calls/{call_id}/accept", json={"device": "x"}).json()["call"]["state"] == "connected"
    return call_id


def test_block_during_a_call_ends_it(hx):
    a, b, cid = pair(hx)
    call_id = connect(hx, a, b, cid)
    assert b.post(f"/api/conversations/{cid}/block").status_code == 200
    with hx.db() as db:
        c = db.get(Call, call_id)
        assert c.state == "ended" and c.end_reason == "blocked"
    assert start(a, cid).status_code in (403, 404)


def test_suspension_ends_a_live_call(hx):
    a, b, cid = pair(hx)
    call_id = connect(hx, a, b, cid)
    effects = Effects()
    ua = uid(hx, a)
    with hx.db() as db:
        admin_service.set_user_status(db, ua, "suspended", effects, hx.settings)
    with hx.db() as db:
        assert db.get(Call, call_id).state == "ended" and db.get(Call, call_id).end_reason == "account_suspended"
    assert effects.events


def test_watchdog_closes_dead_connected_calls(hx):
    a, b, cid = pair(hx)
    call_id = connect(hx, a, b, cid)
    clock.advance(hx.settings.CALL_RECONNECT_TIMEOUT + 3 * hx.settings.CALL_QUALITY_REPORT_SECONDS + 11)
    with hx.db() as db:
        calls.tick(db, hx.settings, Effects())
        c = db.get(Call, call_id)
        assert c.state == "ended" and c.end_reason == "connection_lost"


def test_failed_hangup_and_caller_cancel(hx):
    a, b, cid = pair(hx)
    call_id = start(a, cid).json()["call"]["id"]
    assert a.post(f"/api/calls/{call_id}/hangup", json={}).json()["call"]["state"] == "canceled"
    msgs = b.get(f"/api/conversations/{cid}").json()["messages"]
    assert [m["content"] for m in msgs if m["kind"] == "system"][-1] == "مكالمة صوتية فائتة"
    call_id = connect(hx, a, b, cid)
    assert a.post(f"/api/calls/{call_id}/hangup", json={"reason": "failed"}).json()["call"]["state"] == "failed"


# ----------------------------------------------------------------- reports + admin


def test_report_a_call_and_admin_log(hx):
    a, b, cid = pair(hx)
    call_id = connect(hx, a, b, cid)
    r = b.post(f"/api/calls/{call_id}/report", json={"reason": "harassment", "details": "كلام مسيء"})
    assert r.status_code == 201 and r.json()["duplicate"] is False
    assert b.post(f"/api/calls/{call_id}/report", json={"reason": "harassment"}).json()["duplicate"] is True
    assert hx.user().post(f"/api/calls/{call_id}/report", json={"reason": "spam"}).status_code == 404
    with hx.db() as db:
        rep = [x for x in admin_service.list_reports(db) if x["call_id"] == call_id][0]
        assert rep["target"] == "call" and rep["evidence"][0]["call"]["state"] == "connected"
        log = calls.admin_log(db)
        assert log["calls"][0]["id"] == call_id and log["calls"][0]["reported"] is True
        assert "sdp" not in json.dumps(log)  # metadata only
        stats = calls.admin_stats(db)
        assert stats["total"] == 1 and stats["answered"] == 1


def test_call_log_is_deleted_after_retention(make_harness):
    from app.services.cleanup import run_cleanup

    hx = make_harness(CALL_LOG_RETENTION_DAYS=30)
    a, b, cid = pair(hx)
    call_id = start(a, cid).json()["call"]["id"]
    a.post(f"/api/calls/{call_id}/hangup", json={})
    clock.advance(31 * 86400)
    with hx.db() as db:
        assert run_cleanup(db, hx.settings)["calls"] == 1
        assert db.get(Call, call_id) is None
