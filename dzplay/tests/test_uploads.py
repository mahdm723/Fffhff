"""V5 phase B: uploads through the media worker into Telegram storage, idea pictures, ephemeral chat
pictures, moderation buttons, reports, live tunables."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from app import clock
from app.models import MediaItem, Post, SecurityEvent, User
from app.services import media_check, media_items, tunables
from app.services.messaging import Effects
from tests import fake_telegram as tg
from tests.conftest import reply, send

STORAGE = "-1001111111111"
MODCHAT = "-1002222222222"
WEBHOOK = "/api/telegram/webhook"
SECRET = "hook-secret-0123456789"


# ----------------------------------------------------------------- helpers


def jpeg(color=(200, 40, 40), size=(1200, 1600), gps=True) -> bytes:
    im = Image.new("RGB", size, color)
    for x in range(0, size[0], 50):  # some detail so it is not a flat colour
        im.paste((color[2], color[0], color[1]), (x, 0, x + 10, size[1]))
    exif = Image.Exif()
    exif[0x010F] = "PhoneMaker"
    exif[0x0110] = "Phone X"
    exif[0x0132] = "2024:01:02 03:04:05"
    if gps:
        exif[0x8825] = {1: "N", 2: (36.0, 49.0, 0.0), 3: "E", 4: (0.0, 9.0, 0.0)}
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif, quality=90)
    return buf.getvalue()


def png(size=(300, 300)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture
def mx(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=SECRET,
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE, TELEGRAM_MODERATION_CHAT_ID=MODCHAT)
    hx.tg = fake
    return hx


def upload(hx, c, data: bytes, purpose: str = "idea", conversation_id: str | None = None, wait: bool = True):
    url = f"/api/uploads?purpose={purpose}" + (f"&conversation_id={conversation_id}" if conversation_id else "")
    r = c.post(url, content=data, headers={"Content-Type": "application/octet-stream"})
    if wait and r.status_code == 202:
        hx.state.pipeline.wait_idle()
    return r


def ready_upload(hx, c, data: bytes, **kw) -> str:
    r = upload(hx, c, data, **kw)
    assert r.status_code == 202, r.text
    mid = r.json()["upload"]["id"]
    st = c.get(f"/api/uploads/{mid}").json()["upload"]
    assert st["state"] == "ready", st
    return mid


def deliver(hx, update: dict):
    r = hx.client().post(WEBHOOK, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET, "X-DZ-Requested": ""})
    assert r.status_code == 200, r.text
    hx.state.bot.wait_idle()
    hx.state.pipeline.wait_idle()


def tick(hx) -> dict:
    effects = Effects()
    with hx.db() as db:
        out = media_items.tick(db, hx.settings, effects)
    hx.state.dispatch(effects)
    hx.state.pipeline.wait_idle()
    return out


def tmp_files(hx) -> list:
    d = hx.state.pipeline.tmp
    return list(d.iterdir()) if d.exists() else []


def all_text(*responses) -> str:
    return " ".join(r.text for r in responses)


# ----------------------------------------------------------------- the pipeline


def test_idea_picture_end_to_end_metadata_stripped_and_stored_in_telegram(mx):
    a, b = mx.user(), mx.user()
    cfg = a.get("/api/uploads/config").json()
    assert cfg["available"] is True and cfg["idea"]["remaining"] == 1 and "jpeg" in cfg["image_types"]
    mid = ready_upload(mx, a, jpeg())
    assert tmp_files(mx) == []  # nothing left in the temp area
    assert len(mx.tg.documents) == 1
    doc = mx.tg.documents[0]
    assert doc["chat_id"] == STORAGE and doc["mime"] == "image/webp"
    stored = Image.open(io.BytesIO(doc["content"]))
    assert stored.format == "WEBP" and max(stored.size) <= mx.settings.IMAGE_MAX_SIDE
    assert not stored.getexif() and "exif" not in stored.info and "xmp" not in stored.info  # no GPS/device/date
    assert b"PhoneMaker" not in doc["content"] and b"Phone X" not in doc["content"]

    r = a.post("/api/posts", json={"content": "صورة من غروب اليوم", "media_id": mid})
    assert r.status_code == 201, r.text
    post = r.json()
    assert post["media"]["url"].startswith(f"/media/{mid}/img?")
    mx.state.pipeline.wait_idle()
    # the moderators receive a copy with the context and the buttons
    copy = mx.tg.copies[-1]
    assert copy["chat_id"] == MODCHAT and copy["from_chat_id"] == STORAGE and copy["message_id"] == doc["message_id"]
    assert "صورة مع فكرة" in copy["caption"] and "صورة من غروب اليوم" in copy["caption"]
    assert a.get("/api/me").json()["public_id"] in copy["caption"]
    datas = [btn["callback_data"] for row in copy["reply_markup"]["inline_keyboard"] for btn in row]
    assert datas == [f"md:del:{mid}", f"md:ban:{mid}"]

    # another user sees it in the feed and can load the picture with their own signed URL
    b.get("/api/posts/feed")
    feed = b.get("/api/posts/feed").json()
    shown = next(p for p in feed["posts"] if p["id"] == post["id"])
    img = b.get(shown["media"]["url"])
    assert img.status_code == 200 and img.headers["content-type"] == "image/webp"
    assert a.get(shown["media"]["url"]).status_code == 403  # bound to b's session
    # Telegram ids never reach a client
    assert doc["file_id"] not in all_text(r, b.get("/api/posts/feed"), a.get(f"/api/uploads/{mid}"))

    # one picture per 24 h, with the time of the next one
    assert upload(mx, a, jpeg()).status_code == 429
    cfg = a.get("/api/uploads/config").json()["idea"]
    assert cfg["remaining"] == 0 and cfg["next_at"]
    clock.advance(24 * 3600 + 5)
    assert upload(mx, a, jpeg((10, 200, 10))).status_code == 202


@pytest.mark.parametrize("data,expected", [
    (b"just some text pretending to be a jpg" * 10, "ليس صورة"),
    (b"<html><script>alert(1)</script></html>" * 5, "ليس صورة"),
    (b"\xff\xd8\xff\xe0" + b"\x00" * 400, "غير صالحة"),  # JPEG magic, broken body
    (b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400, "غير مسموح"),  # a video where a picture is expected
])
def test_fake_or_wrong_files_are_refused_before_telegram(mx, data, expected):
    a = mx.user()
    r = upload(mx, a, data)
    assert r.status_code == 202
    st = a.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]
    assert st["state"] == "rejected" and expected in st["error"]
    assert mx.tg.documents == [] and tmp_files(mx) == []
    assert a.post("/api/posts", json={"content": "x", "media_id": st["id"]}).status_code == 400


def test_decompression_bomb_and_tiny_and_oversize(mx, make_harness):
    a = mx.user()
    bomb = io.BytesIO()
    Image.new("1", (15000, 15000)).save(bomb, "PNG")  # 225 Mpx in a few hundred KB
    r = upload(mx, a, bomb.getvalue())
    st = a.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]
    assert st["state"] == "rejected" and "كبيرة" in st["error"]
    r = upload(mx, a, png((20, 20)))
    assert a.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]["error"] == "الصورة صغيرة جدًا."
    big = b"\xff\xd8\xff" + b"0" * int(mx.settings.UPLOAD_IMAGE_MAX_MB * 1024 * 1024 + 10)
    r = upload(mx, a, big, wait=False)
    assert r.status_code == 413  # refused from the declared size, before reading the body
    assert mx.tg.documents == [] and tmp_files(mx) == []


def test_upload_needs_session_csrf_and_valid_purpose(mx):
    a = mx.user()
    anon = mx.client()
    assert upload(mx, anon, png()).status_code == 401
    r = a.post("/api/uploads?purpose=idea", content=png(), headers={"X-DZ-Requested": ""})
    assert r.status_code == 403  # CSRF header missing
    assert upload(mx, a, png(), purpose="video").status_code == 400
    assert a.get("/api/uploads/" + "a" * 32).status_code == 404
    other = ready_upload(mx, a, png())
    b = mx.user()
    assert b.get(f"/api/uploads/{other}").status_code == 404  # someone else's upload
    assert b.post("/api/posts", json={"content": "x", "media_id": other}).status_code == 404


def test_uploads_unavailable_without_storage_channel(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID))
    a = hx.user()
    assert a.get("/api/uploads/config").json()["available"] is False
    assert upload(hx, a, png()).status_code == 503
    no_bot = make_harness()
    assert upload(no_bot, no_bot.user(), png()).status_code == 503


def test_nsfw_refused_on_server_before_telegram(mx, monkeypatch):
    monkeypatch.setattr(media_check, "nsfw_scores", lambda images: [
        {"drawing": 0.0, "hentai": 0.1, "neutral": 0.05, "porn": 0.8, "sexy": 0.05} for _ in images])
    a = mx.user()
    r = upload(mx, a, jpeg())
    st = a.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]
    assert st["state"] == "rejected" and "إرشادات المجتمع" in st["error"]
    assert mx.tg.documents == [] and tmp_files(mx) == []
    with mx.db() as db:
        assert db.query(SecurityEvent).filter_by(type="media_rejected_nsfw").count() == 1
    # the check can be switched off from the panel (live)
    with mx.db() as db:
        tunables.save(db, mx.settings, {"SERVER_NSFW_CHECK": False}, "owner")
    mx.state.reload_tunables()
    assert a.get(f"/api/uploads/{ready_upload(mx, a, jpeg())}").json()["upload"]["state"] == "ready"


def test_real_nsfw_model_scores_and_speed():
    neutral = Image.new("RGB", (640, 480), (90, 140, 200))
    scores = media_check.nsfw_scores([neutral, neutral])
    assert len(scores) == 2 and abs(sum(scores[0].values()) - 1.0) < 1e-3
    worst, refused = media_check.nsfw_verdict(scores, 0.7, 0.92)
    assert worst < 0.5 and not refused
    import time

    started = time.perf_counter()
    media_check.nsfw_scores([neutral] * 8)
    assert (time.perf_counter() - started) / 8 < 0.25  # CPU, one thread: a few ms per picture


def test_sniff_detects_types_by_content():
    assert media_check.sniff(jpeg()[:64]) == ("image", "jpeg")
    assert media_check.sniff(png()[:64]) == ("image", "png")
    assert media_check.sniff(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ("image", "webp")
    assert media_check.sniff(b"\x00\x00\x00\x18ftypheic\x00\x00\x00\x00mif1heic") == ("image", "heic")
    assert media_check.sniff(b"\x00\x00\x00\x18ftypqt  \x00\x00\x00\x00qt  ") == ("video", "mov")
    assert media_check.sniff(b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2") == ("video", "mp4")
    assert media_check.sniff(b"\x1a\x45\xdf\xa3\x01\x00") == ("video", "webm")
    assert media_check.sniff(b"\x00\x00\x00\x1cftypavif\x00\x00\x00\x00avifmif1") is None
    assert media_check.sniff(b"GIF89a....") is None and media_check.sniff(b"%PDF-1.7") is None


def test_videos_are_refused_everywhere(tmp_path, mx):
    """V6: videos left with Reels — the worker refuses them even if a job claims a video, nothing is left behind,
    and no purpose accepts one."""
    tmp = tmp_path / "work"
    tmp.mkdir()
    clip = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + b"\x00" * 400
    for kind in ("video", "image"):
        (tmp / f"{'b' * 32}.in").write_bytes(clip)
        res = media_check.run_job(mx.settings, tmp, {"id": "b" * 32, "kind": kind, "types": ["mp4", "jpeg"]})
        assert not res["ok"] and res["code"] == "bad_type" and list(tmp.iterdir()) == []
    a = mx.user()
    assert upload(mx, a, clip, purpose="reel").status_code == 400
    assert "video_types" not in a.get("/api/uploads/config").json()


# ----------------------------------------------------------------- moderation


def test_telegram_buttons_delete_and_ban_from_the_moderation_group(mx):
    a, b = mx.user(), mx.user()
    mid = ready_upload(mx, a, jpeg())
    post = a.post("/api/posts", json={"content": "", "media_id": mid}).json()
    assert post["content"] == "" and post["media"]
    mx.state.pipeline.wait_idle()
    url = post["media"]["url"]
    b.get("/api/posts/feed")
    burl = next(p for p in b.get("/api/posts/feed").json()["posts"] if p["id"] == post["id"])["media"]["url"]
    assert b.get(burl).status_code == 200
    # a stranger's chat cannot press the buttons
    deliver(mx, tg.callback(f"md:del:{mid}", sender=tg.STRANGER_ID))
    assert b.get(burl).status_code == 200
    # a member of the moderation group can
    deliver(mx, tg.callback(f"md:ban:{mid}", sender=777, chat=int(MODCHAT)))
    assert b.get(burl).status_code == 404 and a.get(url).status_code in (401, 404)
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        assert item.state == "removed" and item.removed_by == "telegram:777" and item.tg_file_id is None
        assert db.get(Post, post["id"]).status == "removed"
        owner = db.query(User).filter_by(id=item.owner_id).one()
        assert owner.status == "banned"
        from app.models import AdminAuditLog

        assert db.query(AdminAuditLog).filter_by(action="media_ban", actor="telegram:777").count() == 1
    stored = mx.tg.documents[0]
    assert (STORAGE, stored["message_id"]) in mx.tg.deleted  # gone from the storage channel too
    assert any(e["method"] == "editMessageReplyMarkup" for e in mx.tg.edited)


def test_idea_picture_waits_for_approval_when_required(mx):
    with mx.db() as db:
        tunables.save(db, mx.settings, {"IDEA_IMAGE_REQUIRE_APPROVAL": True}, "owner")
    mx.state.reload_tunables()
    a, b = mx.user(), mx.user()
    mid = ready_upload(mx, a, jpeg())
    post = a.post("/api/posts", json={"content": "تحتاج موافقة", "media_id": mid}).json()
    assert post["status"] == "pending" and post["media"]["review"] == "pending"
    mx.state.pipeline.wait_idle()
    datas = [btn["callback_data"] for row in mx.tg.copies[-1]["reply_markup"]["inline_keyboard"] for btn in row]
    assert f"md:ok:{mid}" in datas and f"md:no:{mid}" in datas
    assert b.get(f"/api/posts/{post['id']}").status_code == 404
    deliver(mx, tg.callback(f"md:ok:{mid}"))  # the admin's private chat works too
    assert b.get(f"/api/posts/{post['id']}").status_code == 200


def test_reports_hide_after_threshold_and_minor_report(mx):
    with mx.db() as db:
        tunables.save(db, mx.settings, {"REPORT_AUTO_HIDE_THRESHOLD": 2}, "owner")
    mx.state.reload_tunables()
    a, b, c, d = mx.user(), mx.user(), mx.user(), mx.user()
    mid = ready_upload(mx, a, jpeg())
    post = a.post("/api/posts", json={"content": "x y z", "media_id": mid}).json()
    mx.state.pipeline.wait_idle()
    assert b.post(f"/api/posts/{post['id']}/report", json={"reason": "inappropriate"}).status_code == 201
    mx.state.pipeline.wait_idle()
    assert "بلاغ" in mx.tg.copies[-1]["caption"]
    assert d.get(f"/api/posts/{post['id']}").status_code == 200  # one report: still visible
    assert c.post(f"/api/posts/{post['id']}/report", json={"reason": "spam"}).status_code == 201
    assert d.get(f"/api/posts/{post['id']}").status_code == 404  # hidden until reviewed
    with mx.db() as db:
        assert db.get(MediaItem, mid).legal_hold is True
    deliver(mx, tg.callback(f"md:keep:{mid}"))
    assert d.get(f"/api/posts/{post['id']}").status_code == 200
    # a "minor" report hides at once and asks the moderators to confirm
    assert d.post(f"/api/posts/{post['id']}/report", json={"reason": "minor"}).status_code == 201
    mx.state.pipeline.wait_idle()
    assert b.get(f"/api/posts/{post['id']}").status_code == 404
    assert "قاصر" in mx.tg.copies[-1]["caption"]
    deliver(mx, tg.callback(f"md:minor:{mid}"))
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        assert item.state == "removed" and item.legal_hold is True and item.tg_file_id  # evidence kept
        assert db.get(User, item.owner_id).status == "banned"
    stored = mx.tg.documents[0]
    assert (STORAGE, stored["message_id"]) not in mx.tg.deleted


def test_owner_deleting_the_idea_deletes_the_picture(mx):
    a = mx.user()
    mid = ready_upload(mx, a, jpeg())
    post = a.post("/api/posts", json={"content": "سأحذفها", "media_id": mid}).json()
    mx.state.pipeline.wait_idle()
    assert a.delete(f"/api/posts/{post['id']}").status_code == 200
    mx.state.pipeline.wait_idle()
    assert (STORAGE, mx.tg.documents[0]["message_id"]) in mx.tg.deleted
    assert not list(mx.state.media.dir.glob(f"{mid}*"))


def test_unused_upload_is_purged(mx):
    a = mx.user()
    mid = ready_upload(mx, a, jpeg())
    clock.advance(3 * 3600)
    assert tick(mx)["orphans"] == 1
    with mx.db() as db:
        assert db.get(MediaItem, mid).state == "removed"
    assert (STORAGE, mx.tg.documents[0]["message_id"]) in mx.tg.deleted


# ----------------------------------------------------------------- chat pictures


def _chat(mx):
    a, b = mx.user(), mx.user()
    cid = send(a, b, "مرحبا، كيف الحال؟").json()["conversation"]["id"]
    return a, b, cid


def test_chat_picture_after_reply_blurred_open_countdown_then_gone_everywhere(mx):
    a, b, cid = _chat(mx)
    assert upload(mx, a, jpeg(), purpose="chat", conversation_id=cid).status_code == 403  # b has not replied yet
    assert reply(b, cid, "أهلا").status_code == 201
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    stranger = mx.user()
    assert stranger.post(f"/api/conversations/{cid}/media", json={"media_id": mid}).status_code == 404
    r = a.post(f"/api/conversations/{cid}/media", json={"media_id": mid, "client_id": "img-000000001"})
    assert r.status_code == 201, r.text
    msg = r.json()["message"]
    assert msg["kind"] == "image" and msg["media"]["state"] == "sealed" and "url" not in msg["media"]
    conv = b.get(f"/api/conversations/{cid}").json()
    got = next(m for m in conv["messages"] if m["id"] == msg["id"])
    assert got["media"]["state"] == "sealed" and got["media"]["blur"].startswith("data:image/jpeg;base64,")
    assert "url" not in json.dumps(got["media"])
    assert b.get("/api/conversations").json()["conversations"][0]["last_message"]["preview"] == "📷 صورة"
    # only the recipient can open it; nothing in the moderation chat (not reported)
    assert a.post(f"/api/messages/{msg['id']}/open").status_code == 404
    assert stranger.post(f"/api/messages/{msg['id']}/open").status_code == 404
    opened = b.post(f"/api/messages/{msg['id']}/open").json()
    assert opened["seconds_left"] == mx.settings.CHAT_IMAGE_TTL_AFTER_VIEW and opened["secure"] is True
    img = b.get(opened["url"])
    assert img.status_code == 200 and img.headers["cache-control"] == "no-store"
    assert mx.tg.copies == []
    a_view = next(m for m in a.get(f"/api/conversations/{cid}").json()["messages"] if m["id"] == msg["id"])
    assert a_view["media"]["state"] == "open" and a_view["media"]["seconds_left"] > 100  # the sender sees the countdown
    # re-opening does not restart the countdown
    clock.advance(60)
    assert b.post(f"/api/messages/{msg['id']}/open").json()["seconds_left"] <= 60
    clock.advance(61)
    assert b.get(opened["url"]).status_code == 404  # dead the moment it expires
    assert b.post(f"/api/messages/{msg['id']}/open").status_code == 410
    for c in (a, b):
        m = next(m for m in c.get(f"/api/conversations/{cid}").json()["messages"] if m["id"] == msg["id"])
        assert m["media"] == {"kind": "image", "state": "expired", "blur": None}
    assert tick(mx)["expired"] == 1
    assert not list(mx.state.media.dir.glob(f"{mid}*"))  # server cache emptied at once
    assert mx.tg.deleted == []  # storage copy waits for the short report window
    clock.advance(mx.settings.CHAT_IMAGE_REPORT_GRACE + 1)
    tick(mx)
    assert (STORAGE, mx.tg.documents[0]["message_id"]) in mx.tg.deleted
    with mx.db() as db:
        item = db.get(MediaItem, mid)
        assert item.state == "expired" and item.tg_file_id is None and item.tg_message_id is None


def test_unopened_chat_picture_expires_and_reported_one_is_kept(mx):
    a, b, cid = _chat(mx)
    reply(b, cid, "نعم")
    m1 = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    msg1 = a.post(f"/api/conversations/{cid}/media", json={"media_id": m1}).json()["message"]
    m2 = ready_upload(mx, a, jpeg((0, 0, 0)), purpose="chat", conversation_id=cid)
    msg2 = a.post(f"/api/conversations/{cid}/media", json={"media_id": m2}).json()["message"]
    opened = b.post(f"/api/messages/{msg2['id']}/open").json()
    assert b.post(f"/api/messages/{msg2['id']}/report", json={"reason": "harassment"}).status_code == 201
    mx.state.pipeline.wait_idle()
    assert mx.tg.copies[-1]["chat_id"] == MODCHAT and "صورة محادثة" in mx.tg.copies[-1]["caption"]
    clock.advance(mx.settings.CHAT_IMAGE_UNOPENED_TTL + 1)
    assert b.get(opened["url"]).status_code in (403, 404)  # signature expired / file gone
    tick(mx)
    clock.advance(mx.settings.CHAT_IMAGE_REPORT_GRACE + 1)
    tick(mx)
    docs = {d["file_id"]: d for d in mx.tg.documents}
    with mx.db() as db:
        i1, i2 = db.get(MediaItem, m1), db.get(MediaItem, m2)
        assert i1.state == i2.state == "expired"
        assert i2.legal_hold is True and i2.tg_file_id in docs  # reported: kept as evidence
    deleted_ids = {mid for _chat_id, mid in mx.tg.deleted}
    assert docs[next(f for f in docs if docs[f]["message_id"] not in deleted_ids)]["message_id"]  # one kept
    assert len(mx.tg.deleted) == 1
    assert msg1["id"] != msg2["id"]


def test_chat_picture_limits_and_switch(mx):
    a, b, cid = _chat(mx)
    reply(b, cid, "تفضل")
    with mx.db() as db:
        tunables.save(db, mx.settings, {"CHAT_IMAGE_PER_HOUR": 1}, "owner")
    mx.state.reload_tunables()
    mid = ready_upload(mx, a, jpeg(), purpose="chat", conversation_id=cid)
    assert a.post(f"/api/conversations/{cid}/media", json={"media_id": mid}).status_code == 201
    assert upload(mx, a, jpeg(), purpose="chat", conversation_id=cid).status_code == 429
    with mx.db() as db:
        tunables.save(db, mx.settings, {"CHAT_IMAGES_ENABLED": False}, "owner")
    mx.state.reload_tunables()
    assert upload(mx, b, jpeg(), purpose="chat", conversation_id=cid).status_code == 403
    # blocked conversation: no pictures
    with mx.db() as db:
        tunables.save(db, mx.settings, {"CHAT_IMAGES_ENABLED": True}, "owner")
    mx.state.reload_tunables()
    assert b.post(f"/api/conversations/{cid}/block").status_code in (200, 204)
    assert upload(mx, b, jpeg(), purpose="chat", conversation_id=cid).status_code in (403, 404)


# ----------------------------------------------------------------- tunables


def test_tunables_are_validated_applied_live_and_reset(mx):
    a = mx.user()
    with mx.db() as db:
        with pytest.raises(Exception):
            tunables.save(db, mx.settings, {"IDEA_IMAGE_LIMIT_PER_24H": -1}, "owner")
        with pytest.raises(Exception):
            tunables.save(db, mx.settings, {"TELEGRAM_BOT_TOKEN": "x"}, "owner")  # secrets are never tunables
        with pytest.raises(Exception):
            tunables.save(db, mx.settings, {"TELEGRAM_STORAGE_CHANNEL_ID": "abc"}, "owner")
        tunables.save(db, mx.settings, {"IDEA_IMAGE_LIMIT_PER_24H": 3}, "owner")
    mx.state.on_system_event({"type": "tunables"})  # what every instance does on the hub broadcast
    assert a.get("/api/uploads/config").json()["idea"]["limit"] == 3
    with mx.db() as db:
        rows = {r["key"]: r for r in tunables.listing(db, mx.settings)}
        assert rows["IDEA_IMAGE_LIMIT_PER_24H"]["overridden"] and rows["IDEA_IMAGE_LIMIT_PER_24H"]["default"] == 1
        tunables.save(db, mx.settings, {"IDEA_IMAGE_LIMIT_PER_24H": None}, "owner")
    mx.state.reload_tunables()
    assert a.get("/api/uploads/config").json()["idea"]["limit"] == 1
    assert mx.state.bot.settings.TELEGRAM_MODERATION_CHAT_ID == MODCHAT  # the bot's copy follows too


# ----------------------------------------------------------------- queue mode (separate worker container)


def test_queue_mode_with_a_separate_worker(make_harness):
    import os
    import threading

    url = os.environ.get("DZ_TEST_REDIS_URL")
    if not url:
        pytest.skip("needs DZ_TEST_REDIS_URL (the worker talks to the app only through Redis)")
    from app.media_worker import serve
    from app.services.media_pipeline import HEARTBEAT

    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE, MEDIA_WORKER="queue", MEDIA_WORKER_TIMEOUT=60)
    hx.tg = fake
    a = hx.user()
    hx.state.pipeline.redis().delete(HEARTBEAT)
    assert upload(hx, a, jpeg()).status_code == 503  # no worker: refuse instead of queueing forever
    worker_settings = hx.settings.model_copy(update={"TELEGRAM_BOT_TOKEN": "", "DATABASE_URL": "sqlite://"})
    stop = threading.Event()
    t = threading.Thread(target=serve, args=(worker_settings, stop), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if hx.state.pipeline.worker_alive():
                break
            import time

            time.sleep(0.05)
        mid = ready_upload(hx, a, jpeg())
        assert fake.documents[0]["chat_id"] == STORAGE and tmp_files(hx) == []
        r = upload(hx, a, b"not an image at all" * 20)
        st = a.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]
        assert st["state"] == "rejected" and len(fake.documents) == 1 and mid
    finally:
        stop.set()
        t.join(timeout=15)
