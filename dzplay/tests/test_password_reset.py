"""Account recovery: same answer for every e-mail, admin via Telegram, code by e-mail (real
local SMTP server), code rules, IP/e-mail limits, sessions revoked, nothing secret logged."""

from __future__ import annotations

import logging
import re
import time

import pytest
from sqlalchemy import select

from app import clock
from app.models import PasswordReset, User
from tests import fake_telegram as tg
from tests.conftest import PASSWORD
from tests.smtp_sink import SmtpSink

NEW_PASSWORD = "Brand-New-Pass-42"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def rx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET="s3cret-hook",
                      SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none",
                      SMTP_FROM="DZPLAY <no-reply@dzplay.test>")
    hx.tg, hx.smtp = fake, smtp
    return hx


def ask(hx, c, email: str):
    r = c.post("/api/auth/reset/request", json={"email": email, "antibot": hx.challenge(c, "reset")})
    hx.state.bot.wait_idle()
    return r


def bot(hx, update):
    hx.client().post("/api/telegram/webhook", json=update, headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret-hook"})
    hx.state.bot.wait_idle()


def request_id(hx) -> str:
    text = next(m["text"] for m in reversed(hx.tg.sent) if "طلب استعادة" in m["text"])
    return re.search(r"رقم الطلب: ([A-Z0-9]+)", text).group(1)


def mailed_code(hx) -> str:
    deadline = time.time() + 5
    while not hx.smtp.messages and time.time() < deadline:
        time.sleep(0.05)
    return re.search(r"^ {4}([A-Z0-9]{4,12})\s*$", hx.smtp.last_text(), re.M).group(1)


# ----------------------------------------------------------------- no enumeration


def test_same_answer_for_existing_missing_and_google_accounts(rx):
    rx.user("real@example.com")
    with rx.db() as db:
        db.add(User(email="google-only@example.com", google_sub="g-123", password_hash=None))
    answers, timings = [], []
    for email in ("real@example.com", "missing@example.com", "google-only@example.com"):
        c = rx.client()
        t0 = time.perf_counter()
        r = ask(rx, c, email)
        timings.append(time.perf_counter() - t0)
        answers.append((r.status_code, r.json()))
    assert answers[0] == answers[1] == answers[2]
    assert answers[0][0] == 200 and "إن كان هذا البريد" in answers[0][1]["message"]
    # Only the real password account reaches the admin.
    prompts = [m for m in rx.tg.sent if "طلب استعادة" in m["text"]]
    assert len(prompts) == 1 and "real@example.com" in prompts[0]["text"]
    assert prompts[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"].startswith("rgen:")
    assert max(timings) - min(timings) < 0.5  # no slow path that gives away which accounts exist


def test_requires_antibot(rx):
    c = rx.client()
    assert c.post("/api/auth/reset/request", json={"email": "a@example.com"}).status_code == 400


def test_disabled_without_bot_or_smtp(hx):
    c = hx.client()
    assert c.post("/api/auth/reset/request", json={"email": "a@example.com"}).status_code == 503
    assert c.get("/api/config").json()["password_reset_enabled"] is False


# ----------------------------------------------------------------- full flow


def test_full_recovery_with_generated_code(rx):
    victim = rx.user("me@example.com")
    other_device = rx.client()
    assert rx.login(other_device, "me@example.com").status_code == 200
    c = rx.client()
    ask(rx, c, "me@example.com")
    rid = request_id(rx)
    bot(rx, tg.callback(f"rgen:{rid}"))
    assert any("أُرسل رمز الاستعادة" in t for t in rx.tg.texts())
    assert "editMessageReplyMarkup" in rx.tg.calls  # buttons removed after use

    mail = rx.smtp.messages[-1]
    assert mail["to"] == ["me@example.com"] and "DZPLAY" in str(mail["msg"]["From"])
    code = mailed_code(rx)
    assert len(code) == rx.settings.RESET_CODE_LENGTH and code in rx.smtp.last_html()
    assert code not in str(rx.tg.sent)  # the generated code is not shown in Telegram either
    with rx.db() as db:
        r = db.scalar(select(PasswordReset))
        assert r.code_hash and code not in r.code_hash and r.status == "sent"

    v = c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": code})
    assert v.status_code == 200
    token = v.json()["reset_token"]
    # the code is single-use
    assert c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": code}).status_code == 400

    done = c.post("/api/auth/reset/complete", json={"reset_token": token, "password": NEW_PASSWORD,
                                                    "password_confirm": NEW_PASSWORD, "antibot": rx.challenge(c, "reset")})
    assert done.status_code == 200 and done.json()["display_name"].startswith("Tester ")
    assert c.get("/api/me").status_code == 200  # signed in straight away
    # every older session is gone, the old password no longer works, the new one does
    assert victim.get("/api/me").status_code == 401 and other_device.get("/api/me").status_code == 401
    assert rx.login(rx.client(), "me@example.com", PASSWORD).status_code == 401
    assert rx.login(rx.client(), "me@example.com", NEW_PASSWORD).status_code == 200
    # the token is single-use too
    again = c.post("/api/auth/reset/complete", json={"reset_token": token, "password": NEW_PASSWORD,
                                                     "password_confirm": NEW_PASSWORD, "antibot": rx.challenge(c, "reset")})
    assert again.status_code == 400


def test_admin_typed_code_and_new_request_cancels_old(rx):
    rx.user("me@example.com")
    c = rx.client()
    ask(rx, c, "me@example.com")
    first = request_id(rx)
    bot(rx, tg.text(f"/code {first} AB12CD"))
    assert "أُرسل" in rx.tg.last_text()
    assert mailed_code(rx) == "AB12CD"
    ask(rx, c, "me@example.com")  # a new request cancels the previous code
    second = request_id(rx)
    assert second != first
    assert c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": "AB12CD"}).status_code == 400
    bot(rx, tg.text(f"/code {first} ZZ99ZZ"))
    assert "لم يعد صالحًا" in rx.tg.last_text()
    bot(rx, tg.text(f"/code {second} 12"))
    assert "6 إلى 12" in rx.tg.last_text()
    bot(rx, tg.callback(f"rdeny:{second}"))
    assert "رُفض" in rx.tg.last_text()


def test_wrong_codes_cancel_after_limit_and_codes_expire(rx):
    rx.user("me@example.com")
    c = rx.client()
    ask(rx, c, "me@example.com")
    bot(rx, tg.callback(f"rgen:{request_id(rx)}"))
    code = mailed_code(rx)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(rx.settings.RESET_MAX_CODE_ATTEMPTS):
        r = c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": wrong})
        assert r.status_code == 400 and r.json()["error"]["message"] == "الرمز غير صحيح أو منتهي الصلاحية."
    # the right code no longer works: the user must ask for a new one
    assert c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": code}).status_code == 400

    # expiry: a fresh code becomes useless after RESET_CODE_TTL
    clock.advance(rx.settings.RESET_EMAIL_WINDOW + 1)  # (also clears the per-e-mail counter)
    ask(rx, rx.client(), "me@example.com")
    bot(rx, tg.callback(f"rgen:{request_id(rx)}"))
    code2 = mailed_code(rx)
    clock.advance(rx.settings.RESET_CODE_TTL + 1)
    assert c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": code2}).status_code == 400


def test_unknown_email_and_bad_input_get_the_same_error(rx):
    c = rx.client()
    for body in ({"email": "nobody@example.com", "code": "123456"}, {"email": "bad", "code": "123456"},
                 {"email": "nobody@example.com", "code": "x"}):
        r = c.post("/api/auth/reset/verify", json=body)
        assert r.status_code == 400 and r.json()["error"]["message"] == "الرمز غير صحيح أو منتهي الصلاحية."


# ----------------------------------------------------------------- limits


def test_ip_blocked_after_three_requests_for_four_hours(rx):
    c = rx.client(ip="203.0.113.50")
    for i in range(3):
        assert ask(rx, c, f"x{i}@example.com").status_code == 200
    r = ask(rx, c, "x4@example.com")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 3 * 3600
    assert ask(rx, rx.client(ip="198.51.100.60"), "y@example.com").status_code == 200  # other networks unaffected
    clock.advance(rx.settings.RESET_IP_BLOCK_DURATION + 1)
    assert ask(rx, c, "x5@example.com").status_code == 200


def test_trusted_networks_are_not_blocked(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
                      TRUSTED_IPS="203.0.113.77")
    c = hx.client(ip="203.0.113.77")
    for i in range(5):
        assert ask(hx, c, f"t{i}@example.com").status_code == 200


def test_per_email_limit_is_silent(rx):
    rx.user("me@example.com")
    for i in range(rx.settings.RESET_MAX_PER_EMAIL + 2):
        assert ask(rx, rx.client(), "me@example.com").status_code == 200
    prompts = [m for m in rx.tg.sent if "طلب استعادة" in m["text"]]
    assert len(prompts) == rx.settings.RESET_MAX_PER_EMAIL  # the admin is not spammed


def test_smtp_failure_is_reported_to_admin(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      SMTP_HOST="127.0.0.1", SMTP_PORT=1, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
                      SMTP_PASSWORD="smtp-secret-pass", SMTP_TIMEOUT=2, TELEGRAM_WEBHOOK_SECRET="s3cret-hook")
    hx.tg = fake
    hx.user("me@example.com")
    ask(hx, hx.client(), "me@example.com")
    bot(hx, tg.callback(f"rgen:{request_id(hx)}"))
    assert "تعذّر إرسال البريد" in fake.last_text() and "smtp-secret-pass" not in fake.last_text()


def test_no_codes_or_passwords_in_logs(rx, caplog):
    caplog.set_level(logging.DEBUG)
    rx.user("me@example.com")
    c = rx.client()
    ask(rx, c, "me@example.com")
    bot(rx, tg.text(f"/code {request_id(rx)} QW12ER"))
    token = c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": "QW12ER"}).json()["reset_token"]
    c.post("/api/auth/reset/complete", json={"reset_token": token, "password": NEW_PASSWORD,
                                             "password_confirm": NEW_PASSWORD, "antibot": rx.challenge(c, "reset")})
    ours = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith("mail.log"))  # not the test SMTP server
    assert ours, "expected some application log output"
    for secret in ("QW12ER", token, NEW_PASSWORD, tg.TOKEN):
        assert secret not in ours
    with rx.db() as db:
        r = db.scalar(select(PasswordReset))
        assert r.status == "used" and r.token_hash is None


# ----------------------------------------------------------------- manual mode + SMTP from the admin panel


@pytest.fixture
def manual(make_harness):
    """Bot connected, no e-mail server: the admin gets the code in Telegram and sends it by hand."""
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET="s3cret-hook",
                      ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)
    hx.tg = fake
    return hx


def test_recovery_works_without_smtp_admin_sends_the_code(manual):
    c = manual.client()
    assert c.get("/api/config").json()["password_reset_enabled"] is True  # the link is shown
    manual.user("me@example.com")
    assert ask(manual, c, "me@example.com").status_code == 200
    prompt = next(m for m in manual.tg.sent if "طلب استعادة" in m["text"])
    assert "ترسله أنت" in prompt["text"]
    rid = request_id(manual)
    bot(manual, tg.callback(f"rgen:{rid}"))
    reply = manual.tg.last_text()
    assert "me@example.com" in reply and "أرسل هذا الرمز بنفسك" in reply
    code = re.search(r"الرمز: ([A-Z0-9]+)", reply).group(1)
    with manual.db() as db:
        r = db.scalar(select(PasswordReset))
        assert r.status == "sent" and code not in r.code_hash
    v = c.post("/api/auth/reset/verify", json={"email": "me@example.com", "code": code})
    assert v.status_code == 200
    done = c.post("/api/auth/reset/complete", json={"reset_token": v.json()["reset_token"], "password": NEW_PASSWORD,
                                                    "password_confirm": NEW_PASSWORD, "antibot": manual.challenge(c, "reset")})
    assert done.status_code == 200
    assert manual.login(manual.client(), "me@example.com", NEW_PASSWORD).status_code == 200


def _step_up(hx) -> str:
    from app import clock
    from app.security import totp

    clock.advance(totp.STEP)
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


def test_smtp_from_the_panel_sends_codes_automatically(manual, smtp):
    from app.models import AppSetting

    admin = manual.admin()
    body = {"host": "localhost", "port": smtp.port, "security": "none", "username": "", "password": "smtp-pass-123",
            "sender": "DZPLAY <no-reply@dzplay.test>", "test_to": "owner@example.com"}
    assert admin.put("/api/admin/smtp", json={**body, "code": "000000"}).status_code == 403  # 2FA step-up
    assert admin.get("/api/admin/smtp").json()["configured"] is False
    r = admin.put("/api/admin/smtp", json={**body, "code": _step_up(manual)})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["source"] == "panel" and out["password_set"] is True and out["test_error"] is None
    assert "smtp-pass-123" not in r.text and "smtp-pass-123" not in admin.get("/api/admin/smtp").text
    assert smtp.messages[-1]["to"] == ["owner@example.com"]  # the test e-mail
    with manual.db() as db:
        assert "smtp-pass-123" not in str([s.value for s in db.execute(select(AppSetting)).scalars()])

    # now the recovery code goes out by e-mail, live (no restart)
    c = manual.client()
    manual.user("me@example.com")
    ask(manual, c, "me@example.com")
    bot(manual, tg.callback(f"rgen:{request_id(manual)}"))
    assert any("أُرسل رمز الاستعادة" in t for t in manual.tg.texts())
    assert smtp.messages[-1]["to"] == ["me@example.com"]
    assert admin.post("/api/admin/smtp/test", json={"to": "x@example.com"}).json() == {"ok": True}

    # bad values are refused; removing falls back to manual mode
    assert admin.put("/api/admin/smtp", json={**body, "host": "bad host!", "code": _step_up(manual)}).status_code == 400
    assert admin.post("/api/admin/smtp/remove", json={"code": _step_up(manual)}).json()["configured"] is False
    assert manual.client().get("/api/config").json()["password_reset_enabled"] is True
