"""User protection: automatic flagging, admin review of reported/flagged users, privacy notice."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from app import clock
from app.config import PRIVACY_VERSION
from app.models import ContentFlag, Message, SecurityEvent, User
from app.services.moderation import normalize, scan
from tests.conftest import reply, send

from tests.conftest import ADMIN_PATH

REASON = {"reason": "مراجعة بلاغ"}


# ----------------------------------------------------------------- scanner


def test_normalize_unifies_variants():
    assert normalize("أَهْلًا  بِكَ") == normalize("اهلا بك")
    assert normalize("قحبـــة") == normalize("قحبه")
    assert normalize("كلللب") == normalize("كلب")
    assert normalize("رقمي ٠٦٦١") == "رقمي 0661"


@pytest.mark.parametrize("text,category", [
    ("والله نقتلك إذا شفتك", "threat"),
    ("راني نعرف وين تسكن", "threat"),
    ("je vais te tuer", "threat"),
    ("ابعث الدراهم ولا نفضحك", "blackmail"),
    ("nfde7ek 9dam bak", "blackmail"),
    ("يا قحبة", "sexual"),
    ("ابعثيلي صورتك", "sexual"),
    ("zebi", "sexual"),
    ("يا كلللب", "insult"),
    ("nique ta mere", "insult"),
    ("عطيني رقمك 0555 12 34 56", "contact"),
    ("رقمي ٠٦٦١٢٣٤٥٦٧", "contact"),
    ("add me on snap @dz_boy", "contact"),
    ("write me: someone@mail.com", "contact"),
])
def test_scan_detects_categories(text, category):
    assert category in scan(text).categories


@pytest.mark.parametrize("text", [
    "مرحبا كيف حالك اليوم؟", "عندي كلب صغير وأحبه", "الصورة جميلة", "واش راك خويا", "I love this idea",
    "nchallah labas", "شفت فيلم عن الكلاب", "هذا كساء جميل", "قرأت 3 كتب هذا الشهر",
])
def test_scan_ignores_normal_sentences(text):
    assert not scan(text)


def test_scan_extra_words_from_config():
    assert scan("كلمة_خاصة هنا", "كلمة_خاصة").categories == ["custom"]
    assert scan("prefixed-word", "prefix*").categories == ["custom"]


# ----------------------------------------------------------------- flagging


def test_threatening_message_is_flagged_but_still_delivered(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    r = send(a, "راني نعرف وين تسكن، نقتلك")
    assert r.status_code == 201
    body = r.text
    assert "flag" not in body and "threat" not in body  # the sender is not told
    convs = b.get("/api/conversations").json()["conversations"]
    assert len(convs) == 1  # delivered normally

    flags = hx.admin().get("/api/admin/flags").json()["flags"]
    assert len(flags) == 1
    f = flags[0]
    assert f["categories"] == ["threat"] and f["target"] == "message"
    assert f["content"] == "راني نعرف وين تسكن، نقتلك"
    with hx.db() as db:
        sender = db.scalar(select(User).where(User.email == a.email))
        assert f["offender_ref"] == sender.id
    assert "@" not in json.dumps(flags)

    # A clean reply is not flagged.
    cid = convs[0]["id"]
    assert reply(b, cid, "من أنت؟ لا أفهم").status_code == 201
    assert len(hx.admin().get("/api/admin/flags").json()["flags"]) == 1


def test_flag_snapshot_survives_message_ttl(make_harness):
    hx = make_harness()
    a, _b = hx.user(), hx.user()
    send(a, "ابعث الدراهم ولا نفضحك")
    clock.advance(hx.settings.MESSAGE_TTL + 60)
    c = hx.admin()
    assert c.post("/api/admin/cleanup").json()["deleted"]["messages"] == 1
    flags = c.get("/api/admin/flags").json()["flags"]
    assert flags[0]["content"] == "ابعث الدراهم ولا نفضحك"


def test_private_comment_is_flagged(make_harness):
    hx = make_harness()
    owner, b = hx.user(), hx.user()
    pid = owner.post("/api/posts", json={"content": "فكرة عامة"}).json()["id"]
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "يا قحبة"}).status_code == 201
    flags = hx.admin().get("/api/admin/flags").json()["flags"]
    assert flags[0]["target"] == "comment" and flags[0]["categories"] == ["sexual"]


def test_moderation_can_be_disabled(make_harness):
    hx = make_harness(MODERATION_ENABLED=False)
    a, _b = hx.user(), hx.user()
    send(a, "نقتلك")
    with hx.db() as db:
        assert db.scalar(select(ContentFlag.id)) is None


def test_resolve_flag_remove_deletes_message_and_ban_revokes(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    send(a, "يا كلب")
    cid = b.get("/api/conversations").json()["conversations"][0]["id"]
    reply(a, cid, "نقتلك")
    c = hx.admin()
    flags = c.get("/api/admin/flags").json()["flags"]
    assert len(flags) == 2
    insult = next(f for f in flags if f["categories"] == ["insult"])
    threat = next(f for f in flags if f["categories"] == ["threat"])
    assert insult["offender_flags_total"] == 2

    assert c.post(f"/api/admin/flags/{insult['id']}/resolve", json={"action": "remove"}).status_code == 200
    contents = [m["content"] for m in b.get(f"/api/conversations/{cid}").json()["messages"]]
    assert "يا كلب" not in contents and "نقتلك" in contents
    assert c.post(f"/api/admin/flags/{threat['id']}/resolve", json={"action": "ban"}).json()["resolution"] == "ban"
    assert a.get("/api/me").status_code == 401
    assert c.post(f"/api/admin/flags/{threat['id']}/resolve", json={"action": "explode"}).status_code == 400
    assert c.get("/api/admin/flags").json()["flags"] == []
    assert c.get("/api/admin/stats").json()["safety"]["flags_open"] == 0


# ----------------------------------------------------------------- conversation review


def _ref(hx, client) -> str:
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == client.email))


def test_admin_conversation_views_are_logged(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    send(a, "مرحبا، كيف كان يومك؟")
    cid = b.get("/api/conversations").json()["conversations"][0]["id"]
    reply(b, cid, "جيد، شكرًا")
    c = hx.admin()
    ref_a = _ref(hx, a)

    # Full access (policy v3): any user's stored conversations, always audited; reason optional.
    assert c.get(f"/api/admin/users/{ref_a}/conversations").status_code == 200
    assert c.get("/api/admin/users/nope/conversations", params=REASON).status_code == 404
    assert hx.client().get(f"{ADMIN_PATH}/api/admin/users/{ref_a}/conversations", params=REASON).status_code == 401

    # After a report, the stored conversations become reviewable.
    assert b.post(f"/api/conversations/{cid}/report", json={"reason": "harassment"}).status_code == 201
    r = c.get(f"/api/admin/users/{ref_a}/conversations", params=REASON)
    assert r.status_code == 200
    data = r.json()
    conv = data["conversations"][0]
    assert conv["started_by"] == "user"
    assert [(m["from"], m["content"]) for m in conv["messages"]] == [("user", "مرحبا، كيف كان يومك؟"), ("peer", "جيد، شكرًا")]
    assert a.email not in r.text and b.email not in r.text and "@example.com" not in r.text

    with hx.db() as db:
        views = db.scalars(select(SecurityEvent).where(SecurityEvent.type == "admin_view_messages")).all()
        assert len(views) == 2 and {v.user_id for v in views} == {ref_a}
    entries = c.get("/api/admin/audit", params={"action": "view_conversations"}).json()["entries"]
    assert len(entries) == 2 and entries[0]["reason"] == "مراجعة بلاغ" and entries[0]["target_id"] == ref_a
    assert entries[0]["actor"] == "owner"


def test_flag_alone_opens_review_and_marks_flagged_message(make_harness):
    hx = make_harness()
    a, _b = hx.user(), hx.user()
    send(a, "ابعثيلي صورتك")
    data = hx.admin().get(f"/api/admin/users/{_ref(hx, a)}/conversations", params=REASON).json()
    assert data["conversations"][0]["messages"][0]["flagged"] is True


# ----------------------------------------------------------------- privacy notice


def test_privacy_notice_for_existing_users_only_once(hx):
    a = hx.user()
    assert hx.client().get("/api/me").status_code == 401
    assert a.get("/api/me").json()["privacy_notice"] is False  # new accounts accept it at sign-up

    with hx.db() as db:  # an account created before the notice existed
        db.scalar(select(User).where(User.email == a.email)).privacy_ack_version = None
    assert a.get("/api/me").json()["privacy_notice"] is True
    r = a.post("/api/me/privacy-ack")
    assert r.status_code == 200 and r.json()["version"] == PRIVACY_VERSION
    assert a.get("/api/me").json()["privacy_notice"] is False
    assert hx.client().post("/api/me/privacy-ack").status_code == 401


def test_message_model_untouched_by_flagging(make_harness):
    """Flagging keeps a copy; it must not alter or hold the original message."""
    hx = make_harness()
    a, _b = hx.user(), hx.user()
    send(a, "نقتلك")
    with hx.db() as db:
        msg = db.scalar(select(Message))
        assert msg.content == "نقتلك" and msg.expires_at is not None
