"""V4: client → server WebSocket messages (typing indicator) and the socket guards
(size cap, binary frames, per-connection rate limit, authorization per conversation)."""

from __future__ import annotations

import pytest
from starlette.websockets import WebSocketDisconnect

from tests.conftest import reply, send

ORIGIN = {"Origin": "http://testserver"}


def pid(c) -> str:
    return c.get("/api/me").json()["public_id"]


def test_typing_is_relayed_only_to_the_other_member(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "مرحبًا").json()["conversation"]["id"]
    stranger = hx.user()  # created after matching so the message went to b
    with b.websocket_connect("/api/ws", headers=ORIGIN) as wb, a.websocket_connect("/api/ws", headers=ORIGIN) as wa, \
            stranger.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert wb.receive_json() == {"type": "hello"}
        assert wa.receive_json() == {"type": "hello"}
        assert ws.receive_json() == {"type": "hello"}
        # a stranger cannot inject typing into someone else's conversation
        ws.send_json({"type": "typing", "conversation_id": cid, "on": True})
        ws.send_json({"type": "typing", "conversation_id": "x" * 400, "on": True})  # garbage is ignored
        ws.send_json({"type": "typing", "conversation_id": cid, "on": "yes"})
        wa.send_json({"type": "typing", "conversation_id": cid, "on": True})
        assert wb.receive_json() == {"type": "typing", "conversation_id": cid, "on": True}
        wa.send_json({"type": "typing", "conversation_id": cid, "on": False})
        assert wb.receive_json() == {"type": "typing", "conversation_id": cid, "on": False}
        # the stranger's attempt produced nothing: the next event b gets is the real message
        reply(a, cid, "رسالة حقيقية")
        assert wb.receive_json() == {"type": "sync", "reason": "message"}
        assert wa.receive_json() == {"type": "sync", "reason": "sent"}


def test_typing_not_sent_for_an_unanswered_request(hx):
    a, b = hx.user(), hx.user()
    a.patch("/api/me/profile", json={"display_name": "Amine"})
    cid = a.post(f"/api/people/{pid(b)}/messages", json={"content": "سلام"}).json()["conversation"]["id"]
    with b.websocket_connect("/api/ws", headers=ORIGIN) as wb, a.websocket_connect("/api/ws", headers=ORIGIN) as wa:
        wb.receive_json(), wa.receive_json()
        wa.send_json({"type": "typing", "conversation_id": cid, "on": True})
        reply(a, cid, "ثانية")
        assert wb.receive_json() == {"type": "sync", "reason": "message"}  # no typing before that
        assert wa.receive_json() == {"type": "sync", "reason": "sent"}
        # the recipient may type back (that is how they answer)
        wb.send_json({"type": "typing", "conversation_id": cid, "on": True})
        assert wa.receive_json() == {"type": "typing", "conversation_id": cid, "on": True}


def test_oversized_binary_and_flooding_close_the_socket(make_harness):
    hx = make_harness(WS_MAX_MESSAGE_BYTES=300, WS_MSGS_PER_10S=5)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_text("{" + "x" * 400)
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1009
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        ws.send_bytes(b"\x00\x01")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1003
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        ws.receive_json()
        for _ in range(6):
            ws.send_text("not json")
        with pytest.raises(WebSocketDisconnect) as e:
            ws.receive_json()
        assert e.value.code == 1008


def test_presence_only_for_known_peers(hx):
    a, b = hx.user(), hx.user()
    a.patch("/api/me/profile", json={"display_name": "Amine"})
    cid = send(a, "مجهول").json()["conversation"]["id"]
    conv = b.get(f"/api/conversations/{cid}").json()["conversation"]
    assert conv["peer_card"]["active"] is False and conv["peer_card"]["public_id"] is None  # anonymous: nothing
    a.post(f"/api/conversations/{cid}/reveal")
    conv = b.get(f"/api/conversations/{cid}").json()["conversation"]
    assert conv["peer_card"]["active"] is True and conv["peer"] == "Amine"


def test_socket_closes_when_the_session_ends(make_harness):
    hx = make_harness(WS_PING_SECONDS=1)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "hello"}
        assert ws.receive_json() == {"type": "ping"}  # still valid
        assert a.post("/api/auth/logout").status_code in (200, 204)
        with pytest.raises(WebSocketDisconnect) as e:
            for _ in range(5):
                ws.receive_json()
        assert e.value.code == 4401
