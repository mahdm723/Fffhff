"""V6 phase 5: membership — 50 USDT once, features only (star, picture posts, chat pictures); refund within the
window (7 days, 1 USDT fee) with password + e-mail code; every amount goes through the ledger."""

from __future__ import annotations

import re
import threading

import pytest
from sqlalchemy import select

from app import clock
from app.models import LedgerEntry, MembershipRequest, User
from app.services import ledger, legacy_v6
from tests import fake_telegram as tg
from tests.conftest import ADMIN_PATH, PASSWORD
from tests.smtp_sink import SmtpSink

TRC_TX = "a" * 64
WALLET = "TQn9Y2khEsLJW1ChVWFMSMeRDow5KcbLSE"
REFUND_TO = "TNPeeaaFB7K9cmo4uQpcU32zGK8G8NHxXz"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def mx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET="hook-secret-0123456789",
                      SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test")
    hx.default_member = False
    hx.tg, hx.smtp = fake, smtp
    adm = hx.admin()
    r = adm.put("/api/admin/payment-settings", json={"currency": "USDT", "network": "TRC20", "wallet": WALLET})
    assert r.status_code == 200, r.text
    hx.adm = adm
    return hx


def _tx(n: int) -> str:
    return f"{n:064x}"


def _pay(hx, c, txid=TRC_TX):
    return c.post("/api/membership/requests", json={"txid": txid})


def _accept(hx, rid):
    return hx.adm.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "accept"})


def _code(hx) -> str:
    text = hx.smtp.messages[-1]["msg"].get_body(preferencelist=("plain",)).get_content()
    return re.search(r"\b(\d{6})\b", text).group(1)


def _uid(hx, c):
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == c.email))


def test_membership_screen_shows_features_and_payment_only(mx):
    a = mx.user()
    d = a.get("/api/membership").json()
    assert d["price"] == "50.00" and d["currency"] == "USDT" and d["member"] is False
    assert d["payment"]["wallet"] == WALLET and d["payment"]["qr"].startswith("data:image/svg+xml")
    assert d["refund"]["enabled"] is True and d["refund"]["window_days"] == 7 and d["refund"]["fee"] == "1.00"
    text = a.get("/api/membership").text
    for word in ("ربح", "أرباح", "عائد", "profit", "earning", "return"):
        assert word not in text  # features only, never any return


def test_full_flow_accept_star_ledger_notification_mail(mx):
    a = mx.user()
    r = _pay(mx, a)
    assert r.status_code == 201, r.text
    rid = r.json()["request"]["id"]
    assert r.json()["request"]["status"] == "pending"
    mx.state.pipeline.wait_idle()
    assert any(f"ms:accept:{rid}" in str(m.get("reply_markup")) for m in mx.tg.sent)  # Telegram buttons
    assert _pay(mx, a, _tx(2)).status_code == 409  # one pending request at a time
    r = _accept(mx, rid)
    assert r.status_code == 200, r.text
    me = a.get("/api/me").json()
    assert me["member"] is True and me["verified"] is True
    with mx.db() as db:
        u = db.get(User, _uid(mx, a))
        assert u.verified_by == "membership"
        assert ledger.balances(db, u.id, "membership") == {"available": 5000, "pending": 0, "total": 5000}
    assert [n["kind"] for n in a.get("/api/notifications").json()["notifications"]] == ["membership_accepted"]
    mx.state.pipeline.wait_idle()
    assert any("تفعيل عضويتك" in m["msg"].get_body(preferencelist=("plain",)).get_content() for m in mx.smtp.messages)
    # accepting twice changes nothing
    assert _accept(mx, rid).status_code == 409
    with mx.db() as db:
        assert db.scalar(select(LedgerEntry.id).where(LedgerEntry.kind == "mem_pay").offset(1)) is None
    # members can post pictures now (phase 4 rule)
    assert a.get("/api/uploads/config").json()["idea"]["member"] is True


def test_telegram_button_accepts(mx):
    a = mx.user()
    rid = _pay(mx, a).json()["request"]["id"]
    r = mx.client().post("/api/telegram/webhook", json=tg.callback(f"ms:accept:{rid}"),
                         headers={"X-Telegram-Bot-Api-Secret-Token": "hook-secret-0123456789", "X-DZ-Requested": ""})
    assert r.status_code == 200
    mx.state.bot.wait_idle()
    assert a.get("/api/me").json()["member"] is True
    assert any(e["action"] == "membership_accept" for e in mx.adm.get("/api/admin/audit").json()["entries"])


def test_txid_rules(mx):
    a, b = mx.user(), mx.user()
    assert _pay(mx, a, "not-a-txid").status_code == 400
    assert _pay(mx, a).status_code == 201
    assert _pay(mx, b, TRC_TX.upper()).json()["error"]["code"] == "txid_used"  # same payment, any case
    rid = a.get("/api/membership").json()["request"]["id"]
    assert mx.adm.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "reject"}).status_code == 200
    d = a.get("/api/membership").json()
    assert d["request"]["status"] == "rejected" and d["request"]["note"]
    assert _pay(mx, a, _tx(9)).status_code == 201  # can try again with another payment


def test_existing_star_is_kept_and_not_removed_by_refund(mx):
    a = mx.user()
    with mx.db() as db:
        u = db.get(User, _uid(mx, a))
        u.verified_at, u.verified_by = clock.utcnow(), "admin:owner"
        db.commit()
    rid = _pay(mx, a).json()["request"]["id"]
    _accept(mx, rid)
    a.post("/api/membership/refund/code")
    r = a.post("/api/membership/refund", json={"network": "TRC20", "address": REFUND_TO, "password": PASSWORD, "code": _code(mx)})
    assert r.status_code == 201, r.text
    ref = mx.adm.get("/api/admin/membership").json()["refunds"][0]
    mx.adm.post(f"/api/admin/membership/refunds/{ref['id']}/decide", json={"action": "done", "txid": _tx(77)})
    me = a.get("/api/me").json()
    assert me["member"] is False and me["verified"] is True  # the old star stays


def test_refund_within_window_with_password_and_code(mx):
    a = mx.user()
    rid = _pay(mx, a).json()["request"]["id"]
    _accept(mx, rid)
    st = a.get("/api/membership").json()["refund"]
    assert st["eligible"] is True and st["amount"] == "49.00"
    body = {"network": "TRC20", "address": REFUND_TO, "password": PASSWORD}
    assert a.post("/api/membership/refund", json={**body, "code": "123456"}).json()["error"]["code"] == "code_expired"
    assert a.post("/api/membership/refund/code").json()["sent_to"].endswith("@example.com")
    code = _code(mx)
    assert a.post("/api/membership/refund", json={**body, "code": "000000" if code != "000000" else "111111"}).json()["error"]["code"] == "code_invalid"
    assert a.post("/api/membership/refund", json={**body, "password": "wrong", "code": code}).json()["error"]["code"] == "wrong_password"
    assert a.post("/api/membership/refund", json={**body, "address": "0x" + "1" * 40, "code": code}).json()["error"]["code"] == "invalid_address"
    r = a.post("/api/membership/refund", json={**body, "code": code})
    assert r.status_code == 201, r.text
    assert r.json()["refund"]["request"]["status"] == "requested"
    assert a.post("/api/membership/refund/code").status_code == 403  # one open refund
    ref = mx.adm.get("/api/admin/membership").json()["refunds"][0]
    assert ref["amount"] == "49.00" and ref["fee"] == "1.00" and ref["address"] == REFUND_TO
    bad = mx.adm.post(f"/api/admin/membership/refunds/{ref['id']}/decide", json={"action": "done", "txid": "x"})
    assert bad.status_code == 400
    ok = mx.adm.post(f"/api/admin/membership/refunds/{ref['id']}/decide", json={"action": "done", "txid": _tx(55)})
    assert ok.status_code == 200, ok.text
    me = a.get("/api/me").json()
    assert me["member"] is False and me["verified"] is False  # the star came with the membership
    with mx.db() as db:
        uid = _uid(mx, a)
        kinds = sorted(e.kind for e in db.execute(select(LedgerEntry).where(LedgerEntry.user_id == uid)).scalars())
        assert kinds == ["mem_fee", "mem_pay", "mem_refund"]
        assert ledger.balances(db, uid, "membership")["total"] == 0
    assert mx.adm.post(f"/api/admin/membership/refunds/{ref['id']}/decide", json={"action": "done", "txid": _tx(56)}).status_code == 409


def test_refund_window_and_switch(mx):
    a = mx.user()
    _accept(mx, _pay(mx, a).json()["request"]["id"])
    clock.advance(7 * 86400 + 60)
    assert a.get("/api/membership").json()["refund"]["eligible"] is False
    assert a.post("/api/membership/refund/code").status_code == 403
    b = mx.user()
    _accept(mx, _pay(mx, b, _tx(3)).json()["request"]["id"])
    adm = mx.admin("second")  # the first admin session expired with the clock
    assert adm.put("/api/admin/settings", json={"changes": {"MEMBERSHIP_REFUNDABLE": False}}).status_code == 200
    assert b.get("/api/membership").json()["refund"]["eligible"] is False


def test_admin_can_end_a_membership_and_see_the_ledger(mx):
    a = mx.user()
    _accept(mx, _pay(mx, a).json()["request"]["id"])
    uid = _uid(mx, a)
    led = mx.adm.get(f"/api/admin/users/{uid}/balances").json()
    assert led["membership"]["balances"]["total"] == "50.00" and led["rewards"]["balances"]["total"] == "0.00"
    assert mx.adm.post(f"/api/admin/users/{uid}/membership/end", json={"reason": "مخالفة الشروط"}).status_code == 200
    assert a.get("/api/me").json()["member"] is False
    assert any(e["action"] == "membership_end" for e in mx.adm.get("/api/admin/audit").json()["entries"])


def test_no_idor_and_no_mass_assignment(mx):
    a, b = mx.user(), mx.user()
    rid = _pay(mx, a).json()["request"]["id"]
    assert b.post(f"{ADMIN_PATH}/api/admin/membership/requests/{rid}/decide", json={"action": "accept"}).status_code in (401, 403, 404)
    r = b.post("/api/membership/requests", json={"txid": _tx(4), "status": "accepted", "amount_minor": 1})
    assert r.status_code in (201, 400)
    with mx.db() as db:
        rows = db.execute(select(MembershipRequest).where(MembershipRequest.user_id == _uid(mx, b))).scalars().all()
        assert all(x.status == "pending" and x.amount_minor == 5000 for x in rows)
    for path in ("/api/me/profile", "/api/me/privacy"):
        b.patch(path, json={"member_since": "2020-01-01", "verified_at": "2020-01-01"})
    assert b.get("/api/me").json()["member"] is False and b.get("/api/me").json()["verified"] is False


def test_new_ledger_kinds_never_match_the_removed_v5_kinds():
    removed = {"earning", "payout", "adjustment", "reversal"}
    assert not removed & {k for kinds in ledger.KINDS.values() for k in kinds}
    src = legacy_v6.PARTIAL if hasattr(legacy_v6, "PARTIAL") else {}
    assert "payout" in str(src) and "refund" not in str(src) and "withdraw" not in str(src)


@pytest.mark.skipif("not __import__('os').environ.get('DZ_TEST_DATABASE_URL')")
def test_double_accept_at_the_same_moment_posts_once(mx):
    """PostgreSQL: two admins press «accept» together — one wins, the ledger has one entry."""
    a = mx.user()
    rid = _pay(mx, a).json()["request"]["id"]
    codes = []
    adm2 = mx.admin("second")

    def go(c):
        codes.append(c.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "accept"}).status_code)

    threads = [threading.Thread(target=go, args=(c,)) for c in (mx.adm, adm2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 409]
    with mx.db() as db:
        assert len(db.execute(select(LedgerEntry).where(LedgerEntry.kind == "mem_pay")).scalars().all()) == 1
