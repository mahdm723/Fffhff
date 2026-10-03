"""The core MVP scenario and messaging rules."""

from __future__ import annotations

import json

from sqlalchemy import select

from app.models import Conversation, Message, User
from tests.conftest import reply, send


def _internal_ids(hx) -> set[str]:
    with hx.db() as db:
        users = db.scalars(select(User)).all()
        return {u.id for u in users} | {u.email for u in users}


def test_full_scenario_register_send_receive_reply(hx):
    a = hx.user("a@example.com")
    b = hx.user("b@example.com")

    # A writes from Home; the server picks B (the only other person).
    r = send(a, "أحتاج أن أتحدث مع شخص اليوم.")
    assert r.status_code == 201, r.text
    conv_a = r.json()["conversation"]
    cid = conv_a["id"]
    assert conv_a["peer"] == "dzplay" and conv_a["started_by_me"] is True

    # B sees a new conversation from "dzplay" with the message.
    convs_b = b.get("/api/conversations").json()["conversations"]
    assert [c["id"] for c in convs_b] == [cid]
    assert convs_b[0]["unread"] == 1 and convs_b[0]["last_message"]["preview"] == "أحتاج أن أتحدث مع شخص اليوم."
    detail = b.get(f"/api/conversations/{cid}").json()
    assert [m["content"] for m in detail["messages"]] == ["أحتاج أن أتحدث مع شخص اليوم."]
    assert detail["messages"][0]["author"] == "dzplay" and detail["messages"][0]["mine"] is False

    # B reads and replies.
    assert b.post(f"/api/conversations/{cid}/read").json() == {"read": 1}
    r = reply(b, cid, "أنا هنا، ماذا حدث؟")
    assert r.status_code == 201

    # A receives the reply in the same conversation and can answer again.
    msgs = a.get(f"/api/conversations/{cid}").json()["messages"]
    assert [(m["mine"], m["content"]) for m in msgs] == [
        (True, "أحتاج أن أتحدث مع شخص اليوم."),
        (False, "أنا هنا، ماذا حدث؟"),
    ]
    assert msgs[0]["status"] == "read"
    assert reply(a, cid, "شكرًا لأنك هنا").status_code == 201
    assert len(b.get(f"/api/conversations/{cid}").json()["messages"]) == 3

    # Nothing identifying ever reaches the clients.
    secrets_ = _internal_ids(hx)
    for client in (a, b):
        payload = json.dumps([client.get("/api/conversations").json(), client.get(f"/api/conversations/{cid}").json(),
                              client.get("/api/sync").json(), client.get("/api/profile").json()])
        assert not any(s in payload for s in secrets_)

    # Profile statistics.
    assert a.get("/api/profile").json()["stats"] == {"messages_sent": 2, "messages_received": 1, "conversations": 1}
    assert b.get("/api/profile").json()["stats"] == {"messages_sent": 1, "messages_received": 2, "conversations": 1}


def test_no_recipient_when_alone(hx):
    a = hx.user()
    r = send(a)
    assert r.status_code == 409 and r.json()["error"]["code"] == "no_recipient"


def test_never_matched_with_self_and_random_distribution(make_harness):
    hx = make_harness(MAX_NEW_CONVERSATIONS_PER_HOUR=100, MAX_NEW_CONVERSATIONS_PER_DAY=100, MAX_MESSAGES_PER_MINUTE=100,
                      MATCH_EXCLUDE_RECENT_PARTNERS=0, MATCH_MAX_INBOUND_NEW_PER_DAY=100)
    sender = hx.user()
    for _ in range(4):
        hx.user()
    for i in range(4):
        assert send(sender, f"رسالة رقم {i}").status_code == 201
    # "no_open_conversation": everyone already has a live conversation with the sender.
    assert send(sender, "رسالة خامسة").json()["error"]["code"] == "no_recipient"
    with hx.db() as db:
        convs = db.scalars(select(Conversation)).all()
        recipients = [c.recipient_id for c in convs]
        sender_id = convs[0].initiator_id
    assert sender_id not in recipients
    assert len(set(recipients)) == 4  # each live conversation is with a different person


def test_open_conversation_rule_exhausts_partners(hx):
    a = hx.user()
    hx.user()
    assert send(a, "أول رسالة").status_code == 201
    assert send(a, "رسالة ثانية").json()["error"]["code"] == "no_recipient"


def test_recent_partner_and_inbound_capacity_rules(make_harness):
    hx = make_harness(MATCHING_RULES="not_recent_partner,inbound_capacity", MATCH_EXCLUDE_RECENT_PARTNERS=1,
                      MATCH_MAX_INBOUND_NEW_PER_DAY=2, MAX_NEW_CONVERSATIONS_PER_HOUR=50)
    a, b, c = hx.user(), hx.user(), hx.user()
    first = send(a, "مرحبا 1").json()["conversation"]["id"]
    second = send(a, "مرحبا 2").json()["conversation"]["id"]
    with hx.db() as db:
        r1 = db.get(Conversation, first).recipient_id
        r2 = db.get(Conversation, second).recipient_id
    assert r1 != r2  # last partner excluded
    # Fill b's and c's daily inbound capacity (2 each) then nobody is available.
    send(b, "من b")  # b -> someone
    send(c, "من c")
    for i in range(6):
        send(a, f"المزيد {i}")
    with hx.db() as db:
        counts = {}
        for conv in db.scalars(select(Conversation)):
            counts[conv.recipient_id] = counts.get(conv.recipient_id, 0) + 1
    assert max(counts.values()) <= 2


def test_suspended_and_banned_users_not_matched(hx):
    a = hx.user()
    b = hx.user("banned@example.com")
    with hx.db() as db:
        db.scalar(select(User).where(User.email == "banned@example.com")).status = "banned"
    assert send(a).json()["error"]["code"] == "no_recipient"
    assert b.get("/api/me").status_code == 401  # banned sessions stop working


def test_conversation_access_is_private(hx):
    a, b = hx.user(), hx.user()
    outsider = hx.user()
    with hx.db() as db:  # force A -> B
        db.scalar(select(User).where(User.email == outsider.email)).status = "suspended"
    cid = send(a).json()["conversation"]["id"]
    for path in (f"/api/conversations/{cid}",):
        assert outsider.get(path).status_code == 404
    assert reply(outsider, cid, "تطفل").status_code in (403, 404)
    assert outsider.post(f"/api/conversations/{cid}/read").status_code == 404
    assert outsider.post(f"/api/conversations/{cid}/block").status_code == 404
    assert hx.client().get(f"/api/conversations/{cid}").status_code == 401
    assert b.get("/api/conversations/does-not-exist").status_code == 404


def test_suspended_user_cannot_send(hx):
    a = hx.user("s@example.com")
    hx.user()
    with hx.db() as db:
        db.scalar(select(User).where(User.email == "s@example.com")).status = "suspended"
    assert send(a).json()["error"]["code"] == "account_suspended"


def test_content_validation(hx):
    a = hx.user()
    hx.user()
    cases = {
        "": "empty_message",
        "   \n  ": "empty_message",
        "<script>alert(1)</script>": "html_not_allowed",
        "<b>hi</b>": "html_not_allowed",
        "زر موقعي https://evil.example/x": "links_not_allowed",
        "javascript:alert(1)": "links_not_allowed",
        "تواصل معي على t.me/someone": "links_not_allowed",
        "www.example.com": "links_not_allowed",
        "x" * 1001: "message_too_long",
    }
    for content, code in cases.items():
        r = send(a, content)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (content, r.text)
    # Plain text with symbols is fine: "<3" and "a < b" are not HTML.
    assert send(a, "أحبك <3 و 2 < 5").status_code == 201


def test_content_is_normalised(hx):
    a, b = hx.user(), hx.user()
    r = send(a, "  سطر‮ مخفي​ \r\n\n\n\nسطر آخر\x07  ")
    assert r.status_code == 201
    assert r.json()["message"]["content"] == "سطر مخفي\n\nسطر آخر"


def test_idempotent_retry_with_client_id(hx):
    a, b = hx.user(), hx.user()
    r1 = send(a, "مرة واحدة فقط", client_id="client-abc-123")
    r2 = send(a, "مرة واحدة فقط", client_id="client-abc-123")
    assert r1.status_code == r2.status_code == 201
    assert r1.json()["message"]["id"] == r2.json()["message"]["id"]
    cid = r1.json()["conversation"]["id"]
    m1 = reply(b, cid, "رد", client_id="reply-xyz-789").json()["message"]["id"]
    m2 = reply(b, cid, "رد", client_id="reply-xyz-789").json()["message"]["id"]
    assert m1 == m2
    with hx.db() as db:
        assert len(db.scalars(select(Message)).all()) == 2


def test_delivery_and_read_status(hx):
    a, b = hx.user(), hx.user()
    cid = send(a).json()["conversation"]["id"]
    status = lambda: a.get(f"/api/conversations/{cid}").json()["messages"][0]["status"]  # noqa: E731
    assert status() == "sent"
    b.get("/api/conversations")  # B's device fetched it
    assert status() == "delivered"
    b.post(f"/api/conversations/{cid}/read")
    assert status() == "read"
    assert a.get("/api/conversations").json()["conversations"][0]["peer_read_at"] is not None


def test_sync_since_returns_only_new_messages(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "الأولى").json()["conversation"]["id"]
    first = b.get("/api/sync").json()
    assert first["messages"] == [] and len(first["conversations"]) == 1  # initial sync: list only
    since = first["server_time"]
    reply(a, cid, "الثانية")
    nxt = b.get("/api/sync", params={"since": since}).json()
    assert [m["content"] for m in nxt["messages"]] == ["الثانية"]
    assert nxt["conversations"][0]["unread"] == 2


def test_hide_conversation_and_reappear_on_new_message(hx):
    a, b = hx.user(), hx.user()
    cid = send(a).json()["conversation"]["id"]
    assert b.delete(f"/api/conversations/{cid}").status_code == 200
    assert b.get("/api/conversations").json()["conversations"] == []
    reply(a, cid, "هل ما زلت هنا؟")
    assert [c["id"] for c in b.get("/api/conversations").json()["conversations"]] == [cid]


def test_both_hide_deletes_conversation(hx):
    a, b = hx.user(), hx.user()
    cid = send(a).json()["conversation"]["id"]
    a.delete(f"/api/conversations/{cid}")
    b.delete(f"/api/conversations/{cid}")
    with hx.db() as db:
        assert db.get(Conversation, cid) is None
        assert db.scalars(select(Message)).all() == []


def test_pagination_before_cursor(hx):
    a, b = hx.user(), hx.user()
    hx.settings.MAX_CONSECUTIVE_MESSAGES = 100
    hx.settings.MAX_MESSAGES_PER_MINUTE = 100
    cid = send(a, "0").json()["conversation"]["id"]
    from app import clock
    for i in range(1, 8):
        clock.advance(1)
        reply(a, cid, str(i))
    page1 = b.get(f"/api/conversations/{cid}", params={"limit": 5}).json()
    assert [m["content"] for m in page1["messages"]] == ["3", "4", "5", "6", "7"] and page1["has_more"]
    page2 = b.get(f"/api/conversations/{cid}", params={"limit": 5, "before": page1["messages"][0]["created_at"]}).json()
    assert [m["content"] for m in page2["messages"]] == ["0", "1", "2"] and not page2["has_more"]
