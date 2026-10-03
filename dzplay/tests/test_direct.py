"""V4: people search, direct messages + message requests, privacy switches, anonymous chats
staying anonymous and the one-way "reveal my identity"."""

from __future__ import annotations

from sqlalchemy import func, select

from app.models import Conversation
from tests.conftest import reply, send


def pid(c) -> str:
    return c.get("/api/me").json()["public_id"]


def named(hx, name: str, **kw):
    c = hx.user(**kw)
    assert c.patch("/api/me/profile", json={"display_name": name}).status_code == 200
    return c


def dm(c, public_id: str, text: str = "مرحبا، هل نتحدث؟"):
    return c.post(f"/api/people/{public_id}/messages", json={"content": text})


# ----------------------------------------------------------------- search


def test_search_by_id_and_by_name_with_arabic_normalization(hx):
    me = hx.user()
    a = named(hx, "أحمد بن علي")
    named(hx, "فاطمة الزهراء")
    plain = hx.user()  # still "dzplay": only reachable by ID
    r = me.get("/api/people/search", params={"q": "احمد"}).json()
    assert r["by"] == "name" and [x["name"] for x in r["results"]] == ["أحمد بن علي"]
    assert me.get("/api/people/search", params={"q": "فاطمه"}).json()["results"][0]["name"] == "فاطمة الزهراء"
    assert me.get("/api/people/search", params={"q": "زهرا"}).json()["results"]  # partial
    # exact ID (case-insensitive), works for dzplay-named people too
    got = me.get("/api/people/search", params={"q": pid(plain).lower()}).json()
    assert got["by"] == "id" and got["results"][0]["public_id"] == pid(plain) and got["results"][0]["name"] == "dzplay"
    assert me.get("/api/people/search", params={"q": "dzplay"}).json()["results"] == []
    # nothing internal leaks
    res = me.get("/api/people/search", params={"q": "احمد"}).json()["results"][0]
    assert set(res) == {"name", "public_id", "gender", "profile_ref"} and pid(a) == res["public_id"]
    assert me.get("/api/people/search", params={"q": "a"}).status_code == 400  # 2 letters minimum


def test_search_hides_blocked_suspended_hidden_and_team(hx):
    me = hx.user()
    blocked = named(hx, "Samir One")
    hidden = named(hx, "Samir Two")
    suspended = named(hx, "Samir Three")
    named(hx, "Samir Four")
    hidden.patch("/api/me/privacy", json={"searchable_by_name": False})
    # I block "Samir One" through a conversation
    dm(blocked, pid(me))
    conv = me.get("/api/conversations").json()["conversations"][0]
    me.post(f"/api/conversations/{conv['id']}/block")
    with hx.db() as db:
        from app.models import User

        u = db.scalar(select(User).where(User.public_id == pid(suspended)))
        u.status = "suspended"
    names_found = [x["name"] for x in me.get("/api/people/search", params={"q": "samir"}).json()["results"]]
    assert names_found == ["Samir Four"]
    # hidden from name search, still found by exact ID
    assert me.get("/api/people/search", params={"q": pid(hidden)}).json()["results"][0]["name"] == "Samir Two"
    # blocked: not even by ID (both directions)
    assert me.get("/api/people/search", params={"q": pid(blocked)}).json()["results"] == []
    assert blocked.get("/api/people/search", params={"q": pid(me)}).json()["results"] == []


def test_search_is_rate_limited_and_paginated(make_harness):
    hx = make_harness(SEARCH_PER_MINUTE=3, SEARCH_MAX_RESULTS=2, SEARCH_MAX_PAGES=2)
    me = hx.user()
    for i in range(5):
        named(hx, f"Nadia {chr(65 + i)}x")
    first = me.get("/api/people/search", params={"q": "nadia"}).json()
    assert len(first["results"]) == 2 and first["has_more"] is True
    second = me.get("/api/people/search", params={"q": "nadia", "page": 1}).json()
    assert len(second["results"]) == 2 and second["has_more"] is False  # hard page cap
    assert me.get("/api/people/search", params={"q": "nadia"}).status_code == 200
    assert me.get("/api/people/search", params={"q": "nadia"}).status_code == 429
    assert me.get("/api/people/search", params={"q": "nadia", "page": 2}).status_code in (400, 429)


# ----------------------------------------------------------------- direct messages + requests


def test_direct_request_flow_and_limit_before_reply(hx):
    a, b = named(hx, "Amine"), named(hx, "Lina")
    r = dm(a, pid(b))
    assert r.status_code == 201
    conv = r.json()["conversation"]
    assert conv["kind"] == "direct" and conv["peer"] == "Lina" and conv["peer_card"]["public_id"] == pid(b)
    assert conv["request"] == {"state": "pending", "incoming": False, "pending_for_me": False}
    # recipient sees a request with the sender's real chosen name
    theirs = b.get("/api/conversations").json()["conversations"][0]
    assert theirs["request"]["pending_for_me"] is True and theirs["peer"] == "Amine"
    # up to DIRECT_MSG_BEFORE_REPLY_LIMIT messages until accepted/answered
    for _ in range(hx.settings.DIRECT_MSG_BEFORE_REPLY_LIMIT - 1):
        assert reply(a, conv["id"], "رسالة إضافية").status_code == 201
    stop = reply(a, conv["id"], "واحدة أكثر من اللازم")
    assert stop.status_code == 429 and stop.json()["error"]["code"] == "request_pending"
    # the recipient answers → accepted, the limit is gone
    assert reply(b, conv["id"], "أهلًا!").status_code == 201
    assert a.get("/api/conversations").json()["conversations"][0]["request"]["state"] == "accepted"
    assert reply(a, conv["id"], "رائع").status_code == 201


def test_no_duplicate_direct_conversation(hx):
    a, b = named(hx, "Amine"), named(hx, "Lina")
    c1 = dm(a, pid(b)).json()["conversation"]["id"]
    b.post(f"/api/conversations/{c1}/request", json={"action": "accept"})
    c2 = dm(a, pid(b), "مرة ثانية").json()["conversation"]["id"]
    c3 = dm(b, pid(a), "وأنا أيضًا").json()["conversation"]["id"]
    assert c1 == c2 == c3
    with hx.db() as db:
        assert db.scalar(select(func.count()).select_from(Conversation).where(Conversation.kind == "direct")) == 1
    assert a.get(f"/api/people/{pid(b)}").json()["conversation_id"] == c1


def test_ignore_and_block_requests(hx):
    a, b, c = named(hx, "Amine"), named(hx, "Lina"), named(hx, "Rami")
    conv = dm(a, pid(b)).json()["conversation"]["id"]
    assert b.post(f"/api/conversations/{conv}/request", json={"action": "ignore"}).json() == {"state": "ignored"}
    assert b.get("/api/conversations").json()["conversations"] == []  # ignored requests disappear
    assert reply(a, conv, "هل وصلت؟").status_code == 201  # sender is not told
    assert a.get("/api/conversations").json()["conversations"][0]["request"]["state"] == "pending"
    assert b.get("/api/conversations").json()["conversations"] == []
    # only the recipient can answer a request
    assert a.post(f"/api/conversations/{conv}/request", json={"action": "accept"}).status_code == 404
    conv2 = dm(c, pid(b)).json()["conversation"]["id"]
    assert b.post(f"/api/conversations/{conv2}/block").status_code == 200
    assert dm(c, pid(b), "مجددًا").status_code == 404  # blocked: indistinguishable from unknown
    assert c.get(f"/api/people/{pid(b)}").status_code == 404


def test_direct_respects_privacy_and_age(hx):
    a, b = named(hx, "Amine"), named(hx, "Lina")
    b.patch("/api/me/privacy", json={"accept_direct": "nobody"})
    r = dm(a, pid(b))
    assert r.status_code == 403 and r.json()["error"]["code"] == "direct_closed"
    assert a.get(f"/api/people/{pid(b)}").json()["can_message"] is False
    assert dm(a, "DZ-XXXXXX").status_code == 404 and dm(a, "not-an-id").status_code == 404
    assert dm(a, pid(a)).status_code == 404  # not yourself
    from sqlalchemy import text

    with hx.db() as db:
        db.execute(text("UPDATE users SET age_confirmed_at = NULL"))
    c = named(hx, "Rami")
    with hx.db() as db:
        db.execute(text("UPDATE users SET age_confirmed_at = NULL"))
    assert dm(c, pid(a)).json()["error"]["code"] == "age_required"


# ----------------------------------------------------------------- anonymous chats + reveal + privacy


def test_anonymous_chat_shows_dzplay_until_one_side_reveals(hx):
    a = named(hx, "Amine")
    b = named(hx, "Lina")
    conv = send(a, "مرحبًا من شخص مجهول").json()["conversation"]
    assert conv["kind"] == "anonymous" and conv["peer"] == "dzplay" and conv["peer_card"]["public_id"] is None
    theirs = b.get("/api/conversations").json()["conversations"][0]
    assert theirs["peer"] == "dzplay"
    # A reveals: B now sees A's name + ID; A still sees "dzplay" for B
    assert a.post(f"/api/conversations/{conv['id']}/reveal").json() == {"me_revealed": True}
    theirs = b.get("/api/conversations").json()["conversations"][0]
    assert theirs["peer"] == "Amine" and theirs["peer_card"]["public_id"] == pid(a)
    mine = a.get("/api/conversations").json()["conversations"][0]
    assert mine["peer"] == "dzplay" and mine["me_revealed"] is True
    msgs = b.get(f"/api/conversations/{conv['id']}").json()["messages"]
    sysmsg = [m for m in msgs if m["kind"] == "system"]
    assert len(sysmsg) == 1 and sysmsg[0]["meta"]["event"] == "reveal" and sysmsg[0]["meta"]["name"] == "Amine"
    # revealing twice adds nothing; direct chats cannot "reveal"
    a.post(f"/api/conversations/{conv['id']}/reveal")
    assert len([m for m in b.get(f"/api/conversations/{conv['id']}").json()["messages"] if m["kind"] == "system"]) == 1


def test_turning_off_anonymous_removes_from_matching_but_can_still_send(hx):
    sender = hx.user()
    target = hx.user()
    target.patch("/api/me/privacy", json={"accept_anonymous": False})
    r = send(sender, "هل يوجد أحد؟")
    assert r.status_code == 409  # the only other person opted out
    assert send(target, "أنا أرسل رغم أني لا أستقبل").status_code == 201


def test_mute_stops_push(hx):
    a, b = named(hx, "Amine"), named(hx, "Lina")
    conv = dm(a, pid(b)).json()["conversation"]["id"]
    b.post(f"/api/conversations/{conv}/request", json={"action": "accept"})
    assert b.post(f"/api/conversations/{conv}/mute", json={"muted": True}).json() == {"muted": True}
    assert b.get("/api/conversations").json()["conversations"][0]["muted"] is True
