"""V6 phase 17: posting pictures is a per-account permission granted by an admin (never by the membership); the
membership features are switched for everyone from the panel (today: the blue star); pictures in chats are off for
good — refused before a byte is read, the old ones deleted everywhere except reported ones (kept as evidence)."""

from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import admin_cli, clock
from app.models import AdminAuditLog, MediaItem, Message, Report, User
from app.services import media_items
from tests.conftest import reply, send
from tests.test_rewards import _credit, _withdraw, rx, smtp  # noqa: F401 (rx, smtp are fixtures)
from tests.test_uploads import STORAGE, jpeg, mx, ready_upload, upload  # noqa: F401 (mx is a fixture)


def _uid(hx, c) -> str:
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == c.email))


def _audit(hx, action: str) -> int:
    with hx.db() as db:
        return len(db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == action)).all())


# ----------------------------------------------------------------- the picture permission


@pytest.fixture
def px(mx):
    mx.default_images = False  # every account starts without the permission
    mx.adm = mx.admin()
    return mx


def test_nobody_has_it_by_default_and_a_membership_does_not_give_it(px):
    a = px.user()  # a member (the harness makes members by default)
    with px.db() as db:
        assert db.scalar(select(User).where(User.email == a.email)).member_since is not None
    assert a.get("/api/uploads/config").json()["idea"]["allowed"] is False
    r = a.post("/api/uploads/precheck", json={"purpose": "idea"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "images_not_allowed"
    r = upload(px, a, jpeg())
    assert r.status_code == 403 and r.json()["error"]["code"] == "images_not_allowed"
    with px.db() as db:
        assert db.scalar(select(MediaItem).where(MediaItem.purpose == "idea")) is None  # nothing was read or stored


def test_the_admin_grants_and_takes_it_back_audited(px):
    a = px.user()
    uid = _uid(px, a)
    r = px.adm.post(f"/api/admin/users/{uid}/images", json={"on": True})
    assert r.status_code == 200 and r.json()["allowed"] is True and r.json()["granted_by"] == "admin:owner"
    assert _audit(px, "images_grant") == 1
    assert a.get("/api/uploads/config").json()["idea"]["allowed"] is True
    mid = ready_upload(px, a, jpeg())
    post = a.post("/api/posts", json={"content": "فكرة بصورة", "media_id": mid})
    assert post.status_code == 201, post.text
    # the daily limit still applies to those who have it
    assert upload(px, a, jpeg((0, 90, 0))).json()["error"]["code"] == "idea_image_limit"
    detail = px.adm.get(f"/api/admin/access/users/{uid}").json()["images"]
    assert detail["allowed"] is True and detail["image_posts"] == 1
    # taken back: refused at once; the old picture post stays as it is
    assert px.adm.post(f"/api/admin/users/{uid}/images", json={"on": False}).json()["allowed"] is False
    assert _audit(px, "images_revoke") == 1
    clock.advance(25 * 3600)
    assert upload(px, a, jpeg()).json()["error"]["code"] == "images_not_allowed"
    pid = post.json()["id"]
    assert a.get(f"/api/posts/{pid}").json()["media"] is not None
    # the same state again changes nothing (no second audit line)
    px.adm.post(f"/api/admin/users/{uid}/images", json={"on": False})
    assert _audit(px, "images_revoke") == 1


def test_a_ready_upload_cannot_be_posted_after_the_permission_is_taken_back(px):
    a = px.user()
    uid = _uid(px, a)
    px.adm.post(f"/api/admin/users/{uid}/images", json={"on": True})
    mid = ready_upload(px, a, jpeg())
    px.adm.post(f"/api/admin/users/{uid}/images", json={"on": False})
    r = a.post("/api/posts", json={"content": "x", "media_id": mid})
    assert r.status_code == 403 and r.json()["error"]["code"] == "images_not_allowed"


def test_bulk_and_the_list_of_former_posters(px):
    a, b, c = px.user(), px.user(), px.user()
    with px.db() as db:  # «a» posted a picture before (when pictures were a membership feature)
        u = db.scalar(select(User).where(User.email == a.email))
        item = MediaItem(owner_id=u.id, owner_public_id=u.public_id, purpose="idea", kind="image", state="ready",
                         created_at=clock.utcnow())
        db.add(item)
        db.commit()
        mid = item.id
    with px.db() as db:
        from app.models import Post

        db.add(Post(author_id=_uid(px, a), content="قديمة", media_id=mid, status="visible", created_at=clock.utcnow()))
        db.commit()
    lists = px.adm.get("/api/admin/images").json()
    former = {r["id"]: r for r in lists["former"]}
    assert former[_uid(px, a)]["image_posts"] == 1 and former[_uid(px, a)]["allowed"] is False
    assert lists["holders"] == []
    ids = [_uid(px, a), _uid(px, b), _uid(px, c)]
    assert px.adm.post("/api/admin/images/bulk", json={"ids": ids, "on": True}).json()["changed"] == 3
    assert px.adm.post("/api/admin/images/bulk", json={"ids": ids, "on": True}).json()["changed"] == 0
    assert {r["id"] for r in px.adm.get("/api/admin/images").json()["holders"]} == set(ids)
    assert px.adm.post("/api/admin/images/bulk", json={"ids": ids[:2], "on": False}).json()["changed"] == 2
    assert [r["id"] for r in px.adm.get("/api/admin/images").json()["holders"]] == [ids[2]]
    assert _audit(px, "images_grant") == 3 and _audit(px, "images_revoke") == 2
    assert px.adm.post("/api/admin/images/bulk", json={"ids": [], "on": True}).status_code == 400


def test_only_the_admin_can_change_it(px):
    a = px.user()
    for path, body in ((f"/api/admin/users/{_uid(px, a)}/images", {"on": True}), ("/api/admin/images/bulk", {"ids": [_uid(px, a)], "on": True})):
        assert a.post(path, json=body).status_code in (401, 403, 404, 405)  # the panel lives under ADMIN_PATH
    assert a.get("/api/uploads/config").json()["idea"]["allowed"] is False


# ----------------------------------------------------------------- membership features


def _with_star(hx, c, by: str) -> None:
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        u.member_since, u.verified_at, u.verified_by = clock.utcnow(), clock.utcnow(), by
        db.commit()


def _verified_in_feed(hx, viewer, *authors) -> list[bool]:
    feed = viewer.get("/api/posts/feed").json()["posts"]
    return [next(p for p in feed if p["content"] == c.idea)["author"]["verified"] for c in authors]


def test_switching_the_star_feature_off_takes_it_from_members_only(make_harness):
    hx = make_harness()
    hx.adm = hx.admin()
    member, manual, viewer = hx.user(), hx.user(), hx.user()
    _with_star(hx, member, "membership")
    _with_star(hx, manual, "admin:owner")
    for n, c in enumerate((member, manual)):
        c.idea = f"فكرة رقم {n + 1}"
        assert c.post("/api/posts", json={"content": c.idea}).status_code == 201
    assert _verified_in_feed(hx, viewer, member, manual) == [True, True]
    assert member.get("/api/membership").json()["features"] == ["النجمة الزرقاء بجانب اسمك"]
    assert "النجمة الزرقاء بجانب اسمك" in hx.client().get("/policies/membership_terms").text  # {{member_features}}
    r = hx.adm.put("/api/admin/settings", json={"changes": {"MEMBER_FEATURE_STAR": False}})
    assert r.status_code == 200
    assert _verified_in_feed(hx, viewer, member, manual) == [False, True]  # a star granted by hand stays
    assert member.get("/api/membership").json()["features"] == []
    assert "النجمة الزرقاء بجانب اسمك" not in hx.client().get("/policies/membership_terms").text
    hx.adm.put("/api/admin/settings", json={"changes": {"MEMBER_FEATURE_STAR": True}})
    assert _verified_in_feed(hx, viewer, member, manual) == [True, True]


def test_a_non_member_earns_and_withdraws(rx):  # noqa: F811
    """The rule stays: the membership is never part of any reward or withdrawal."""
    a = rx.user()
    with rx.db() as db:
        assert db.scalar(select(User).where(User.email == a.email)).member_since is None
    _credit(rx, a, "30")
    assert _withdraw(rx, a, amount="12").status_code == 201


# ----------------------------------------------------------------- chat pictures: off for good


def _chat(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "مرحبا").json()["conversation"]["id"]
    assert reply(b, cid, "أهلا").status_code == 201
    return a, b, cid


def test_chat_picture_uploads_are_refused_before_the_body_is_read(mx):
    """The ASGI app answers 410 without ever asking for the request body."""
    reads: list[int] = []

    async def receive():
        reads.append(1)
        return {"type": "http.request", "body": b"x" * 1000, "more_body": False}

    sent: list[dict] = []

    async def send_(message):
        sent.append(message)

    for path, query in (("/api/uploads", b"purpose=chat"), ("/api/conversations/abc123/media", b"")):
        scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": "POST", "scheme": "http",
                 "path": path, "raw_path": path.encode(), "query_string": query, "root_path": "",
                 "headers": [(b"host", b"testserver"), (b"content-length", b"1000"), (b"x-dz-requested", b"1")],
                 "client": ("127.0.0.1", 50000), "server": ("testserver", 80)}
        sent.clear()
        asyncio.run(mx.app(scope, receive, send_))
        start = next(m for m in sent if m["type"] == "http.response.start")
        body = json.loads(b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body"))
        assert start["status"] == 410 and body["error"]["code"] == "chat_images_disabled"
    assert reads == []


def test_every_chat_picture_path_is_closed(mx):
    a, b, cid = _chat(mx)
    r = upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    assert r.status_code == 410 and r.json()["error"]["code"] == "chat_images_disabled"
    r = a.post("/api/uploads/precheck", json={"purpose": "chat", "conversation_id": cid})
    assert r.status_code == 410
    assert a.post(f"/api/conversations/{cid}/media", json={"media_id": "a" * 32}).status_code == 410
    assert a.get("/api/uploads/config").json()["chat"] == {"enabled": False}
    with mx.db() as db:
        assert db.scalar(select(MediaItem).where(MediaItem.purpose == "chat")) is None
        user = db.scalar(select(User).where(User.email == a.email))
        with pytest.raises(Exception) as exc:  # defence in depth: the service refuses too
            media_items.begin_upload(db, mx.settings, mx.state.limiter, user, purpose="chat", length=100)
        assert getattr(exc.value, "code", None) == "chat_images_disabled"
    assert mx.tg.documents == []
    with mx.db() as db:
        rows = {r["key"] for r in __import__("app.services.tunables", fromlist=["listing"]).listing(db, mx.settings)}
    assert not [k for k in rows if k.startswith("CHAT_IMAGE")]  # the settings are gone from the panel


def _old_picture(hx, a, b, cid, n: int, *, state="attached", reported=False, held=False) -> tuple[str, str]:
    """An old chat picture as V5/V6 stored it (file in the storage channel, message in the chat)."""
    with hx.db() as db:
        ua = db.scalar(select(User).where(User.email == a.email))
        ub = db.scalar(select(User).where(User.email == b.email))
        now = clock.utcnow()
        item = MediaItem(owner_id=ua.id, owner_public_id=ua.public_id, purpose="chat", kind="image", state=state,
                         conversation_id=cid, attached_type="message", attached_at=now, created_at=now,
                         tg_file_id=f"file-{n}", tg_message_id=1000 + n, legal_hold=held or reported,
                         reports_count=1 if reported else None, removed_at=now if state != "attached" else None)
        db.add(item)
        db.flush()
        msg = Message(conversation_id=cid, sender_id=ua.id, recipient_id=ub.id, content="", kind="image",
                      media_id=item.id, created_at=now, expires_at=now + timedelta(days=1))
        db.add(msg)
        db.flush()
        item.attached_id = msg.id
        if reported:
            db.add(Report(reporter_id=ub.id, reported_user_id=ua.id, conversation_id=cid, message_id=msg.id,
                          media_id=item.id, reason="harassment", created_at=now, expires_at=now + timedelta(days=30)))
        db.commit()
        return item.id, msg.id


def test_old_chat_pictures_show_as_deleted_and_are_never_served(mx):
    a, b, cid = _chat(mx)
    mid, msg_id = _old_picture(mx, a, b, cid, 1)
    for c in (a, b):
        m = next(m for m in c.get(f"/api/conversations/{cid}").json()["messages"] if m["id"] == msg_id)
        assert m["media"] == {"kind": "image", "state": "expired", "blur": None}
    assert b.post(f"/api/messages/{msg_id}/open").status_code == 410
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        user = db.scalar(select(User).where(User.email == b.email))
        assert media_items.can_view(db, user, item) is None


def test_the_purge_deletes_everywhere_except_reported_pictures(mx, capsys):
    a, b, cid = _chat(mx)
    normal, _ = _old_picture(mx, a, b, cid, 1)
    expired, _ = _old_picture(mx, a, b, cid, 2, state="expired")
    reported, _ = _old_picture(mx, a, b, cid, 3, reported=True)
    archived, _ = _old_picture(mx, a, b, cid, 4, state="expired", held=True)  # the old archive mode, no report
    with mx.db() as db:
        assert media_items.chat_images_left(db) == 4
    admin_cli._purge_chat_images(mx.settings, mx.state.database, mx.tg.transport, delay=0)
    out = json.loads(capsys.readouterr().out)
    assert out == {"removed": 3, "kept_reported": 1, "telegram_deleted": 3, "telegram_not_deleted": []}
    assert sorted(m for _c, m in mx.tg.deleted) == [1001, 1002, 1004]
    assert {c for c, _m in mx.tg.deleted} == {STORAGE}
    with mx.db() as db:
        for mid in (normal, expired, archived):
            item = db.get(MediaItem, mid)
            assert item.state in ("removed", "expired") and not item.legal_hold
            assert item.tg_file_id is None and item.tg_message_id is None
        kept = db.get(MediaItem, reported)
        assert kept.state == "removed" and kept.legal_hold is True and kept.tg_file_id == "file-3"  # evidence
        assert media_items.chat_images_left(db) == 0
    assert _audit(mx, "chat_images_purge") == 1
    # again: nothing more to do
    admin_cli._purge_chat_images(mx.settings, mx.state.database, mx.tg.transport, delay=0)
    assert json.loads(capsys.readouterr().out)["removed"] == 0 and len(mx.tg.deleted) == 3


def test_what_telegram_refuses_is_listed_and_retried(mx, capsys):
    a, b, cid = _chat(mx)
    mid, _ = _old_picture(mx, a, b, cid, 7)
    mx.tg.fail_methods.add("deleteMessage")
    admin_cli._purge_chat_images(mx.settings, mx.state.database, mx.tg.transport, delay=0)
    captured = capsys.readouterr()
    assert json.loads(captured.out)["telegram_not_deleted"] == [1007] and "by hand" in captured.err
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        assert item.state == "removed" and item.tg_file_id is None and item.tg_message_id == 1007  # unreachable already
        assert media_items.chat_images_left(db) == 1
    mx.tg.fail_methods.clear()
    admin_cli._purge_chat_images(mx.settings, mx.state.database, mx.tg.transport, delay=0)
    assert json.loads(capsys.readouterr().out)["telegram_deleted"] == 1
    with mx.db() as db:
        assert db.get(MediaItem, mid).tg_message_id is None and media_items.chat_images_left(db) == 0


def test_the_check_mode_of_the_command(mx, monkeypatch, capsys):
    from app.config import get_settings

    a, b, cid = _chat(mx)
    _old_picture(mx, a, b, cid, 9)
    monkeypatch.setenv("DATABASE_URL", mx.settings.DATABASE_URL)
    monkeypatch.setenv("SECRET_KEY", mx.settings.SECRET_KEY)
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["purge-chat-images", "--check"])
        assert exc.value.code == 1 and "1" in capsys.readouterr().out
        with mx.db() as db:
            media_items.purge_chat_images(db)
            db.commit()
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["purge-chat-images", "--check"])
        assert exc.value.code == 1  # the storage copy is still listed until the bot deletes it
        with mx.db() as db:
            for item in db.scalars(select(MediaItem)).all():
                item.tg_message_id = None
            db.commit()
        with pytest.raises(SystemExit) as exc:
            admin_cli.main(["purge-chat-images", "--check"])
        assert exc.value.code == 0
    finally:
        get_settings.cache_clear()
