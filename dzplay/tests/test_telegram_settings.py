"""Connecting the Telegram bot from the admin panel: write-only token, 2FA step-up, live reconfiguration."""

from __future__ import annotations

import logging

import pytest
from sqlalchemy import select

from app import clock
from app.models import AdminAuditLog, AppSetting, Reel
from app.security import totp
from tests import fake_telegram as tg
from tests.test_reels import ffmpeg_binary, media_files  # noqa: F401  (session fixture with real clips)

TOKEN = "987654321:AAH" + "x" * 32
WEBHOOK = "/api/telegram/webhook"


@pytest.fixture
def panel(make_harness):
    fake = tg.FakeTelegram(token=TOKEN)
    hx = make_harness(telegram_transport=fake.transport, PUBLIC_URL="https://chat.example.com",
                      ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)
    hx.tg = fake
    hx.panel_admin = hx.admin()
    return hx


def code(hx, username: str = "owner") -> str:
    clock.advance(totp.STEP)  # a fresh step: codes are single-use (the login used the previous one)
    return totp.code_at(hx.admin_secrets[username], totp.current_step(clock.timestamp()))


def connect(hx, token: str = TOKEN, chat_id: str = str(tg.ADMIN_ID), otp: str | None = None):
    return hx.panel_admin.put("/api/admin/telegram", json={"token": token, "chat_id": chat_id, "code": otp or code(hx)})


def test_connect_from_panel_goes_live_without_restart(panel, media_files, caplog):  # noqa: F811
    caplog.set_level(logging.DEBUG)
    assert panel.state.bot is None
    assert panel.panel_admin.get("/api/admin/telegram").json()["configured"] is False
    assert panel.client().post(WEBHOOK, json={}).status_code == 404  # no bot yet

    r = connect(panel)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["configured"] and body["source"] == "panel" and body["bot_username"] == "dzplay_test_bot"
    assert body["token_hint"] == "…" + TOKEN[-4:] and body["chat_id"] == str(tg.ADMIN_ID) and body["test_error"] is None
    assert TOKEN not in r.text
    # webhook registered on the public URL with a generated secret; hello message sent to the admin chat
    hook = panel.tg.webhook
    assert hook["url"] == "https://chat.example.com/api/telegram/webhook" and len(hook["secret_token"]) == 64
    assert "DZPLAY متصل" in panel.tg.last_text()

    # the token is stored sealed, never in clear
    with panel.db() as db:
        stored = {s.key: s.value for s in db.execute(select(AppSetting)).scalars()}
        audit_rows = [f"{a.action} {a.detail}" for a in db.execute(select(AdminAuditLog)).scalars()]
    assert TOKEN not in str(stored) and stored["telegram.chat_id"] == str(tg.ADMIN_ID)
    assert any(r.startswith("telegram_update") for r in audit_rows) and TOKEN not in str(audit_rows)
    assert TOKEN not in caplog.text

    # live: an upload through the webhook with the new secret becomes a Reel
    fid = panel.tg.add_file(media_files["small"], name="vid")
    up = panel.client().post(WEBHOOK, json=tg.video(fid, len(media_files["small"]), "من اللوحة"),
                             headers={"X-Telegram-Bot-Api-Secret-Token": hook["secret_token"]})
    assert up.status_code == 200
    panel.state.bot.wait_idle()
    with panel.db() as db:
        assert db.scalar(select(Reel.status)) == "visible"
    # GET never returns the token
    status = panel.panel_admin.get("/api/admin/telegram")
    assert status.status_code == 200 and TOKEN not in status.text
    assert panel.panel_admin.post("/api/admin/telegram/test").json() == {"ok": True}


def test_needs_fresh_2fa_code_and_valid_token(panel):
    assert connect(panel, otp="000000").status_code == 403
    # the code of the login step is already used (replay)
    login_code = totp.code_at(panel.admin_secrets["owner"], totp.current_step(clock.timestamp()))
    assert connect(panel, otp=login_code).status_code == 403
    assert connect(panel, token="not-a-token").status_code == 400
    assert connect(panel, chat_id="abc").status_code == 400
    # Telegram rejects an unknown token (getMe 401)
    assert connect(panel, token="111111111:AAH" + "y" * 32).json()["error"]["code"] == "invalid_token"
    with panel.db() as db:
        assert db.scalar(select(AppSetting)) is None
    assert panel.state.bot is None


def test_users_and_anonymous_cannot_touch_it(panel):
    user = panel.user()
    for c in (panel.client(), user):
        assert c.put("/test-panel/api/admin/telegram", json={"token": TOKEN, "chat_id": "1", "code": "1"}).status_code in (401, 403)
        assert c.get("/test-panel/api/admin/telegram").status_code in (401, 403)


def test_remove_falls_back_to_env_or_disables(make_harness):
    fake = tg.FakeTelegram(token=TOKEN)
    env_fake_token = tg.TOKEN  # .env bot uses the default fake token (a different bot)
    hx = make_harness(telegram_transport=fake.transport, PUBLIC_URL="https://chat.example.com",
                      ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)
    hx.tg, hx.panel_admin = fake, hx.admin()
    assert connect(hx).status_code == 200
    r = hx.panel_admin.post("/api/admin/telegram/remove", json={"code": code(hx)})
    assert r.status_code == 200 and r.json()["configured"] is False and fake.webhook is None
    assert hx.client().post(WEBHOOK, json={}).status_code == 404
    with hx.db() as db:
        assert db.scalar(select(AppSetting)) is None

    # with a bot in .env, the panel value wins while set, and removing it goes back to .env
    hx2 = make_harness(telegram_transport=fake.transport, PUBLIC_URL="https://chat.example.com",
                       TELEGRAM_BOT_TOKEN=env_fake_token, TELEGRAM_ADMIN_CHAT_ID="555",
                       TELEGRAM_WEBHOOK_SECRET="e" * 32,
                       ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)
    hx2.tg, hx2.panel_admin = fake, hx2.admin()
    assert hx2.state.telegram_source == "env"
    assert connect(hx2).json()["source"] == "panel" and hx2.state.bot.admin_id == str(tg.ADMIN_ID)
    hx2.panel_admin.post("/api/admin/telegram/remove", json={"code": code(hx2)})
    assert hx2.state.telegram_source == "env" and hx2.state.bot.admin_id == "555"


def test_panel_settings_are_applied_at_startup(panel):
    assert connect(panel).status_code == 200
    secret = panel.tg.webhook["secret_token"]
    panel.state.configure_telegram(None)  # as if the process restarted with no bot in .env ...
    assert panel.state.bot is None
    panel.state.load_runtime_config()  # ... and ran the startup step
    assert panel.state.telegram_source == "panel" and panel.state.telegram_secret == secret
    assert panel.state.bot is not None and panel.state.bot.admin_id == str(tg.ADMIN_ID)
