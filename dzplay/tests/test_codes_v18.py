"""V6 phase 18: codes sent automatically (REGISTRATION_CODE_AUTO on, RESET_CODE_AUTO off by default).

* Every code e-mail leaves from a background queue: the answer never waits for the mail server, and is the same
  for an address that already has an account (that one gets «لديك حساب بالفعل» instead of a code).
* Automatic mode: the code is e-mailed, the owner gets a note in Telegram (masked address). Manual mode: the code
  goes to the owner in Telegram, who sends it by hand. A failed e-mail falls back to the manual path + an alert.
* Every rule stays: hashed codes, single use, a new code cancels the old one, expiry, wrong tries, network and
  mailbox limits.
"""

from __future__ import annotations

import json
import re
import time

import pytest
from sqlalchemy import select

from app import clock
from app.models import AdminAuditLog, PasswordReset, SignupPending, User
from app.services import mail
from tests import fake_telegram as tg
from tests.conftest import PASSWORD
from tests.smtp_sink import SmtpSink

HOOK = "hook-secret-0123456789"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


def _hx(make_harness, smtp=None, **extra):
    fake = tg.FakeTelegram()
    settings = dict(TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=HOOK,
                    REGISTRATION_EMAIL_VERIFICATION=True, ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)
    if smtp is not None:
        settings.update(SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test")
    settings.update(extra)
    hx = make_harness(telegram_transport=fake.transport, **settings)
    hx.default_member = False
    hx.tg, hx.smtp = fake, smtp
    return hx


def _idle(hx) -> None:
    hx.state.mail_queue.wait_idle()
    if hx.state.bot is not None:
        hx.state.bot.wait_idle()


def _mails_to(smtp, to: str) -> list[str]:
    return [m["msg"].get_body(preferencelist=("plain",)).get_content() for m in smtp.messages if to in m["to"]]


def _code_in(text: str) -> str:
    return re.search(r"\b([0-9]{6})\b", text).group(1)


def _account(hx, email: str) -> None:
    with hx.db() as db:
        db.add(User(email=email, email_canonical=email, password_hash="x"))
        db.commit()


def _tg(hx, data: str) -> None:
    r = hx.client().post("/api/telegram/webhook", json=tg.callback(data),
                         headers={"X-Telegram-Bot-Api-Secret-Token": HOOK, "X-DZ-Requested": ""})
    assert r.status_code == 200
    _idle(hx)


# ---------------------------------------------------------------------------------------------- signup


def test_the_same_answer_for_a_new_and_a_taken_address(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    _account(hx, "taken@example.com")
    new = hx.register(hx.client(), "fresh@example.com")
    taken = hx.register(hx.client(), "taken@example.com")
    assert new.status_code == taken.status_code == 202
    a, b = new.json(), taken.json()
    assert set(a) == set(b) and a["signup_token"] != b["signup_token"]
    assert {k: v for k, v in a.items() if k not in ("signup_token", "sent_to")} == \
           {k: v for k, v in b.items() if k not in ("signup_token", "sent_to")}
    _idle(hx)
    assert _code_in(_mails_to(smtp, "fresh@example.com")[-1])
    exists = _mails_to(smtp, "taken@example.com")
    assert len(exists) == 1 and "لديك حساب بالفعل" in exists[0] and not re.search(r"\b[0-9]{6}\b", exists[0])
    notes = "\n".join(hx.tg.texts())
    assert "f***@example.com" in notes and "fresh@example.com" not in notes  # the owner's note: masked, no code
    assert not re.search(r"\b[0-9]{6}\b", notes)


def test_the_answer_does_not_wait_for_the_mail_server(make_harness, smtp, monkeypatch):
    hx = _hx(make_harness, smtp)
    real = mail.deliver

    def slow(out):
        time.sleep(1.5)
        real(out)

    monkeypatch.setattr(mail, "deliver", slow)
    t0 = time.monotonic()
    r = hx.register(hx.client(), "slow@example.com")
    assert r.status_code == 202 and time.monotonic() - t0 < 1.0
    _idle(hx)
    assert _mails_to(smtp, "slow@example.com")


def test_the_taken_address_never_gets_an_account_or_a_code(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    _account(hx, "owner@example.com")
    c = hx.client()
    d = hx.register(c, "owner@example.com").json()
    _idle(hx)
    for guess in ("000000", "123456"):
        r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": guess})
        assert r.status_code == 400 and r.json()["error"]["code"] in ("code_invalid", "code_cancelled")
    with hx.db() as db:
        assert len(db.scalars(select(User).where(User.email == "owner@example.com")).all()) == 1


def test_a_failed_email_falls_back_to_the_owner(make_harness):
    hx = _hx(make_harness, None, SMTP_HOST="127.0.0.1", SMTP_PORT=1, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
             SMTP_TIMEOUT=2)
    c = hx.client()
    r = hx.register(c, "fallback@example.com")
    assert r.status_code == 202  # the person sees the code screen as always
    _idle(hx)
    texts = hx.tg.texts()
    assert any("تعذّر إرسال رمز التسجيل تلقائيًا" in t for t in texts)  # the alert
    manual = next(t for t in texts if "fallback@example.com" in t)
    code = _code_in(manual)
    r = c.post("/api/auth/register/verify", json={"signup_token": r.json()["signup_token"], "code": code})
    assert r.status_code == 201


def test_manual_mode_sends_the_code_to_the_owner_only(make_harness, smtp):
    hx = _hx(make_harness, smtp, REGISTRATION_CODE_AUTO=False)
    _account(hx, "had@example.com")
    c = hx.client()
    d = hx.register(c, "byhand@example.com").json()
    hx.register(hx.client(), "had@example.com")
    _idle(hx)
    assert smtp.messages == []  # nothing e-mailed: the owner sends it
    msg = next(m for m in hx.tg.sent if "byhand@example.com" in m.get("text", ""))
    assert "sg:sent:" in json.dumps(msg["reply_markup"]) and "sg:cancel:" in json.dumps(msg["reply_markup"])
    had = next(t for t in hx.tg.texts() if "had@example.com" in t)
    assert "مسجّل مسبقًا" in had and not re.search(r"\b[0-9]{6}\b", had)
    _tg(hx, msg["reply_markup"]["inline_keyboard"][0][0]["callback_data"])  # «أُرسل»
    with hx.db() as db:
        assert db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "signup_code_sent_by_hand"))
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code_in(msg["text"])})
    assert r.status_code == 201


def test_the_owner_cancels_a_pending_signup_from_telegram(make_harness, smtp):
    hx = _hx(make_harness, smtp, REGISTRATION_CODE_AUTO=False)
    c = hx.client()
    d = hx.register(c, "cancel@example.com").json()
    _idle(hx)
    msg = next(m for m in hx.tg.sent if "cancel@example.com" in m.get("text", ""))
    _tg(hx, msg["reply_markup"]["inline_keyboard"][1][0]["callback_data"])
    with hx.db() as db:
        assert db.scalars(select(SignupPending)).all() == []
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code_in(msg["text"])})
    assert r.json()["error"]["code"] == "signup_expired"


def test_no_mailbox_and_no_bot_means_unavailable(make_harness):
    hx = make_harness(REGISTRATION_EMAIL_VERIFICATION=True)  # neither SMTP nor Telegram
    r = hx.register(hx.client(), "nothing@example.com")
    assert r.status_code == 503 and r.json()["error"]["code"] == "registration_unavailable"


def test_no_mailbox_with_the_bot_goes_manual(make_harness):
    hx = _hx(make_harness, None)
    c = hx.client()
    d = hx.register(c, "nomailbox@example.com").json()
    _idle(hx)
    text = next(t for t in hx.tg.texts() if "nomailbox@example.com" in t)
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code_in(text)})
    assert r.status_code == 201


def test_resend_replaces_the_code_through_the_queue(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    c = hx.client()
    d = hx.register(c, "twice@example.com").json()
    _idle(hx)
    first = _code_in(_mails_to(smtp, "twice@example.com")[-1])
    clock.advance(hx.settings.REGISTER_RESEND_SECONDS + 1)
    assert c.post("/api/auth/register/resend", json={"signup_token": d["signup_token"]}).status_code == 200
    _idle(hx)
    second = _code_in(_mails_to(smtp, "twice@example.com")[-1])
    if first != second:
        r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": first})
        assert r.json()["error"]["code"] == "code_invalid"
    assert c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": second}).status_code == 201


# ---------------------------------------------------------------------------------------------- password reset


def _reset(hx, c, email):
    r = c.post("/api/auth/reset/request", json={"email": email, "antibot": hx.challenge(c, "reset")})
    assert r.status_code == 200, r.text
    _idle(hx)
    return r.json()


def test_reset_stays_manual_by_default(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    _account(hx, "manual@example.com")
    _reset(hx, hx.client(), "manual@example.com")
    assert _mails_to(smtp, "manual@example.com") == []
    assert any("rgen:" in json.dumps(m.get("reply_markup")) for m in hx.tg.sent)


def test_automatic_reset_codes(make_harness, smtp):
    hx = _hx(make_harness, smtp, RESET_CODE_AUTO=True)
    _account(hx, "auto@example.com")
    c = hx.client()
    res = _reset(hx, c, "auto@example.com")
    unknown = _reset(hx, hx.client(), "nobody@example.com")
    assert res == unknown  # the same answer
    first = _code_in(_mails_to(smtp, "auto@example.com")[-1])
    note = next(m for m in hx.tg.sent if "a***@example.com" in m.get("text", ""))
    assert "rdeny:" in json.dumps(note["reply_markup"]) and first not in note["text"]  # an FYI, never the code
    assert _mails_to(smtp, "nobody@example.com") == []
    # a new request cancels the first code
    clock.advance(60)
    _reset(hx, c, "auto@example.com")
    second = _code_in(_mails_to(smtp, "auto@example.com")[-1])
    if first != second:
        assert c.post("/api/auth/reset/verify", json={"email": "auto@example.com", "code": first}).status_code == 400
    r = c.post("/api/auth/reset/verify", json={"email": "auto@example.com", "code": second})
    assert r.status_code == 200 and r.json()["reset_token"]
    # single use
    assert c.post("/api/auth/reset/verify", json={"email": "auto@example.com", "code": second}).status_code == 400
    with hx.db() as db:
        assert all(r.code_hash is None or not r.code_hash.isdigit() for r in db.scalars(select(PasswordReset)))


def test_automatic_reset_failure_falls_back_to_the_owner(make_harness):
    hx = _hx(make_harness, None, RESET_CODE_AUTO=True, SMTP_HOST="127.0.0.1", SMTP_PORT=1, SMTP_SECURITY="none",
             SMTP_FROM="no-reply@dzplay.test", SMTP_TIMEOUT=2)
    _account(hx, "fails@example.com")
    _reset(hx, hx.client(), "fails@example.com")
    texts = "\n".join(hx.tg.texts())
    assert "تعذّر إرسال رمز الاستعادة تلقائيًا" in texts and "fails@example.com" in texts
    assert any("rgen:" in json.dumps(m.get("reply_markup")) for m in hx.tg.sent)


def test_automatic_reset_works_without_the_bot(make_harness, smtp):
    hx = make_harness(RESET_CODE_AUTO=True, SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none",
                      SMTP_FROM="no-reply@dzplay.test")
    _account(hx, "nobot@example.com")
    c = hx.client()
    r = c.post("/api/auth/reset/request", json={"email": "nobot@example.com", "antibot": hx.challenge(c, "reset")})
    assert r.status_code == 200
    hx.state.mail_queue.wait_idle()
    code = _code_in(_mails_to(smtp, "nobot@example.com")[-1])
    assert c.post("/api/auth/reset/verify", json={"email": "nobot@example.com", "code": code}).status_code == 200


def test_the_reset_answer_is_in_the_persons_language(make_harness, smtp):
    hx = _hx(make_harness, smtp, RESET_CODE_AUTO=True)
    c = hx.client()
    r = c.post("/api/auth/reset/request", json={"email": "x@example.com", "antibot": hx.challenge(c, "reset")},
               headers={"Accept-Language": "fr"})
    assert "e-mail" in r.json()["message"] and PASSWORD not in r.text
