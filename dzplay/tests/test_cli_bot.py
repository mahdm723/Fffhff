"""The admin's Telegram bot (webhook security, commands), the admin CLI, the media disk cache and response
headers. (Moved here from the V5 Reels tests when Reels were removed in V6.)"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import select

from app import clock
from app.config import Settings
from app.models import SecurityEvent
from tests import fake_telegram as tg
from tests.test_uploads import jpeg, ready_upload

WEBHOOK = "/api/telegram/webhook"
SECRET = "hook-secret-0123456789"
STORAGE = "-1001111111111"


@pytest.fixture
def bot(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=SECRET,
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE)
    hx.tg = fake
    return hx


def deliver(hx, update: dict, secret: str = SECRET):
    r = hx.client().post(WEBHOOK, json=update, headers={"X-Telegram-Bot-Api-Secret-Token": secret, "X-DZ-Requested": ""})
    hx.state.bot.wait_idle()
    return r


# ----------------------------------------------------------------- the bot


def test_webhook_requires_secret_and_bot_enabled(bot, hx):
    assert hx.client().post(WEBHOOK, json={}, headers={"X-Telegram-Bot-Api-Secret-Token": SECRET}).status_code == 404
    c = bot.client()
    assert c.post(WEBHOOK, json=tg.text("/stats")).status_code == 403  # no header (and no CSRF header needed)
    assert c.post(WEBHOOK, json=tg.text("/stats"), headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}).status_code == 403
    assert bot.tg.sent == []
    assert deliver(bot, tg.text("/stats")).status_code == 200
    assert "المستخدمون" in bot.tg.last_text()


def test_strangers_are_ignored_silently_and_logged(bot):
    fid = bot.tg.add_file(jpeg())
    deliver(bot, tg.photo(fid, 1000, sender=tg.STRANGER_ID))
    deliver(bot, tg.text("/stats", sender=tg.STRANGER_ID))
    deliver(bot, tg.text("/stats", sender=tg.STRANGER_ID, chat=tg.ADMIN_ID))  # spoofed chat, wrong sender
    deliver(bot, tg.callback("vf:ok:abc", sender=tg.STRANGER_ID))
    assert bot.tg.sent == [] and bot.tg.downloads == []
    with bot.db() as db:
        events = db.scalars(select(SecurityEvent).where(SecurityEvent.type == "telegram_unauthorized")).all()
        assert len(events) == 4 and str(tg.STRANGER_ID) not in " ".join(e.detail for e in events)


def test_media_sent_to_the_bot_is_not_published_any_more(bot):
    """V6: Reels were removed — a video or a picture sent by the admin only gets the help text."""
    vid = bot.tg.add_file(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 400, name="vid")
    deliver(bot, tg.video(vid, 416, "وصف"))
    assert "أوامر DZPLAY" in bot.tg.last_text() and "Reel" not in bot.tg.last_text()
    assert bot.tg.downloads == []
    for cmd in ("/list", "/pin abc", "/delete abc"):
        deliver(bot, tg.text(cmd))
        assert "أمر غير معروف" in bot.tg.last_text()


def test_token_never_reaches_logs_or_responses(bot, caplog):
    caplog.set_level(logging.DEBUG)
    bot.tg.fail_methods.add("sendMessage")  # every reply fails → errors get logged
    deliver(bot, tg.text("/stats"))
    deliver(bot, tg.text("/help"))
    assert caplog.records, "expected some log output"
    assert tg.TOKEN not in caplog.text and tg.TOKEN.split(":")[1] not in caplog.text


# ----------------------------------------------------------------- disk cache (pictures)


def test_cache_lru_size_cap_ttl_and_refetch(bot):
    a, b = bot.user(), bot.user()
    m1 = ready_upload(bot, a, jpeg((200, 20, 20)))
    p1 = a.post("/api/posts", json={"content": "الأولى", "media_id": m1}).json()
    clock.advance(600)
    m2 = ready_upload(bot, b, jpeg((20, 200, 20)))
    b.post("/api/posts", json={"content": "الثانية", "media_id": m2})
    store = bot.state.media
    with bot.db() as db:
        total = store.total_size(db)
    assert total > 0
    # Cap the cache just under its current size: the least recently used picture goes first.
    store.settings = Settings(**{**bot.settings.model_dump(), "MEDIA_CACHE_MAX_GB": (total - 1) / 1024 ** 3})
    with bot.db() as db:
        assert store.purge(db)["lru"] >= 1
        assert not store.path_for(m1, "img").exists()
        assert store.total_size(db) <= total - 1
    # A viewer asking for the evicted file gets it again (re-fetched from Telegram and prepared).
    before = len(bot.tg.downloads)
    viewer = bot.user()
    url = next(p for p in viewer.get("/api/posts/feed").json()["posts"] if p["id"] == p1["id"])["media"]["url"]
    assert viewer.get(url).status_code == 200
    assert len(bot.tg.downloads) == before + 1

    store.settings = bot.settings
    clock.advance(bot.settings.MEDIA_CACHE_TTL + 60)
    with bot.db() as db:
        assert store.purge(db)["expired"] >= 2
        assert store.total_size(db) == 0
    assert not any(Path(bot.settings.MEDIA_CACHE_DIR).glob("*.webp"))


def test_media_paths_cannot_escape_the_cache(bot):
    a = bot.user()
    mid = ready_upload(bot, a, jpeg())
    a.post("/api/posts", json={"content": "صورة", "media_id": mid})
    for bad in (f"/media/{mid}/../../app/config.py", f"/media/..%2f..%2fapp/img?e=1&s={'0' * 40}",
                f"/media/{mid}/source?e=1&s={'0' * 40}", f"/media/{mid}/mp4?e=1&s={'0' * 40}", f"/media/{mid}/img"):
        assert a.get(bad).status_code in (403, 404), bad


def test_every_response_has_corp_including_media(hx):
    c = hx.client()
    assert c.get("/").headers["cross-origin-resource-policy"] == "same-origin"
    assert c.get("/api/me").headers["cross-origin-resource-policy"] == "same-origin"
    r = c.get("/media/xxxxxxxxxxxxxxxx/img?e=1&s=bad")
    assert r.status_code in (403, 404) and r.headers["cross-origin-resource-policy"] == "same-origin"
    assert c.get("/").headers["permissions-policy"].startswith("camera=(), microphone=()")


def test_ideas_comments_are_public_since_v6(hx):
    owner, writer, other = hx.user(), hx.user(), hx.user()
    pid = owner.post("/api/posts", json={"content": "فكرة بتعليقات عامة"}).json()["id"]
    assert writer.post(f"/api/posts/{pid}/comments", json={"content": "تعليق عام"}).status_code == 201
    for c in (owner, writer, other):
        assert "تعليق عام" in c.get(f"/api/posts/{pid}/comments").text
    assert writer.email not in other.get(f"/api/posts/{pid}/comments").text


# ----------------------------------------------------------------- admin CLI


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
        assert "Reel" not in fake.sent[-1]["text"]
        admin_cli.main(["admin-link", "https://chat.example.com/"])
        out = capsys.readouterr().out
        assert "https://chat.example.com/panel-cli-test" in out and "create-admin" in out
        assert tg.TOKEN not in out
    finally:
        get_settings.cache_clear()


def test_cli_alert_goes_to_the_admin_chat(monkeypatch, capsys, tmp_path):
    """Server monitoring: deploy/monitor.sh → admin_cli alert → the admin's Telegram chat (token stays server-side)."""
    from app import admin_cli
    from app.config import get_settings

    fake = tg.FakeTelegram()
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", tg.TOKEN)
    monkeypatch.setenv("TELEGRAM_ADMIN_CHAT_ID", str(tg.ADMIN_ID))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/cli.db")
    get_settings.cache_clear()
    try:
        admin_cli.main(["alert", "القرص / ممتلئ بنسبة 91%"], telegram_transport=fake.transport)
        assert str(fake.sent[-1]["chat_id"]) == str(tg.ADMIN_ID)
        assert fake.sent[-1]["text"].startswith("🚨 DZPLAY") and "91%" in fake.sent[-1]["text"]
        assert tg.TOKEN not in capsys.readouterr().out
        with pytest.raises(SystemExit):
            admin_cli.main(["alert", "   "], telegram_transport=fake.transport)
    finally:
        get_settings.cache_clear()
