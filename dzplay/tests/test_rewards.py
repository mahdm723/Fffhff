"""V6 phase 5b: «أرباحي» — referrals (one level, held, reversed on refund, flagged when suspicious), group and
individual rewards (activity only, never membership), withdrawals (limits, fee, password + e-mail code, lock)."""

from __future__ import annotations

import re
import threading

import pytest
from sqlalchemy import select

from app import clock
from app.models import LedgerEntry, Referral, User, WithdrawalRequest
from app.security import totp
from app.services import ledger
from tests import fake_telegram as tg
from tests.conftest import ADMIN_PATH, PASSWORD
from tests.smtp_sink import SmtpSink
from tests.test_membership import REFUND_TO, WALLET, _code, _tx

BEP_TO = "0x" + "ab" * 20


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def rx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET="hook-secret-0123456789",
                      SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
                      ADMIN_SESSION_IDLE=60 * 86400, ADMIN_SESSION_TTL=60 * 86400)
    hx.default_member = False
    hx.tg, hx.smtp = fake, smtp
    hx.adm = hx.admin()
    assert hx.adm.put("/api/admin/payment-settings", json={"currency": "USDT", "network": "TRC20", "wallet": WALLET}).status_code == 200
    return hx


def _uid(hx, c):
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == c.email))


def _invited(hx, inviter, ip=None):
    """A new account registered through the inviter's link."""
    code = inviter.get("/api/rewards/referrals").json()["code"]
    c = hx.client(ip)
    assert c.get(f"/r/{code}", follow_redirects=False).status_code == 302
    email = f"inv{len(hx.state.database.engine.url.database or '')}{id(c)}@example.com"
    r = hx.register(c, email)
    assert r.status_code == 201, r.text
    c.email = email
    return c


def _member(hx, c, n):
    rid = c.post("/api/membership/requests", json={"txid": _tx(n)}).json()["request"]["id"]
    assert hx.adm.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "accept"}).status_code == 200
    return rid


def _credit(hx, c, amount="30"):
    r = hx.adm.post(f"/api/admin/users/{_uid(hx, c)}/rewards", json={"kind": "contest", "amount": amount, "reason": "فائز بمسابقة"})
    assert r.status_code == 200, r.text


def test_balances_are_three_numbers_from_the_ledger_only(rx):
    a = rx.user()
    d = a.get("/api/rewards").json()
    assert d["balances"] == {"available": "0.00", "pending": "0.00", "total": "0.00"}
    assert "عضوية" in d["note"] and d["withdraw"]["min"] == "10.00" and d["withdraw"]["fee"] == "1.00"
    assert set(d["withdraw"]["networks"]) == {"TRC20", "BEP20"}
    _credit(rx, a, "12.5")
    d = a.get("/api/rewards").json()
    assert d["balances"] == {"available": "12.50", "pending": "0.00", "total": "12.50"}
    assert d["entries"][0]["label"] == "مسابقة" and d["entries"][0]["note"] == "فائز بمسابقة"


def test_referral_held_then_available_and_reversed_on_refund(rx):
    inviter = rx.user()
    b = _invited(rx, inviter, ip="10.9.1.1")
    with rx.db() as db:
        ref = db.scalar(select(Referral))
        assert ref.referrer_id == _uid(rx, inviter) and ref.status == "joined"
        assert "same_network" not in (ref.flags or "")
    assert inviter.get("/api/rewards/referrals").json()["invited"] == 1
    assert inviter.get("/api/rewards").json()["balances"]["total"] == "0.00"  # nothing until the invitee is a member
    _member(rx, b, 1)
    d = inviter.get("/api/rewards").json()
    assert d["balances"] == {"available": "0.00", "pending": "5.00", "total": "5.00"}
    assert inviter.get("/api/rewards/referrals").json()["members"] == 1
    assert [n["kind"] for n in inviter.get("/api/notifications").json()["notifications"]] == ["referral_reward"]
    # the invitee asks for a refund within the window: the pending reward is cancelled
    b.post("/api/membership/refund/code")
    r = b.post("/api/membership/refund", json={"network": "TRC20", "address": REFUND_TO, "password": PASSWORD, "code": _code(rx)})
    assert r.status_code == 201, r.text
    ref_id = rx.adm.get("/api/admin/membership").json()["refunds"][0]["id"]
    assert rx.adm.post(f"/api/admin/membership/refunds/{ref_id}/decide", json={"action": "done", "txid": _tx(90)}).status_code == 200
    assert inviter.get("/api/rewards").json()["balances"]["total"] == "0.00"
    kinds = [e["kind"] for e in inviter.get("/api/rewards").json()["entries"]]
    assert kinds == ["referral_rev", "referral"]


def test_referral_becomes_available_after_the_hold(rx):
    inviter = rx.user()
    b = _invited(rx, inviter, ip="10.9.2.1")
    _member(rx, b, 2)
    clock.advance(14 * 86400 + 60)
    assert inviter.get("/api/rewards").json()["balances"] == {"available": "5.00", "pending": "0.00", "total": "5.00"}


def test_suspicious_referral_waits_for_the_admin(rx):
    inviter = rx.user(ip="10.8.8.8")
    b = _invited(rx, inviter, ip="10.8.8.8")  # same network as the inviter
    with rx.db() as db:
        assert "same_network" in db.scalar(select(Referral.flags))
    _member(rx, b, 3)
    assert inviter.get("/api/rewards").json()["balances"]["total"] == "0.00"
    lst = rx.adm.get("/api/admin/rewards").json()
    assert lst["review"] == 1 and lst["referrals"][0]["status"] == "review"
    rid = lst["referrals"][0]["id"]
    assert rx.adm.post(f"/api/admin/referrals/{rid}/review", json={"action": "approve"}).status_code == 200
    assert inviter.get("/api/rewards").json()["balances"]["pending"] == "5.00"
    assert rx.adm.post(f"/api/admin/referrals/{rid}/review", json={"action": "approve"}).status_code == 409


def test_referral_rules_one_level_no_self_no_late_and_bad_codes(rx):
    a = rx.user()
    code = a.get("/api/rewards/referrals").json()["code"]
    assert re.fullmatch(r"[23456789A-HJ-NP-Z]{8}", code)
    # the cookie lives on /api/auth only and is HttpOnly
    c = rx.client("10.7.0.1")
    cookie = c.get(f"/r/{code}", follow_redirects=False).headers["set-cookie"]
    assert "HttpOnly" in cookie and "Path=/api/auth" in cookie
    # garbage codes are ignored
    for bad in ("../../x", "AAAA", "<script>", "0OIL0OIL"):
        assert "dz_ref" not in rx.client().get(f"/r/{bad}", follow_redirects=False).headers.get("set-cookie", "")
    # an existing account cannot be "invited" later; one inviter per person
    with rx.db() as db:
        u = db.get(User, _uid(rx, a))
        from app.services import rewards

        rewards.attach_referral(db, rx.settings, u, code, None, None)  # self
        db.commit()
        assert db.scalar(select(Referral)) is None
    b = _invited(rx, a, ip="10.7.0.2")
    c2 = _invited(rx, b, ip="10.7.0.3")  # b invites c2: only b is rewarded, never a (one level)
    _member(rx, b, 4)
    _member(rx, c2, 5)
    assert a.get("/api/rewards").json()["balances"]["total"] == "5.00"
    assert b.get("/api/rewards").json()["balances"]["total"] == "5.00"


def test_group_reward_preview_then_fresh_2fa_activity_only(rx):
    active = [rx.user() for _ in range(3)]
    idle = rx.user()
    for c in active:
        c.post("/api/posts", json={"content": "تحليل اليوم: السوق في اتجاه صاعد"})
    member = active[0]
    _member(rx, member, 6)  # membership must change nothing in the criteria
    with rx.db() as db:
        db.get(User, _uid(rx, idle)).last_active_at = clock.utcnow() - __import__("datetime").timedelta(days=40)
        db.commit()
    body = {"amount": 2, "active_days": 30, "min_posts": 1, "min_age_days": 0, "note": "شكرًا لنشاطكم"}
    p = rx.adm.post("/api/admin/rewards/group/preview", json=body).json()
    assert p["count"] == 3 and p["total"] == "6.00"
    assert rx.adm.post("/api/admin/rewards/group", json={**body, "code": "000000"}).status_code == 403
    clock.advance(totp.STEP)
    code = totp.code_at(rx.admin_secrets["owner"], totp.current_step(clock.timestamp()))
    r = rx.adm.post("/api/admin/rewards/group", json={**body, "code": code})
    assert r.status_code == 200, r.text
    assert r.json()["count"] == 3
    for c in active:
        assert c.get("/api/rewards").json()["balances"]["available"] == "2.00"
    assert idle.get("/api/rewards").json()["balances"]["total"] == "0.00"
    assert any(e["action"] == "reward_group" for e in rx.adm.get("/api/admin/audit").json()["entries"])
    import inspect

    from app.services import rewards

    src = inspect.getsource(rewards._eligible)
    assert "member" not in src.replace("Never membership", "") and "Membership" not in src


def test_individual_reward_and_correction_rules(rx):
    a = rx.user()
    uid = _uid(rx, a)
    adm = rx.adm
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "contest", "amount": 5, "reason": ""}).status_code == 400
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "contest", "amount": -5, "reason": "خطأ"}).status_code == 400
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "activity", "amount": 5, "reason": "نشاط"}).status_code == 200
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "correction", "amount": -6, "reason": "تصحيح"}).json()["error"]["code"] == "over_balance"
    assert adm.post(f"/api/admin/users/{uid}/rewards", json={"kind": "correction", "amount": -2, "reason": "تصحيح"}).status_code == 200
    assert a.get("/api/rewards").json()["balances"]["available"] == "3.00"


def _withdraw(hx, c, **kw):
    c.post("/api/rewards/withdraw/code")
    body = {"network": "TRC20", "address": REFUND_TO, "amount": "12", "password": PASSWORD, "code": _code(hx)}
    body.update(kw)
    return c.post("/api/rewards/withdraw", json=body)


def test_withdraw_limits_fee_code_and_admin_done(rx):
    a = rx.user()
    assert a.post("/api/rewards/withdraw/code").json()["error"]["code"] == "below_min"
    _credit(rx, a, "30")
    assert _withdraw(rx, a, amount="9").json()["error"]["code"] == "below_min"
    assert _withdraw(rx, a, amount="31").json()["error"]["code"] == "over_balance"
    assert _withdraw(rx, a, address="0x" + "1" * 40).json()["error"]["code"] == "invalid_address"
    assert _withdraw(rx, a, network="ERC20").json()["error"]["code"] == "invalid_network"
    assert _withdraw(rx, a, password="nope").json()["error"]["code"] == "wrong_password"
    assert _withdraw(rx, a, code="999999").json()["error"]["code"] in ("code_invalid", "code_expired")
    r = _withdraw(rx, a, amount="12")
    assert r.status_code == 201, r.text
    w = r.json()
    assert w["amount"] == "12.00" and w["fee"] == "1.00" and w["net"] == "11.00" and w["status"] == "pending"
    assert a.get("/api/rewards").json()["balances"]["available"] == "18.00"  # reserved at once
    assert _withdraw(rx, a, amount="10").json()["error"]["code"] == "withdraw_open"
    rx.state.pipeline.wait_idle()
    assert any("طلب سحب" in m.get("text", "") for m in rx.tg.sent)
    lst = rx.adm.get("/api/admin/withdrawals").json()
    assert lst["pending"] == 1
    wid = lst["withdrawals"][0]["id"]
    assert rx.adm.post(f"/api/admin/withdrawals/{wid}/decide", json={"action": "done", "txid": "bad"}).status_code == 400
    assert rx.adm.post(f"/api/admin/withdrawals/{wid}/decide", json={"action": "done", "txid": _tx(500)}).status_code == 200
    assert rx.adm.post(f"/api/admin/withdrawals/{wid}/decide", json={"action": "done", "txid": _tx(501)}).status_code == 409
    me = a.get("/api/rewards").json()
    assert me["withdraw"]["history"][0]["status"] == "done" and me["balances"]["available"] == "18.00"


def test_withdraw_reject_returns_the_amount_and_daily_limit(rx):
    a = rx.user()
    _credit(rx, a, "50")
    w = _withdraw(rx, a, network="BEP20", address=BEP_TO, amount="10").json()
    assert rx.adm.post(f"/api/admin/withdrawals/{w['id']}/decide", json={"action": "reject"}).status_code == 400  # reason
    assert rx.adm.post(f"/api/admin/withdrawals/{w['id']}/decide", json={"action": "reject", "note": "العنوان خاطئ"}).status_code == 200
    d = a.get("/api/rewards").json()
    assert d["balances"]["available"] == "50.00" and [e["kind"] for e in d["entries"]][:2] == ["withdraw_rev", "withdraw"]
    w2 = _withdraw(rx, a, amount="10").json()
    rx.adm.post(f"/api/admin/withdrawals/{w2['id']}/decide", json={"action": "reject", "note": "تجربة"})
    assert _withdraw(rx, a, amount="10").json()["error"]["code"] == "withdraw_limit"  # 2 per day


def test_owner_only_no_idor(rx):
    a, b = rx.user(), rx.user()
    _credit(rx, a, "20")
    w = _withdraw(rx, a, amount="10").json()
    assert b.get("/api/rewards").json()["balances"]["total"] == "0.00"
    assert b.post(f"{ADMIN_PATH}/api/admin/withdrawals/{w['id']}/decide", json={"action": "done", "txid": _tx(7)}).status_code in (401, 403, 404)
    r = b.post("/api/rewards/withdraw", json={"network": "TRC20", "address": REFUND_TO, "amount": "10", "password": PASSWORD,
                                               "code": "123456", "user_id": _uid(rx, a)})
    assert r.status_code in (400, 403)
    with rx.db() as db:
        assert db.scalar(select(WithdrawalRequest).where(WithdrawalRequest.user_id == _uid(rx, b))) is None
    assert rx.client().get("/api/rewards").status_code == 401


def test_hold_must_stay_longer_than_the_refund_window(rx):
    r = rx.adm.put("/api/admin/settings", json={"changes": {"REFERRAL_HOLD_DAYS": 7}})
    assert r.status_code == 400 and r.json()["error"]["code"] == "hold_too_short"
    assert rx.adm.put("/api/admin/settings", json={"changes": {"MEMBERSHIP_REFUND_WINDOW_DAYS": 20}}).status_code == 400
    assert rx.adm.put("/api/admin/settings", json={"changes": {"MEMBERSHIP_REFUNDABLE": False}}).status_code == 200
    assert rx.adm.put("/api/admin/settings", json={"changes": {"REFERRAL_HOLD_DAYS": 3}}).status_code == 200  # no refunds now
    assert rx.adm.put("/api/admin/settings", json={"changes": {"MEMBERSHIP_REFUNDABLE": True}}).status_code == 400


@pytest.mark.skipif("not __import__('os').environ.get('DZ_TEST_DATABASE_URL')")
def test_two_withdrawals_at_the_same_moment_reserve_once(rx):
    a = rx.user()
    _credit(rx, a, "15")
    a.post("/api/rewards/withdraw/code")
    code = _code(rx)
    body = {"network": "TRC20", "address": REFUND_TO, "amount": "12", "password": PASSWORD, "code": code}
    codes = []

    def go():
        codes.append(a.post("/api/rewards/withdraw", json=body).status_code)

    threads = [threading.Thread(target=go) for _ in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes)[0] == 201 and sorted(codes)[1] != 201
    with rx.db() as db:
        assert len(db.execute(select(LedgerEntry).where(LedgerEntry.kind == "withdraw")).scalars().all()) == 1
    assert ledger is not None
