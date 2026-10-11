"""V6 phase 7: two mailboxes (system / support), editable texts with safe variables, the "do not reply" footer,
Reply-To, Telegram alerts on failed delivery (rate limited), and «تواصل معنا»."""

from __future__ import annotations

import socket

import pytest
from sqlalchemy import select

from app import clock
from app.models import User
from app.security import totp
from app.services import email_codes, mail_templates
from tests import fake_telegram as tg
from tests.smtp_sink import SmtpSink

SUPPORT_FROM = "DALTA.BIT — الدعم <support@dzplay.test>"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def mx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_WEBHOOK_SECRET="s3cret-hook", SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port,
                      SMTP_SECURITY="none", SMTP_FROM="DALTA.BIT <no-reply@dzplay.test>",
                      ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)
    hx.tg, hx.smtp = fake, smtp
    return hx


def _step(hx) -> str:
    clock.advance(totp.STEP)
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


def _code_mail(hx, c):
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        email_codes.send(db, hx.settings, hx.state.limiter, u, "withdraw")
        db.commit()
    return hx.smtp.messages[-1]


def _support_box(hx, adm):
    body = {"host": "127.0.0.1", "port": hx.smtp.port, "security": "none", "username": "", "password": "app-pass-xyz",
            "sender": SUPPORT_FROM, "test_to": ""}
    r = adm.put("/api/admin/smtp/support", json={**body, "code": _step(hx)})
    assert r.status_code == 200, r.text
    return r.json()


def _idle(hx):
    hx.state.pipeline.wait_idle()


def test_system_mail_has_footer_and_no_reply_address_until_support_is_set(mx):
    a = mx.user()
    m = _code_mail(mx, a)
    text = m["msg"].get_body(preferencelist=("plain",)).get_content()
    assert "رمز التأكيد" in text and "هذه رسالة آلية، لا ترد عليها" in text
    assert "no-reply@dzplay.test" in m["msg"]["From"] and m["msg"]["Reply-To"] is None
    assert a.get("/api/contact").json() == {"app_name": mx.settings.APP_NAME, "support_email": None}
    # the support mailbox is set: it becomes the official address (Reply-To + footer + «تواصل معنا»)
    adm = mx.admin()
    out = _support_box(mx, adm)
    assert out["inherited"] is False and out["support_address"] == "support@dzplay.test"
    assert "app-pass-xyz" not in str(out)
    m = _code_mail(mx, a)
    text = m["msg"].get_body(preferencelist=("plain",)).get_content()
    assert m["msg"]["Reply-To"] == "support@dzplay.test" and "اكتب إلى support@dzplay.test" in text
    assert "no-reply@dzplay.test" in m["msg"]["From"]  # still sent by the system mailbox
    assert mx.client().get("/api/contact").json()["support_email"] == "support@dzplay.test"  # public, no session


def test_tickets_use_the_support_mailbox_and_fall_back_to_system(mx):
    a = mx.user()
    adm = mx.admin()
    assert adm.get("/api/admin/smtp/support").json()["inherited"] is True
    # before the support mailbox: no inbox known → only Telegram; replies go out from the system mailbox
    t = a.post("/api/support/tickets", json={"category": "technical", "body": "مشكلة"}).json()["ticket"]
    _idle(mx)
    n = len(mx.smtp.messages)
    assert any("تذكرة جديدة" in x for x in mx.tg.texts())
    assert adm.post(f"/api/admin/support/{t['id']}/reply", json={"body": "تم"}).status_code == 200
    _idle(mx)
    m = mx.smtp.messages[-1]
    assert len(mx.smtp.messages) == n + 1 and m["to"] == [a.email] and "no-reply@dzplay.test" in m["msg"]["From"]
    assert "لا ترد عليها" not in m["msg"].get_body(preferencelist=("plain",)).get_content()  # support mail: no footer
    # with the support mailbox
    _support_box(mx, adm)
    t2 = a.post("/api/support/tickets", json={"category": "other", "subject": "سؤال", "body": "كيف أغيّر الصورة؟"}).json()["ticket"]
    _idle(mx)
    m = mx.smtp.messages[-1]
    assert m["to"] == ["support@dzplay.test"] and "support@dzplay.test" in m["msg"]["From"]
    assert m["msg"]["Reply-To"] == a.email and f"#{t2['number']}" in m["msg"]["Subject"]
    assert "كيف أغيّر الصورة؟" in m["msg"].get_body(preferencelist=("plain",)).get_content()
    adm.post(f"/api/admin/support/{t2['id']}/reply", json={"body": "من حسابي ← الصورة"})
    _idle(mx)
    m = mx.smtp.messages[-1]
    assert m["to"] == [a.email] and "support@dzplay.test" in m["msg"]["From"] and m["msg"]["Reply-To"] == "support@dzplay.test"
    # removing it falls back to the system mailbox again
    assert adm.post("/api/admin/smtp/support/remove", json={"code": _step(mx)}).json()["inherited"] is True


def test_admin_routes_need_super_admin_and_fresh_2fa(mx):
    a = mx.user()
    from tests.conftest import ADMIN_PATH

    for method, path in (("get", "/api/admin/smtp/support"), ("put", "/api/admin/smtp/support"),
                         ("post", "/api/admin/smtp/support/test"), ("post", "/api/admin/smtp/support/remove")):
        assert getattr(a, method)(ADMIN_PATH + path, json={}).status_code in (401, 403, 404, 405) if method != "get" \
            else a.get(ADMIN_PATH + path).status_code in (401, 403, 404)
    adm = mx.admin()
    body = {"host": "127.0.0.1", "port": mx.smtp.port, "security": "none", "username": "", "password": "p",
            "sender": SUPPORT_FROM, "test_to": "", "code": "000000"}
    assert adm.put("/api/admin/smtp/support", json=body).status_code == 403
    assert adm.put("/api/admin/smtp/support", json={**body, "sender": "not an address", "code": _step(mx)}).status_code == 400
    _support_box(mx, adm)
    assert adm.post("/api/admin/smtp/support/test", json={"to": "owner@example.com"}).json() == {"ok": True}
    m = mx.smtp.messages[-1]
    assert "بريد الدعم" in m["msg"]["Subject"] and "support@dzplay.test" in m["msg"]["From"]
    audit = [e["action"] for e in adm.get("/api/admin/audit").json()["entries"]]
    assert "smtp_update" in audit


def test_templates_replace_only_known_variables():
    out = mail_templates.fill("{0.__class__} {app} {settings} {code} {code.__init__}", {"code": "123", "app": "X"}, ("code",))
    assert out == "{0.__class__} X {settings} 123 {code.__init__}"
    # a value is never expanded again
    assert mail_templates.fill("{message}", {"message": "{app} {code}", "app": "X"}, ("message",)) == "{app} {code}"
    assert set(mail_templates.TEMPLATES) >= {"email.code", "email.reset", "email.giveaway", "email.ticket_new",
                                             "email.ticket_reply", "email.footer", "email.notice"}


def test_failed_delivery_alerts_the_admin_once_per_half_hour(make_harness):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        closed = s.getsockname()[1]  # nothing listens here once the socket is closed
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_WEBHOOK_SECRET="s3cret-hook", SMTP_HOST="127.0.0.1", SMTP_PORT=closed, SMTP_SECURITY="none",
                      SMTP_FROM="DALTA.BIT <no-reply@dzplay.test>")
    a = hx.user()

    def attempt():
        with hx.db() as db:
            u = db.scalar(select(User).where(User.email == a.email))
            with pytest.raises(Exception) as exc:
                email_codes.send(db, hx.settings, hx.state.limiter, u, "refund")
            assert getattr(exc.value, "code", "") == "mail_failed"

    def alerts():
        return [t for t in fake.texts() if "تعذّر إرسال بريد (بريد النظام)" in t]

    attempt()
    assert len(alerts()) == 1 and "ConnectionRefusedError" in alerts()[0]
    attempt()
    assert len(alerts()) == 1  # not again within 30 minutes
    clock.advance(1801)
    attempt()
    assert len(alerts()) == 2
    assert all("smtp" not in t.lower() or "password" not in t.lower() for t in fake.texts())
