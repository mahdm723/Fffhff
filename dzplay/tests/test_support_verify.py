"""V5 phase C: support tickets, blue-star verification with manual crypto payment, star visibility,
mass assignment, star-like names."""

from __future__ import annotations

import pytest

from app import clock
from app.models import AdminAuditLog, User
from tests import fake_telegram as tg
from tests.conftest import send
from tests.smtp_sink import SmtpSink

TRC_TX = "a" * 64
WALLET = "TQ5pZ9aBcDeFgHiJkLmNoPqRsTuVwXyZ12"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def cx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_WEBHOOK_SECRET="s3cret-hook", SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port,
                      SMTP_SECURITY="none", SMTP_FROM="DZPLAY <no-reply@dzplay.test>",
                      SUPPORT_INBOX_EMAIL="support-inbox@dzplay.test",
                      VERIFY_MIN_POSTS=1, VERIFY_MIN_LIKES=0, VERIFY_MIN_ACCOUNT_AGE_DAYS=0)
    hx.tg, hx.smtp = fake, smtp
    return hx


def idle(hx):
    hx.state.pipeline.wait_idle()
    if hx.state.bot:
        hx.state.bot.wait_idle()


def pid(c) -> str:
    return c.get("/api/me").json()["public_id"]


def deliver(hx, update):
    r = hx.client().post("/api/telegram/webhook", json=update,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret-hook", "X-DZ-Requested": ""})
    assert r.status_code == 200
    idle(hx)


# ----------------------------------------------------------------- support


def test_ticket_mail_telegram_reply_and_privacy(cx):
    a, b = cx.user(), cx.user()
    r = a.post("/api/support/tickets", json={"category": "technical", "subject": "لا تصل الإشعارات",
                                             "body": "منذ أمس لا تصلني إشعارات الرسائل."})
    assert r.status_code == 201, r.text
    t = r.json()["ticket"]
    assert t["number"] == 1001 and t["status"] == "open"
    idle(cx)
    mail = cx.smtp.messages[-1]
    assert mail["to"] == ["support-inbox@dzplay.test"]
    assert "#1001" in mail["msg"]["Subject"] and mail["msg"]["Reply-To"] == a.email
    text = cx.smtp.last_text()
    assert pid(a) in text and "منذ أمس" in text
    with cx.db() as db:
        internal = db.query(User).filter_by(email=a.email).one().id
    assert internal not in text
    assert any("تذكرة جديدة #1001" in m for m in cx.tg.texts())
    # only the owner sees it
    assert b.get(f"/api/support/tickets/{t['id']}").status_code == 404
    assert b.post(f"/api/support/tickets/{t['id']}/messages", json={"body": "x"}).status_code == 404
    # the panel answers: the user sees it as unread + gets an e-mail
    adm = cx.admin()
    r = adm.post(f"/api/admin/support/{t['id']}/reply", json={"body": "جرّب تفعيل الإشعارات من الإعدادات."})
    assert r.status_code == 200, r.text
    idle(cx)
    assert cx.smtp.messages[-1]["to"] == [a.email] and "#1001" in cx.smtp.messages[-1]["msg"]["Subject"]
    mine = a.get("/api/support/tickets").json()["tickets"][0]
    assert mine["status"] == "answered" and mine["unread"] is True
    detail = a.get(f"/api/support/tickets/{t['id']}").json()
    assert [m["from"] for m in detail["messages"]] == ["me", "support"]
    assert "admin" not in detail["messages"][1]  # who answered stays internal
    assert a.get("/api/support/tickets").json()["tickets"][0]["unread"] is False
    assert a.post(f"/api/support/tickets/{t['id']}/messages", json={"body": "نجح، شكرًا"}).status_code == 201
    assert a.post(f"/api/support/tickets/{t['id']}/close").json()["ticket"]["status"] == "closed"
    assert a.post(f"/api/support/tickets/{t['id']}/messages", json={"body": "?"}).status_code == 409
    with cx.db() as db:
        assert db.query(AdminAuditLog).filter_by(action="support_reply").count() == 1


def test_ticket_validation_and_rate_limit(cx):
    a = cx.user()
    assert a.post("/api/support/tickets", json={"category": "hack", "body": "x"}).status_code == 400
    assert a.post("/api/support/tickets", json={"category": "other", "body": "  "}).status_code == 400
    for i in range(cx.settings.SUPPORT_TICKETS_PER_DAY):
        assert a.post("/api/support/tickets", json={"category": "other", "body": f"رسالة {i}"}).status_code == 201
    assert a.post("/api/support/tickets", json={"category": "other", "body": "أخرى"}).status_code == 429


# ----------------------------------------------------------------- verification


def eligible(c):
    assert c.post("/api/posts", json={"content": "فكرة أولى لأصبح مؤهلًا"}).status_code == 201


def set_payment(cx, **kw):
    body = {"currency": "USDT", "network": "TRC20", "wallet": WALLET, **kw}
    return cx.admin().put("/api/admin/payment-settings", json=body)


def test_conditions_payment_settings_and_request_flow(cx):
    a = cx.user()
    ov = a.get("/api/verification").json()
    assert ov["conditions"]["met"] is False and ov["payment"]["available"] is False
    assert {i["key"] for i in ov["conditions"]["items"]} == {"posts", "likes", "age", "clean"}
    eligible(a)
    form = {"account_type": "writer", "description": "أكتب خواطر يومية", "reason": "ليعرفني القراء", "amount": "5",
            "txid": TRC_TX}
    assert a.post("/api/verification", json=form).json()["error"]["code"] == "payment_unavailable"
    # payment settings: validated, audited
    assert set_payment(cx, network="NOPE").status_code == 400
    assert set_payment(cx, wallet="x").status_code == 400
    assert set_payment(cx, explorer="http://evil/{txid}").status_code == 400
    assert set_payment(cx).status_code == 200
    ov = a.get("/api/verification").json()
    assert ov["conditions"]["met"] is True and ov["payment"]["available"] is True
    assert ov["payment"]["wallet"] == WALLET and ov["payment"]["qr"].startswith("data:image/svg+xml;base64,")
    assert "TRC20" in ov["payment"]["warning"]
    # TXID format per network, amount
    assert a.post("/api/verification", json={**form, "txid": "0x" + "a" * 64}).json()["error"]["code"] == "invalid_txid"
    assert a.post("/api/verification", json={**form, "amount": "-1"}).json()["error"]["code"] == "invalid_amount"
    r = a.post("/api/verification", json={**form, "verified": True, "status": "accepted"})  # extra fields ignored
    assert r.status_code == 201, r.text
    req = r.json()["request"]
    assert req["status"] == "pending"
    assert a.post("/api/verification", json=form).status_code == 409  # one open request
    b = cx.user()
    eligible(b)
    assert b.post("/api/verification", json=form).json()["error"]["code"] == "txid_used"  # one payment, one request
    assert a.get("/api/me").json()["verified"] is False
    idle(cx)
    notice = next(m for m in cx.tg.sent if "طلب توثيق" in m.get("text", ""))
    assert TRC_TX in notice["text"] and "tronscan.org" in notice["text"]
    datas = [btn["callback_data"] for row in notice["reply_markup"]["inline_keyboard"] for btn in row]
    assert f"vf:accept:{req['id']}" in datas
    # panel: ask for a correction; the user fixes it; Telegram accepts
    adm = cx.admin()
    lst = adm.get("/api/admin/verification?status=pending").json()
    row = lst["requests"][0]
    assert row["explorer_url"] == f"https://tronscan.org/#/transaction/{TRC_TX}" and row["public_id"] == pid(a)
    assert adm.post(f"/api/admin/verification/{req['id']}/decide", json={"action": "fix", "note": "المبلغ غير مطابق"}).status_code == 200
    ov = a.get("/api/verification").json()
    assert ov["request"]["status"] == "needs_fix" and ov["request"]["admin_note"] == "المبلغ غير مطابق"
    assert a.put(f"/api/verification/{req['id']}", json={**form, "amount": "10", "txid": "b" * 64}).status_code == 200
    assert b.put(f"/api/verification/{req['id']}", json=form).status_code == 404  # not theirs
    deliver(cx, tg.callback(f"vf:accept:{req['id']}"))
    me = a.get("/api/me").json()
    assert me["verified"] is True
    idle(cx)
    assert "النجمة الزرقاء" in cx.smtp.last_text()
    with cx.db() as db:
        actions = {r.action for r in db.query(AdminAuditLog).all()}
    assert {"payment_settings", "verify_fix", "verify_accept", "verify_grant"} <= actions


def test_star_shown_where_allowed_never_in_anonymous_chats_and_revocable(cx):
    a, b = cx.user(), cx.user()
    a.patch("/api/me/profile", json={"display_name": "Amine"})
    with cx.db() as db:
        aid = db.query(User).filter_by(email=a.email).one().id
    adm = cx.admin()
    assert adm.post(f"/api/admin/users/{aid}/verified", json={"verified": True}).json()["verified"] is True
    post = a.post("/api/posts", json={"content": "فكرة موثقة"}).json()
    assert post["author"]["verified"] is True
    assert b.get(f"/api/posts/{post['id']}").json()["author"]["verified"] is True
    assert b.get(f"/api/profiles/{post['author']['ref']}").json()["verified"] is True
    found = b.get("/api/people/search?q=Amine").json()
    assert any(p["verified"] for p in found["results"])
    assert b.get(f"/api/people/{pid(a)}").json()["verified"] is True
    # direct chat: star; anonymous chat: never (even once identity is revealed)
    cid = b.post(f"/api/people/{pid(a)}/messages", json={"content": "سلام"}).json()["conversation"]["id"]
    assert b.get(f"/api/conversations/{cid}").json()["conversation"]["peer_card"]["verified"] is True
    c = cx.user()  # b already has an open chat with a: the random match goes to c
    anon = send(a, "رسالة مجهولة").json()["conversation"]["id"]
    assert a.post(f"/api/conversations/{anon}/reveal").status_code == 200
    card = c.get(f"/api/conversations/{anon}").json()["conversation"]["peer_card"]
    assert card["name"] == "Amine" and card["verified"] is False
    # revoke
    assert adm.post(f"/api/admin/users/{aid}/verified", json={"verified": False, "reason": "مخالفة"}).status_code == 200
    assert b.get(f"/api/posts/{post['id']}").json()["author"]["verified"] is False


def test_mass_assignment_cannot_grant_star_or_money(cx):
    a = cx.user()
    for body in ({"verified": True}, {"verified_at": "2026-01-01T00:00:00Z"}, {"is_verified": True},
                 {"role": "super_admin"}, {"balance": 1000}, {"monetization_status": "accepted"}, {"status": "active"}):
        a.patch("/api/me/profile", json=body)
        a.patch("/api/me/privacy", json=body)
    me = a.get("/api/me").json()
    assert me["verified"] is False
    with cx.db() as db:
        u = db.query(User).filter_by(email=a.email).one()
        assert u.verified_at is None and u.verified_by is None
    # the panel endpoints need an admin session
    assert a.post(f"/test-panel/api/admin/users/{u.id}/verified", json={"verified": True}).status_code == 401
    assert a.put("/test-panel/api/admin/payment-settings", json={"currency": "USDT"}).status_code == 401
    assert a.put("/test-panel/api/admin/settings", json={"changes": {"VERIFY_MIN_POSTS": 0}}).status_code == 401


@pytest.mark.parametrize("name", ["Amine✓", "✔Amine", "Amine☑", "Amine⭐", "🌟Amine", "Amine✅", "Amine★", "Amine✪", "Amine✯"])
def test_star_like_symbols_are_refused_in_names(cx, name):
    a = cx.user()
    r = a.patch("/api/me/profile", json={"display_name": name})
    assert r.status_code == 400


def test_admin_settings_endpoint_live_and_audited(cx):
    a = cx.user()
    adm = cx.admin()
    rows = {s["key"]: s for s in adm.get("/api/admin/settings").json()["settings"]}
    assert "VERIFY_MIN_POSTS" in rows and "TELEGRAM_BOT_TOKEN" not in rows and "SECRET_KEY" not in rows
    r = adm.put("/api/admin/settings", json={"changes": {"VERIFY_MIN_POSTS": 3, "SUPPORT_TICKETS_PER_DAY": 7}})
    assert r.status_code == 200, r.text
    items = {i["key"]: i for i in a.get("/api/verification").json()["conditions"]["items"]}
    assert items["posts"]["target"] == 3
    assert adm.put("/api/admin/settings", json={"changes": {"VERIFY_MIN_POSTS": "lots"}}).status_code == 400
    with cx.db() as db:
        assert db.query(AdminAuditLog).filter_by(action="settings_change").count() == 1
    clock.advance(1)
