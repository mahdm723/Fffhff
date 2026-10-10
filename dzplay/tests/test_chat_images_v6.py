"""V6 phase 6: sending chat pictures is a membership feature; anyone may receive and open them. Once opened a
picture stays CHAT_IMAGE_TTL_AFTER_VIEW (60 s by default), then it is gone for both sides and from Telegram."""

from __future__ import annotations

from sqlalchemy import select

from app import clock
from app.config import Settings
from app.models import MediaItem, User
from tests.conftest import reply, send
from tests.test_uploads import STORAGE, jpeg, mx, ready_upload, tick, upload  # noqa: F401 (mx is a fixture)


def _set_member(hx, c, member: bool) -> None:
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        if member:
            u.member_since, u.member_ended_at = clock.utcnow(), None
        else:
            u.member_ended_at = clock.utcnow() if u.member_since else None
        db.commit()


def _chat(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "مرحبا").json()["conversation"]["id"]
    assert reply(b, cid, "أهلا").status_code == 201
    return a, b, cid


def _send(c, cid, mid):
    return c.post(f"/api/conversations/{cid}/media", json={"media_id": mid})


def test_default_is_60_seconds_and_config_tells_the_phone():
    assert Settings.model_fields["CHAT_IMAGE_TTL_AFTER_VIEW"].default == 60


def test_non_member_cannot_send_but_can_receive_and_open(mx):
    a, b, cid = _chat(mx)
    _set_member(mx, b, False)
    cfg_a, cfg_b = a.get("/api/uploads/config").json(), b.get("/api/uploads/config").json()
    assert cfg_a["chat"]["member"] is True and cfg_b["chat"]["member"] is False
    assert cfg_a["chat"]["ttl_after_view"] == mx.settings.CHAT_IMAGE_TTL_AFTER_VIEW
    # the non-member is refused at every step (precheck, upload start)
    r = b.post("/api/uploads/precheck", json={"purpose": "chat", "conversation_id": cid})
    assert r.status_code == 403 and r.json()["error"]["code"] == "members_only"
    r = upload(mx, b, jpeg(), purpose="chat", conversation_id=cid)
    assert r.status_code == 403 and r.json()["error"]["code"] == "members_only"
    with mx.db() as db:
        assert db.scalar(select(MediaItem).where(MediaItem.purpose == "chat")) is None  # nothing stored
    assert mx.tg.documents == []
    # the member sends; the non-member opens it
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    msg = _send(a, cid, mid).json()["message"]
    opened = b.post(f"/api/messages/{msg['id']}/open")
    assert opened.status_code == 200 and b.get(opened.json()["url"]).status_code == 200


def test_membership_ended_after_the_upload_blocks_the_send(mx):
    a, b, cid = _chat(mx)
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    _set_member(mx, a, False)
    r = _send(a, cid, mid)
    assert r.status_code == 403 and r.json()["error"]["code"] == "members_only"
    _set_member(mx, a, True)  # a new membership: allowed again
    assert _send(a, cid, mid).status_code == 201


def test_gone_for_both_sides_60_seconds_after_opening_then_from_telegram(mx):
    assert mx.settings.CHAT_IMAGE_TTL_AFTER_VIEW == 60
    a, b, cid = _chat(mx)
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    msg = _send(a, cid, mid).json()["message"]
    opened = b.post(f"/api/messages/{msg['id']}/open").json()
    assert opened["seconds_left"] == 60
    clock.advance(59)
    for c in (a, b):
        m = next(m for m in c.get(f"/api/conversations/{cid}").json()["messages"] if m["id"] == msg["id"])
        assert m["media"]["state"] == "open" and m["media"]["seconds_left"] <= 1
    clock.advance(2)
    assert b.get(opened["url"]).status_code == 404
    assert b.post(f"/api/messages/{msg['id']}/open").status_code == 410
    for c in (a, b):
        m = next(m for m in c.get(f"/api/conversations/{cid}").json()["messages"] if m["id"] == msg["id"])
        assert m["media"] == {"kind": "image", "state": "expired", "blur": None}
    assert tick(mx)["expired"] == 1
    assert not list(mx.state.media.dir.glob(f"{mid}*"))
    clock.advance(mx.settings.CHAT_IMAGE_REPORT_GRACE + 1)
    tick(mx)
    assert (STORAGE, mx.tg.documents[0]["message_id"]) in mx.tg.deleted
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        assert item.state == "expired" and item.tg_file_id is None
