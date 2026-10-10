"""V6 phase 3: a name is mandatory (registration, Google onboarding, existing nameless accounts), profile
pictures (same scanning pipeline, daily limit, moderation, signed URLs), and the «المستخدمون» list."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import clock
from app.models import MediaItem, User
from tests import fake_telegram as tg
from tests.conftest import PASSWORD
from tests.test_uploads import MODCHAT, STORAGE, deliver, jpeg, ready_upload, upload


def _set(hx, email, **fields):
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == email))
        for k, v in fields.items():
            setattr(u, k, v)
        db.commit()


# ----------------------------------------------------------------- mandatory name


def test_registration_requires_a_valid_name(hx):
    c = hx.client()
    r = hx.register(c, "noname@example.com", display_name=None)
    assert r.status_code == 400 and r.json()["error"]["code"] == "name_required"
    for bad, code in (("dzplay", "reserved_name"), ("DZ PLAY", "reserved_name"), ("ab", "invalid_name_length"),
                      ("12345", "invalid_name"), ("a\u202eb\u202ccd", "invalid_name")):
        r = hx.register(c, "noname@example.com", display_name=bad)
        assert r.status_code == 400 and r.json()["error"]["code"] == code, (bad, r.text)
    r = hx.register(c, "named@example.com", display_name="  سارة   الجزائرية ")
    assert r.status_code == 201, r.text
    me = c.get("/api/me").json()
    assert me["display_name"] == "سارة الجزائرية" and me["has_custom_name"] is True and me["name_required"] is False
    # the first change is not blocked by the cooldown
    assert c.patch("/api/me/profile", json={"display_name": "Sara DZ"}).status_code == 200


def test_cannot_go_back_to_no_name(hx):
    a = hx.user()
    for empty in ("", "   ", "dzplay"):
        r = a.patch("/api/me/profile", json={"display_name": empty})
        assert r.status_code == 400 and r.json()["error"]["code"] in ("name_required", "reserved_name"), r.text
    assert a.get("/api/me").json()["has_custom_name"] is True


def test_existing_nameless_account_must_choose_a_name_first(hx):
    a, b = hx.user(), hx.user()
    _set(hx, a.email, display_name=None, name_norm=None)
    me = a.get("/api/me").json()
    assert me["name_required"] is True
    r = a.post("/api/posts", json={"content": "فكرة بلا اسم"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "name_required"
    bid = b.get("/api/me").json()["public_id"]
    r = a.post(f"/api/people/{bid}/messages", json={"content": "مرحبا"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "name_required"
    # reading still works, then a name unlocks everything
    assert a.get("/api/posts/feed").status_code == 200
    assert a.patch("/api/me/profile", json={"display_name": "Ahmed"}).status_code == 200
    assert a.get("/api/me").json()["name_required"] is False
    assert a.post("/api/posts", json={"content": "فكرة باسم"}).status_code == 201


def test_google_onboarding_asks_for_the_name(hx):
    a = hx.user()
    _set(hx, a.email, display_name=None, name_norm=None, onboarding_required=True)
    r = a.post("/api/me/onboarding", json={"gender": "male", "age_confirmed": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "name_required"
    r = a.post("/api/me/onboarding", json={"gender": "male", "age_confirmed": True, "display_name": "Karim"})
    assert r.status_code == 200, r.text
    assert r.json()["display_name"] == "Karim" and r.json()["needs_onboarding"] is False


def test_team_accounts_are_not_asked(hx):
    from app.services import engagement

    with hx.db() as db:
        engagement._system_pool(db, hx.settings)
        db.commit()
        sys_user = db.scalar(select(User).where(User.is_system.is_(True)))
        from app.services.messaging import profile

        assert profile(sys_user, hx.settings)["name_required"] is False


# ----------------------------------------------------------------- profile picture


@pytest.fixture
def px(make_harness):
    fake = tg.FakeTelegram()
    from tests.test_uploads import SECRET

    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=SECRET,
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE, TELEGRAM_MODERATION_CHAT_ID=MODCHAT)
    hx.tg = fake
    return hx


def _square(color=(30, 160, 90)):
    return jpeg(color=color, size=(512, 512), gps=True)


def test_avatar_upload_scan_and_signed_url_everywhere(px):
    a, b = px.user(), px.user()
    mid = ready_upload(px, a, _square(), purpose="avatar")
    r = a.put("/api/me/avatar", json={"media_id": mid})
    assert r.status_code == 200, r.text
    url = r.json()["avatar_url"]
    assert url and url.startswith(f"/media/{mid}/img")
    px.state.pipeline.wait_idle()
    assert a.get(url).status_code == 200
    # stored in Telegram, announced to the moderators with buttons
    assert any(m["chat_id"] == MODCHAT and "صورة شخصية" in (m.get("caption") or "") for m in px.tg.copies)
    a_id = a.get("/api/me").json()["public_id"]
    # visible to others: card, list, posts
    card = b.get(f"/api/people/{a_id}").json()
    assert card["avatar_url"] and card["avatar_url"].startswith(f"/media/{mid}/img")
    assert b.get(card["avatar_url"]).status_code == 200
    a.post("/api/posts", json={"content": "فكرة مع صورة شخصية"})
    post = b.get("/api/posts/feed").json()["posts"][0]
    assert post["author"]["avatar_url"].startswith(f"/media/{mid}/img")
    listed = [p for p in b.get("/api/people").json()["results"] if p["public_id"] == a_id]
    assert listed and listed[0]["avatar_url"]
    # the URL is bound to the viewer's session: useless for anyone else
    assert px.client().get(card["avatar_url"]).status_code in (401, 403, 404)
    # can't use somebody else's upload, nor an idea upload
    other = ready_upload(px, b, _square((200, 10, 10)), purpose="avatar")
    assert a.put("/api/me/avatar", json={"media_id": other}).status_code == 404
    idea = ready_upload(px, a, jpeg(), purpose="idea")
    assert a.put("/api/me/avatar", json={"media_id": idea}).status_code == 404


def test_avatar_daily_limit_and_replace(px):
    a = px.user()
    px.settings.AVATAR_CHANGES_PER_DAY = 2
    first = ready_upload(px, a, _square((1, 2, 3)), purpose="avatar")
    assert a.put("/api/me/avatar", json={"media_id": first}).status_code == 200
    second = ready_upload(px, a, _square((4, 5, 6)), purpose="avatar")
    assert a.put("/api/me/avatar", json={"media_id": second}).status_code == 200
    with px.db() as db:
        assert db.get(MediaItem, first).state == "removed"  # the old picture goes away
    r = upload(px, a, _square((7, 8, 9)), purpose="avatar", wait=False)
    assert r.status_code == 429 and r.json()["error"]["code"] == "avatar_limit"
    clock.advance(86400 + 60)
    third = ready_upload(px, a, _square((7, 8, 9)), purpose="avatar")
    assert a.put("/api/me/avatar", json={"media_id": third}).status_code == 200
    assert a.delete("/api/me/avatar").status_code == 200
    assert a.get("/api/me").json()["avatar_url"] is None


def test_avatar_removed_by_moderator_button(px):
    a, b = px.user(), px.user()
    mid = ready_upload(px, a, _square(), purpose="avatar")
    a.put("/api/me/avatar", json={"media_id": mid})
    deliver(px, tg.callback(f"md:del:{mid}", sender=777, chat=int(MODCHAT)))
    assert a.get("/api/me").json()["avatar_url"] is None
    a_id = a.get("/api/me").json()["public_id"]
    assert b.get(f"/api/people/{a_id}").json()["avatar_url"] is None
    with px.db() as db:
        assert db.scalar(select(User.avatar_media_id).where(User.email == a.email)) is None


def test_avatar_report_hides_and_alerts(px):
    a, b = px.user(), px.user()
    mid = ready_upload(px, a, _square(), purpose="avatar")
    a.put("/api/me/avatar", json={"media_id": mid})
    a_id = a.get("/api/me").json()["public_id"]
    r = b.post(f"/api/people/{a_id}/report-avatar", json={"reason": "minor"})
    assert r.status_code == 201, r.text
    assert b.get(f"/api/people/{a_id}").json()["avatar_url"] is None  # hidden at once ("minor")
    assert any("قاصر" in (m.get("caption") or "") for m in px.tg.copies)
    assert b.post(f"/api/people/{a_id}/report-avatar", json={"reason": "spam"}).status_code in (201, 200)
    # nothing to report when there is no picture
    c = px.user()
    c_id = c.get("/api/me").json()["public_id"]
    assert b.post(f"/api/people/{c_id}/report-avatar", json={"reason": "spam"}).status_code == 404


def test_admin_can_remove_a_picture(px):
    a = px.user()
    mid = ready_upload(px, a, _square(), purpose="avatar")
    a.put("/api/me/avatar", json={"media_id": mid})
    adm = px.admin()
    with px.db() as db:
        uid = db.scalar(select(User.id).where(User.email == a.email))
    r = adm.post(f"/api/admin/users/{uid}/avatar/remove")
    assert r.status_code == 200, r.text
    assert a.get("/api/me").json()["avatar_url"] is None
    assert any(e["action"] == "media_del" for e in adm.get("/api/admin/audit").json()["entries"])


# ----------------------------------------------------------------- users list


def test_users_list_excludes_and_paginates(hx):
    viewer = hx.user()
    people = [hx.user() for _ in range(5)]
    hidden = hx.user()
    blocked_me = hx.user()
    banned = hx.user()
    nameless = hx.user()
    _set(hx, hidden.email, searchable_by_name=False)
    _set(hx, banned.email, status="banned")
    _set(hx, nameless.email, display_name=None, name_norm=None)
    bid = viewer.get("/api/me").json()["public_id"]
    sent = blocked_me.post(f"/api/people/{bid}/messages", json={"content": "مرحبا"}).json()
    assert blocked_me.post(f"/api/conversations/{sent['conversation']['id']}/block").status_code == 200, sent
    hx.settings.SEARCH_MAX_RESULTS = 2
    seen, cursor = [], None
    for _ in range(10):
        r = viewer.get("/api/people" + (f"?cursor={cursor}" if cursor else ""))
        assert r.status_code == 200, r.text
        d = r.json()
        seen += [p["public_id"] for p in d["results"]]
        cursor = d["next_cursor"]
        if not cursor:
            break
    ids = {c.get("/api/me").json()["public_id"] for c in people}
    assert set(seen) == ids and len(seen) == len(set(seen))
    for p in d["results"]:
        assert set(p) == {"public_id", "name", "avatar_url", "verified", "gender"}
    # search by name and by ID still respect the rules
    _set(hx, people[0].email, display_name="Yacine Trader", name_norm="yacine trader")
    found = viewer.get("/api/people?q=yacine").json()["results"]
    assert [p["public_id"] for p in found] == [people[0].get("/api/me").json()["public_id"]]
    hid = hidden.get("/api/me").json()["public_id"]
    assert [p["public_id"] for p in viewer.get(f"/api/people?q={hid}").json()["results"]] == [hid]


def test_users_list_needs_a_session_and_is_rate_limited(hx):
    assert hx.client().get("/api/people").status_code == 401
    v = hx.user()
    hx.settings.SEARCH_PER_MINUTE = 3
    codes = [v.get("/api/people").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429


def test_no_private_data_in_people_responses(px):
    a, b = px.user(), px.user()
    a_id = a.get("/api/me").json()["public_id"]
    bodies = [b.get("/api/people").text, b.get(f"/api/people/{a_id}").text, b.get(f"/api/people/search?q={a_id}").text]
    with px.db() as db:
        internal = db.scalar(select(User.id).where(User.email == a.email))
    for text in bodies:
        assert a.email not in text and internal not in text and PASSWORD not in text
        data = json.loads(text)
        assert "email" not in json.dumps(data)
