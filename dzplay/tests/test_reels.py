"""Reels: Telegram uploads (fake Bot API), media pipeline, signed URLs, cache, ranking,
reactions, public comments — and Ideas comments staying owner-only."""

from __future__ import annotations

import io
import logging
import subprocess
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app import clock
from app.config import Settings
from app.models import MediaCacheEntry, Reel, ReelAsset, ReelView, SecurityEvent
from app.services import reels as reels_service
from app.services import reels_ranking
from tests import fake_telegram as tg
from tests.conftest import ffmpeg_binary

WEBHOOK = "/api/telegram/webhook"
SECRET = "hook-secret-0123456789"


# ----------------------------------------------------------------- fixtures


@pytest.fixture(scope="session")
def media_files(tmp_path_factory) -> dict[str, bytes]:
    d = tmp_path_factory.mktemp("media")
    ff = ffmpeg_binary()

    def make(name: str, args: list[str]) -> bytes:
        out = d / name
        subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *args, str(out)], check=True, timeout=120)
        return out.read_bytes()

    small = make("small.mp4", ["-f", "lavfi", "-i", "testsrc=size=360x640:rate=25", "-f", "lavfi", "-i", "sine=frequency=440",
                               "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-b:v", "300k", "-c:a", "aac",
                               "-shortest"])
    big = make("big.mp4", ["-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=25", "-f", "lavfi", "-i", "sine=frequency=330",
                           "-t", "2", "-c:v", "mpeg4", "-q:v", "2", "-c:a", "aac", "-shortest"])
    from PIL import Image

    def jpeg(color, with_gps=False) -> bytes:
        im = Image.new("RGB", (1200, 1600), color)
        exif = Image.Exif()
        exif[0x010F] = "PhoneMaker"  # Make
        if with_gps:
            exif[0x8825] = {1: "N", 2: (36.0, 49.0, 0.0), 3: "E", 4: (0.0, 9.0, 0.0)}
        buf = io.BytesIO()
        im.save(buf, "JPEG", exif=exif)
        return buf.getvalue()

    return {"small": small, "big": big, "red": jpeg((200, 40, 40), True), "green": jpeg((40, 200, 40)),
            "blue": jpeg((40, 40, 200))}


@pytest.fixture
def bot(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=SECRET,
                      TELEGRAM_ALBUM_SETTLE_SECONDS=0.3)
    hx.tg = fake
    return hx


def deliver(hx, update: dict, secret: str = SECRET, wait: bool = True):
    r = hx.client().post(WEBHOOK, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret, "X-DZ-Requested": ""})
    if wait:
        hx.state.bot.wait_idle()
    return r


def upload_video(hx, data: bytes, caption: str = "وصف المقطع") -> Reel:
    fid = hx.tg.add_file(data, name="vid")
    assert deliver(hx, tg.video(fid, len(data), caption)).status_code == 200
    with hx.db() as db:
        reel = db.scalar(select(Reel).order_by(Reel.created_at.desc()))
        db.expunge(reel)
    return reel


def make_reels(hx, n: int, *, age_hours=lambda i: i, status="visible") -> list[str]:
    """Reels straight in the DB (for feed/ranking tests that need no media)."""
    ids = []
    with hx.db() as db:
        for i in range(n):
            r = reels_service.create_reel(db, hx.settings, "video", f"reel {i}")
            r.status = status
            r.created_at = clock.utcnow() - timedelta(hours=age_hours(i))
            db.add(ReelAsset(reel_id=r.id, kind="video", tg_file_id=f"x{i}", position=0))
            ids.append(r.id)
    return ids


def feed_ids(c, **params) -> tuple[list[str], str | None]:
    data = c.get("/api/reels/feed", params=params).json()
    return [r["id"] for r in data["reels"]], data["next_cursor"]


# ----------------------------------------------------------------- webhook security


def test_webhook_requires_secret_and_bot_enabled(bot, hx):
    assert hx.client().post(WEBHOOK, json={}, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}).status_code == 404
    c = bot.client()
    assert c.post(WEBHOOK, json=tg.text("/list")).status_code == 403  # no header (and no CSRF header needed)
    assert c.post(WEBHOOK, json=tg.text("/list"), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert bot.tg.sent == []
    assert deliver(bot, tg.text("/list")).status_code == 200
    assert "لا يوجد محتوى" in bot.tg.last_text()


def test_strangers_are_ignored_silently_and_logged(bot, media_files):
    fid = bot.tg.add_file(media_files["small"])
    deliver(bot, tg.video(fid, len(media_files["small"]), sender=tg.STRANGER_ID))
    deliver(bot, tg.text("/delete abc", sender=tg.STRANGER_ID))
    deliver(bot, tg.text("/list", sender=tg.STRANGER_ID, chat=tg.ADMIN_ID))  # spoofed chat, wrong sender
    deliver(bot, tg.callback("rgen:abc", sender=tg.STRANGER_ID))
    assert bot.tg.sent == [] and bot.tg.downloads == []
    with bot.db() as db:
        assert db.scalar(select(Reel.id)) is None
        events = db.scalars(select(SecurityEvent).where(SecurityEvent.type == "telegram_unauthorized")).all()
        assert len(events) == 4 and str(tg.STRANGER_ID) not in " ".join(e.detail for e in events)


# ----------------------------------------------------------------- uploads + media pipeline


def test_video_upload_is_remuxed_with_faststart_and_served_with_range(bot, media_files):
    reel = upload_video(bot, media_files["small"], "أول مقطع على DZPLAY")
    assert reel.status == "visible" and reel.kind == "video" and reel.caption == "أول مقطع على DZPLAY"
    assert f"نُشر Reel {reel.short_id}" in bot.tg.last_text()

    u = bot.user()
    data = u.get("/api/reels/feed").json()
    item = data["reels"][0]
    assert item["caption"] == "أول مقطع على DZPLAY" and item["media"][0]["type"] == "video"
    assert "telegram" not in str(data).lower() and tg.TOKEN not in str(data)

    video = u.get(item["media"][0]["src"])
    assert video.status_code == 200 and video.headers["content-type"] == "video/mp4"
    body = video.content
    assert body.find(b"moov") < body.find(b"mdat")  # faststart: playback starts before the whole file arrives
    part = u.get(item["media"][0]["src"], headers={"Range": "bytes=0-1023"})
    assert part.status_code == 206 and len(part.content) == 1024 and part.headers["content-range"].startswith("bytes 0-1023/")
    poster = u.get(item["media"][0]["poster"])
    assert poster.status_code == 200 and poster.content[:3] == b"\xff\xd8\xff"
    assert bot.tg.downloads.count(bot.tg.downloads[0]) == 1  # prepared once at upload, then served from cache


def test_large_or_foreign_codec_video_is_transcoded_to_720p_h264(bot, media_files):
    reel = upload_video(bot, media_files["big"])
    assert reel.status == "visible"
    with bot.db() as db:
        asset = db.scalar(select(ReelAsset).where(ReelAsset.reel_id == reel.id))
        assert (asset.width, asset.height) == (1280, 720)  # landscape source → 1280x720 box
        path = bot.state.media.path_for(asset.id, "mp4")
    import av

    with av.open(str(path)) as c:
        assert c.streams.video[0].codec_context.name == "h264"
        assert c.streams.audio[0].codec_context.name == "aac"


def test_album_becomes_one_image_reel_in_order_without_exif(bot, media_files):
    ids = [bot.tg.add_file(media_files[c], name=c) for c in ("red", "green", "blue")]
    # Telegram delivers album items as separate updates; the caption is on the first.
    deliver(bot, tg.photo(ids[0], 5000, caption="ألبوم ثلاث صور", group="g1", message_id=501), wait=False)
    deliver(bot, tg.photo(ids[2], 5000, group="g1", message_id=503), wait=False)
    deliver(bot, tg.photo(ids[1], 5000, group="g1", message_id=502), wait=False)
    bot.state.bot.wait_idle()
    with bot.db() as db:
        rows = db.scalars(select(Reel)).all()
        assert len(rows) == 1 and rows[0].kind == "images" and rows[0].status == "visible"
    assert sum("نُشر" in t for t in bot.tg.texts()) == 1  # one confirmation for the whole album

    u = bot.user()
    item = u.get("/api/reels/feed").json()["reels"][0]
    assert item["caption"] == "ألبوم ثلاث صور" and len(item["media"]) == 3
    from PIL import Image

    colors = []
    for m in item["media"]:
        r = u.get(m["src"])
        assert r.status_code == 200 and r.headers["content-type"] == "image/webp"
        im = Image.open(io.BytesIO(r.content))
        assert not im.info.get("exif") and b"Exif" not in r.content and b"PhoneMaker" not in r.content
        assert max(im.size) <= bot.settings.IMAGE_MAX_SIDE
        colors.append(max(range(3), key=lambda ch: im.getpixel((10, 10))[ch]))
    assert colors == [0, 1, 2]  # red, green, blue — the order they were sent in


def test_file_over_telegram_limit_is_refused_with_a_clear_message(bot, media_files):
    fid = bot.tg.add_file(media_files["small"])
    deliver(bot, tg.video(fid, 25 * 1024 * 1024))
    assert "25.0MB" in bot.tg.last_text() and "20MB" in bot.tg.last_text()
    with bot.db() as db:
        assert db.scalar(select(Reel.id)) is None
    # Size unknown in the update but getFile reports it too big → the reel fails, admin is told.
    fid2 = bot.tg.add_file(media_files["small"], reported_size=30 * 1024 * 1024)
    deliver(bot, tg.video(fid2, 0))
    with bot.db() as db:
        assert db.scalar(select(Reel.status)) == "failed"
    assert "لم يُنشر" in bot.tg.last_text()


def test_broken_file_fails_cleanly(bot):
    fid = bot.tg.add_file(b"this is not a video at all", name="junk")
    deliver(bot, tg.video(fid, 30))
    with bot.db() as db:
        assert db.scalar(select(Reel.status)) == "failed"
    assert "لم يُنشر" in bot.tg.last_text()
    assert feed_ids(bot.user())[0] == []


# ----------------------------------------------------------------- bot commands


def test_bot_commands_manage_reels(bot, media_files):
    reel = upload_video(bot, media_files["small"])
    sid = reel.short_id
    u = bot.user()
    deliver(bot, tg.text("/list"))
    assert sid in bot.tg.last_text() and "ظاهر" in bot.tg.last_text()

    deliver(bot, tg.text(f"/hide {sid}"))
    assert "أُخفي" in bot.tg.last_text() and feed_ids(u)[0] == []
    src = None
    deliver(bot, tg.text(f"/show {sid}"))
    assert feed_ids(u)[0] == [reel.id]
    src = u.get("/api/reels/feed").json()["reels"][0]["media"][0]["src"]
    deliver(bot, tg.text(f"/caption {sid} وصف جديد"))
    assert u.get("/api/reels/feed").json()["reels"][0]["caption"] == "وصف جديد"
    deliver(bot, tg.text(f"/pin {sid} 5"))
    assert "ثُبّت" in bot.tg.last_text()
    deliver(bot, tg.text("/stats"))
    assert "Reels: 1 ظاهر" in bot.tg.last_text()
    deliver(bot, tg.text("/nonsense"))
    assert "أمر غير معروف" in bot.tg.last_text()
    deliver(bot, tg.text("/hide zzzzzz"))
    assert "لا يوجد محتوى" in bot.tg.last_text()

    deliver(bot, tg.text(f"/delete {sid}"))
    assert "حُذف" in bot.tg.last_text() and feed_ids(u)[0] == []
    assert u.get(src).status_code == 404
    with bot.db() as db:
        assert db.scalar(select(MediaCacheEntry.key)) is None
    assert not any(Path(bot.settings.MEDIA_CACHE_DIR).glob("*.mp4"))
    audit = bot.admin().get("/api/admin/audit").json()["entries"]
    assert {"reel_hide", "reel_show", "reel_delete", "reel_pin", "reel_published"} <= {e["action"] for e in audit}
    assert all(e["actor"] in ("telegram", "owner") for e in audit)


def test_token_never_reaches_logs_or_responses(bot, media_files, caplog):
    caplog.set_level(logging.DEBUG)
    bot.tg.fail_methods.add("sendMessage")  # every reply fails → errors get logged
    upload_video(bot, media_files["small"])
    deliver(bot, tg.text("/list"))
    assert caplog.records, "expected some log output"
    assert tg.TOKEN not in caplog.text and tg.TOKEN.split(":")[1] not in caplog.text


# ----------------------------------------------------------------- signed media URLs


def test_media_urls_are_signed_short_lived_and_session_bound(bot, media_files):
    upload_video(bot, media_files["small"])
    a, b = bot.user(), bot.user()
    src = a.get("/api/reels/feed").json()["reels"][0]["media"][0]["src"]
    assert a.get(src).status_code == 200
    assert b.get(src).status_code == 403  # another session cannot reuse my link
    assert bot.client().get(src).status_code == 403  # nor someone not signed in
    tampered = src[:-1] + ("0" if src[-1] != "0" else "1")
    assert a.get(tampered).status_code == 403
    asset_id = src.split("/")[2]
    for bad in (f"/media/{asset_id}/../../app/config.py", f"/media/..%2f..%2fapp/mp4?e=1&s={'0' * 40}",
                f"/media/{asset_id}/source?e=1&s={'0' * 40}", f"/media/{asset_id}/mp4"):
        assert a.get(bad).status_code in (403, 404), bad
    clock.advance(bot.settings.MEDIA_URL_TTL + 5)
    assert a.get(src).status_code == 403  # expired
    a.post("/api/auth/logout")
    assert a.get(a.get("/api/reels/feed").json().get("reels", [{}])[0].get("media", [{}])[0].get("src", src)).status_code in (401, 403)


# ----------------------------------------------------------------- disk cache


def test_cache_lru_size_cap_ttl_and_refetch(bot, media_files):
    r1 = upload_video(bot, media_files["small"], "one")
    clock.advance(600)
    upload_video(bot, media_files["small"], "two")
    store = bot.state.media
    with bot.db() as db:
        total = store.total_size(db)
    assert total > 0
    # Cap the cache just under its current size: the least recently used reel goes first.
    bot.state.media.settings = Settings(**{**bot.settings.model_dump(), "MEDIA_CACHE_MAX_GB": (total - 1) / 1024 ** 3})
    with bot.db() as db:
        removed = store.purge(db)
        assert removed["lru"] >= 1
        assets = {a.reel_id: a for a in db.scalars(select(ReelAsset)).all()}
        assert not store.path_for(assets[r1.id].id, "mp4").exists()
        assert store.total_size(db) <= total - 1
    # A viewer asking for the evicted file gets it again (re-fetched from Telegram and prepared).
    before = len(bot.tg.downloads)
    u = bot.user()
    item = next(r for r in u.get("/api/reels/feed").json()["reels"] if r["id"] == r1.id)
    assert u.get(item["media"][0]["src"]).status_code == 200
    assert len(bot.tg.downloads) == before + 1

    bot.state.media.settings = bot.settings
    clock.advance(bot.settings.MEDIA_CACHE_TTL + 60)
    with bot.db() as db:
        assert store.purge(db)["expired"] >= 2
        assert store.total_size(db) == 0
    assert not any(Path(bot.settings.MEDIA_CACHE_DIR).glob("*.mp4"))


# ----------------------------------------------------------------- ranking


def test_feed_random_per_session_stable_paging_and_seen_last(hx):
    ids = make_reels(hx, 20)
    u = hx.user()
    first, cursor = feed_ids(u, limit=8)
    second, cursor2 = feed_ids(u, limit=8, cursor=cursor)
    third, cursor3 = feed_ids(u, limit=8, cursor=cursor2)
    assert cursor3 is None and sorted(first + second + third) == sorted(ids)  # no duplicates, nothing skipped
    orders = {tuple(feed_ids(u, limit=20)[0]) for _ in range(6)}
    assert len(orders) > 1  # a new session gets a new order

    for rid in first:  # watch the first page
        assert u.post(f"/api/reels/{rid}/view").status_code == 200
    clock.advance(1)
    fresh, _ = feed_ids(u, limit=20)
    assert set(fresh[-8:]) == set(first)  # seen reels only come back once the others are shown
    clock.advance(hx.settings.REELS_SEEN_TTL + 10)
    with hx.db() as db:
        assert reels_service.purge_views(db, hx.settings) == 8
        assert db.scalar(select(ReelView.id)) is None


def test_freshness_weighting_and_pinned_first():
    s = Settings(SECRET_KEY="x", REELS_FRESHNESS_HALF_LIFE_HOURS=24, REELS_OLD_MIN_WEIGHT=0.05)
    now = clock.utcnow()
    cands = [reels_ranking.Candidate(f"r{i}", now - timedelta(hours=24 * i), None) for i in range(10)]
    position = Counter()
    for seed in range(400):
        for pos, rid in enumerate(reels_ranking.order(s, cands, set(), str(seed), now)):
            position[rid] += pos
    avg = [position[f"r{i}"] / 400 for i in range(10)]
    assert avg[0] < avg[3] < avg[9]  # newer content comes earlier on average
    firsts = Counter(reels_ranking.order(s, cands, set(), str(seed), now)[0] for seed in range(400))
    assert firsts["r9"] > 0  # ...but old content still gets shown first sometimes (never disappears)
    pinned = cands + [reels_ranking.Candidate("pinned", now - timedelta(days=30), now + timedelta(hours=2))]
    assert all(reels_ranking.order(s, pinned, set(), str(seed), now)[0] == "pinned" for seed in range(50))
    expired = cands + [reels_ranking.Candidate("was", now - timedelta(days=30), now - timedelta(hours=1))]
    assert any(reels_ranking.order(s, expired, set(), str(seed), now)[0] != "was" for seed in range(50))


def test_hidden_processing_and_failed_reels_are_not_served(hx):
    make_reels(hx, 1, status="hidden")
    make_reels(hx, 1, status="processing")
    make_reels(hx, 1, status="failed")
    visible = make_reels(hx, 1)
    u = hx.user()
    assert feed_ids(u)[0] == visible
    with hx.db() as db:
        hidden = db.scalar(select(Reel.id).where(Reel.status == "hidden"))
    assert u.put(f"/api/reels/{hidden}/reaction", json={"reaction": "like"}).status_code == 404
    assert u.get(f"/api/reels/{hidden}/comments").status_code == 404
    assert hx.client().get("/api/reels/feed").status_code == 401


# ----------------------------------------------------------------- reactions


def test_reactions_one_per_user_switch_and_remove(hx):
    rid = make_reels(hx, 1)[0]
    a, b = hx.user(), hx.user()
    r = a.put(f"/api/reels/{rid}/reaction", json={"reaction": "like"}).json()
    assert r == {"likes": 1, "dislikes": 0, "my_reaction": "like"}
    assert a.put(f"/api/reels/{rid}/reaction", json={"reaction": "like"}).json()["likes"] == 1  # no double count
    assert a.put(f"/api/reels/{rid}/reaction", json={"reaction": "dislike"}).json() == {"likes": 0, "dislikes": 1, "my_reaction": "dislike"}
    b.put(f"/api/reels/{rid}/reaction", json={"reaction": "like"})
    assert a.put(f"/api/reels/{rid}/reaction", json={"reaction": None}).json() == {"likes": 1, "dislikes": 0, "my_reaction": None}
    assert a.put(f"/api/reels/{rid}/reaction", json={"reaction": "love"}).status_code == 400
    item = b.get("/api/reels/feed").json()["reels"][0]
    assert item["my_reaction"] == "like" and item["likes"] == 1


# ----------------------------------------------------------------- public comments


def test_reel_comments_are_public_paginated_deletable_reportable(make_harness):
    hx = make_harness(REEL_COMMENTS_PAGE_SIZE=3, MAX_REEL_COMMENTS_PER_MINUTE=50)
    rid = make_reels(hx, 1)[0]
    a, b, c = hx.user(), hx.user(), hx.user()
    for i in range(4):
        clock.advance(1)
        assert a.post(f"/api/reels/{rid}/comments", json={"content": f"تعليق عام {i}"}).status_code == 201
    first = c.get(f"/api/reels/{rid}/comments").json()  # a third user sees everyone's comments
    assert [x["content"] for x in first["comments"]] == ["تعليق عام 3", "تعليق عام 2", "تعليق عام 1"]
    assert first["total"] == 4 and first["next_cursor"]
    assert all(x["author"] == {"name": "dzplay", "official": False, "gender": None, "verified": False} and x["mine"] is False for x in first["comments"])
    more = c.get(f"/api/reels/{rid}/comments", params={"cursor": first["next_cursor"]}).json()
    assert [x["content"] for x in more["comments"]] == ["تعليق عام 0"] and more["next_cursor"] is None
    assert "@example.com" not in str(first) and "author_id" not in str(first)

    cid = first["comments"][0]["id"]
    assert b.delete(f"/api/reel-comments/{cid}").status_code == 404  # not yours
    assert b.post(f"/api/reel-comments/{cid}/report", json={"reason": "harassment"}).status_code == 201
    assert b.post(f"/api/reel-comments/{cid}/report", json={"reason": "harassment"}).json()["duplicate"] is True
    assert a.post(f"/api/reel-comments/{cid}/report", json={"reason": "spam"}).status_code == 400  # own comment
    reports = hx.admin().get("/api/admin/reports").json()["reports"]
    assert reports[0]["target"] == "reel_comment" and reports[0]["evidence"][0]["content"] == "تعليق عام 3"
    hx.admin().post(f"/api/admin/reports/{reports[0]['id']}/resolve", json={"action": "remove"})
    assert c.get(f"/api/reels/{rid}/comments").json()["total"] == 3

    mine = a.get(f"/api/reels/{rid}/comments").json()["comments"][0]
    assert mine["mine"] is True
    assert a.delete(f"/api/reel-comments/{mine['id']}").status_code == 200
    assert c.get(f"/api/reels/{rid}/comments").json()["total"] == 2


def test_reel_comment_limits_xss_and_blocks(make_harness):
    hx = make_harness(MAX_REEL_COMMENTS_PER_MINUTE=2, MAX_REEL_COMMENT_LENGTH=20)
    rid = make_reels(hx, 1)[0]
    a, b = hx.user(), hx.user()
    assert a.post(f"/api/reels/{rid}/comments", json={"content": "x" * 21}).status_code == 400
    assert a.post(f"/api/reels/{rid}/comments", json={"content": "<script>alert(1)</script>"}).status_code == 400
    assert a.post(f"/api/reels/{rid}/comments", json={"content": "واحد"}).status_code == 201
    assert a.post(f"/api/reels/{rid}/comments", json={"content": "اثنان"}).status_code == 201
    assert a.post(f"/api/reels/{rid}/comments", json={"content": "ثلاثة"}).status_code == 429
    # A user who blocked A (from a conversation) no longer sees A's public comments.
    from tests.conftest import send

    cid = send(a, "مرحبا").json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/block")
    assert b.get(f"/api/reels/{rid}/comments").json()["comments"] == []
    # Threatening public comments are flagged for review like private ones.
    hx2_c = hx.user()
    hx2_c.post(f"/api/reels/{rid}/comments", json={"content": "نقتلك"})
    flags = hx.admin().get("/api/admin/flags").json()["flags"]
    assert flags[0]["target"] == "reel_comment"


def test_ideas_comments_still_owner_only(hx):
    """Reels comments are public; this must never leak into Ideas comments."""
    owner, writer, other = hx.user(), hx.user(), hx.user()
    pid = owner.post("/api/posts", json={"content": "فكرة خاصة التعليقات"}).json()["id"]
    assert writer.post(f"/api/posts/{pid}/comments", json={"content": "تعليق لصاحب الفكرة فقط"}).status_code == 201
    assert owner.get(f"/api/posts/{pid}/comments").status_code == 200
    assert "تعليق لصاحب الفكرة فقط" in owner.get(f"/api/posts/{pid}/comments").text
    for c in (writer, other):
        r = c.get(f"/api/posts/{pid}/comments")
        assert r.status_code == 403 and "تعليق لصاحب الفكرة" not in r.text
    # and the Reels comment endpoints cannot reach Ideas comments
    assert other.get(f"/api/reels/{pid}/comments").status_code == 404


def test_playlist_or_concat_inputs_are_refused(tmp_path, media_files):
    """A file sent to the bot can't make ffmpeg read other local files or URLs (LFI / SSRF)."""
    from app.services import media
    from tests.conftest import ffprobe_shim

    settings = Settings(SECRET_KEY="x", FFMPEG_BINARY=ffmpeg_binary(), FFPROBE_BINARY=ffprobe_shim())
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(media_files["small"])
    assert media.probe(settings, clip)["vcodec"] == "h264"  # a real container is fine
    concat = tmp_path / "upload.mp4"
    concat.write_text(f"ffconcat version 1.0\nfile '{clip}'\n")
    hls = tmp_path / "upload2.mp4"
    hls.write_text(f"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nhttp://127.0.0.1:9/x.ts\n#EXTINF:1,\nfile://{clip}\n#EXT-X-ENDLIST\n")
    for bad in (concat, hls):
        with pytest.raises(media.MediaError):
            media.prepare_video(settings, bad, tmp_path / "out.mp4", tmp_path / "out.jpg")
        assert not (tmp_path / "out.mp4").exists()


def test_cli_registers_the_webhook_without_printing_the_token(monkeypatch, capsys):
    from app import admin_cli
    from app.config import get_settings

    fake = tg.FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", tg.TOKEN)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(tg.ADMIN_ID))
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "s" * 32)
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    get_settings.cache_clear()
    try:
        with pytest.raises(SystemExit, match="https"):
            admin_cli.main(["set-webhook", "http://insecure.example"], telegram_transport=fake.transport)
        admin_cli.main(["set-webhook", "https://chat.example.com/"], telegram_transport=fake.transport)
        assert fake.webhook["url"] == "https://chat.example.com/api/telegram/webhook"
        assert fake.webhook["secret_token"] == "s" * 32
        admin_cli.main(["bot-status"], telegram_transport=fake.transport)
        out = capsys.readouterr().out
        assert "https://chat.example.com/api/telegram/webhook" in out and tg.TOKEN not in out
        monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "")
        get_settings.cache_clear()
        with pytest.raises(SystemExit, match="TELEGRAM_WEBHOOK_SECRET"):
            admin_cli.main(["set-webhook", "https://chat.example.com"], telegram_transport=fake.transport)
    finally:
        get_settings.cache_clear()


def test_media_responses_skip_corp_everything_else_has_it(hx):
    """CORP on videos breaks playback when the first bytes come from the prefetch cache (Service Worker)."""
    c = hx.client()
    assert c.get("/").headers["cross-origin-resource-policy"] == "same-origin"
    assert c.get("/api/me").headers["cross-origin-resource-policy"] == "same-origin"
    r = c.get("/media/xxxxxxxxxxxxxxxx/mp4?e=1&s=bad")
    assert r.status_code in (403, 404) and "cross-origin-resource-policy" not in r.headers


def test_cli_bot_test_and_admin_link(monkeypatch, capsys, tmp_path):
    from app import admin_cli
    from app.config import get_settings

    fake = tg.FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", tg.TOKEN)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(tg.ADMIN_ID))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    monkeypatch.setenv("ADMIN_PATH", "/panel-cli-test")
    get_settings.cache_clear()
    try:
        admin_cli.main(["bot-test"], telegram_transport=fake.transport)
        assert str(fake.sent[-1]["chat_id"]) == str(tg.ADMIN_ID) and "DZPLAY متصل" in fake.sent[-1]["text"]
        admin_cli.main(["admin-link", "https://chat.example.com/"])
        out = capsys.readouterr().out
        assert "https://chat.example.com/panel-cli-test" in out and "create-admin" in out
        assert tg.TOKEN not in out
    finally:
        get_settings.cache_clear()
