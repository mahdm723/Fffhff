"""V6 phase 16: membership granted automatically after the chain check (AUTO_APPROVE_MEMBERSHIP, off by default).

Granted only when every TXID is ✅ and the sum covers the price (an overpayment is granted and marked for review),
the account is active and not suspicious (an admin mark, a phone shared with a banned / suspended account, an open
report or content flag, an unlocked phone — never a shared network), and the daily limit is not reached. Anything
else waits for the owner, with the reason. No double grant. The owner can take an automatic grant back."""

from __future__ import annotations

import json
import threading

import pytest
from sqlalchemy import select

from app import clock
from app.models import (AdminAuditLog, AppDevice, LedgerEntry, MembershipRequest, MembershipTx, Notification, Referral, Report,
                        User, UserDevice)
from app.services import membership_auto, payment_check
from tests.test_chain import _harness, _pay, _req, _tg, _tx, smtp  # noqa: F401 (fixture)
from tests.test_rewards import _invited

PRICE = 50


@pytest.fixture
def ax(make_harness, smtp):  # noqa: F811
    return _harness(make_harness, smtp, AUTO_APPROVE_MEMBERSHIP=True)


def _user(hx, c) -> User:
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        db.expunge(u)
        return u


def _granted(hx, rid) -> bool:
    req = _req(hx, rid)
    return req.status == "accepted" and req.decided_by == "system:auto" and req.auto_state == "granted"


def _txs(hx, rid) -> list[MembershipTx]:
    with hx.db() as db:
        return list(db.scalars(select(MembershipTx).where(MembershipTx.request_id == rid)))


def _manual(hx, rid) -> list[str]:
    req = _req(hx, rid)
    assert req.status == "pending" and req.decided_by is None
    return membership_auto.note_of(req).get("manual") or []


def test_off_by_default_nothing_is_decided(make_harness, smtp):  # noqa: F811
    hx = _harness(make_harness, smtp)
    c = hx.user()
    hx.chain.tron_tx(_tx(1), PRICE)
    rid = _pay(hx, c, _tx(1))
    req = _req(hx, rid)
    assert req.check_status == "match" and req.status == "pending" and req.auto_state is None


def test_a_matching_payment_is_granted_with_audit_telegram_notification_and_mail(ax):
    c = ax.user()
    ax.chain.tron_tx(_tx(2), PRICE)
    rid = _pay(ax, c, _tx(2))
    ax.state.pipeline.wait_idle()
    assert _granted(ax, rid)
    req = _req(ax, rid)
    assert req.check_status == "match" and req.checked_at is not None  # the check result is kept
    assert all(t.status == "ok" for t in _txs(ax, rid))
    assert c.get("/api/me").json()["member"] is True
    with ax.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        assert db.scalar(select(LedgerEntry).where(LedgerEntry.user_id == u.id, LedgerEntry.kind == "mem_pay")) is not None
        actions = set(db.scalars(select(AdminAuditLog.action).where(AdminAuditLog.target_id == rid)))
        assert {"membership_accept", "membership_auto_granted"} <= actions
        assert db.scalar(select(Notification).where(Notification.user_id == u.id, Notification.kind == "membership_accepted"))
    text = "\n".join(ax.tg.texts())
    assert "تم منح العضوية تلقائيًا" in text
    buttons = json.dumps([m.get("reply_markup") for m in ax.tg.sent])
    assert f"ms:revoke:{rid}" in buttons
    assert any(m["to"] == [c.email] for m in ax.smtp.messages)  # the «membership accepted» e-mail


def test_an_overpayment_is_granted_and_marked_for_review(ax):
    c = ax.user()
    ax.chain.tron_tx(_tx(3), PRICE + 7)
    rid = _pay(ax, c, _tx(3))
    assert _granted(ax, rid)
    assert membership_auto.note_of(_req(ax, rid))["review"] == "over"
    assert "مبلغ زائد" in ax.tg.texts()[-1] or any("مبلغ زائد" in t for t in ax.tg.texts())


@pytest.mark.parametrize("case", ["under", "wrong_recipient", "fake_token", "failed", "waiting"])
def test_anything_but_a_full_valid_payment_waits_for_the_owner(ax, case):
    from tests.test_chain import FAKE_HEX, OTHER_HEX

    c = ax.user()
    txid = _tx(10 + len(case))
    if case == "under":
        ax.chain.tron_tx(txid, PRICE - 10)
    elif case == "wrong_recipient":
        ax.chain.tron_tx(txid, PRICE, to=OTHER_HEX)
    elif case == "fake_token":
        ax.chain.tron_tx(txid, PRICE, contract=FAKE_HEX)
    elif case == "failed":
        ax.chain.tron_tx(txid, PRICE, result="REVERT")
    # waiting: not on the chain yet
    rid = _pay(ax, c, txid)
    req = _req(ax, rid)
    assert req.status == "pending" and req.auto_state is None
    assert c.get("/api/me").json()["member"] is False


def test_a_network_without_automatic_check_stays_manual(make_harness, smtp):  # noqa: F811
    hx = _harness(make_harness, smtp, network="SOL", wallet="So1anaWa11etAddre55xxxxxxxxxxxxxxxxxxxxxx",
                  AUTO_APPROVE_MEMBERSHIP=True)
    c = hx.user()
    rid = _pay(hx, c, "5" * 88)
    req = _req(hx, rid)
    assert req.check_status == "unsupported" and req.status == "pending"


def test_an_extra_failed_txid_keeps_it_manual(ax):
    c = ax.user()
    ax.chain.tron_tx(_tx(20), PRICE - 10)
    rid = _pay(ax, c, _tx(20))
    ax.adm.post(f"/api/admin/membership/requests/{rid}/topup")
    ax.chain.tron_tx(_tx(21), 10, result="REVERT")
    ax.chain.tron_tx(_tx(22), 10)
    assert c.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(21)}).status_code == 201
    ax.state.pipeline.wait_idle()
    assert c.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(22)}).status_code == 201
    ax.state.pipeline.wait_idle()
    req = _req(ax, rid)
    assert req.check_status == "match" and req.status == "pending"  # summed to the price, but one TXID failed
    assert _manual(ax, rid)


def _device(ax, user_id: str, **kw) -> str:
    with ax.db() as db:
        dev = AppDevice(key_id=(user_id + "k" * 64)[:64], public_key="x", security_level="tee",
                        details=json.dumps({"boot": kw.get("boot", "verified"), "locked": kw.get("locked", True)}),
                        suspicious_at=clock.utcnow() if kw.get("flagged") else None, suspicious_reason="test" if kw.get("flagged") else None)
        db.add(dev)
        db.flush()
        now = clock.utcnow()
        db.add(UserDevice(user_id=user_id, device_id=dev.id, first_seen_at=now, last_seen_at=now))
        db.commit()
        return dev.id


@pytest.mark.parametrize("signal", ["flagged_account", "flagged_device", "device_of_banned", "open_report", "unlocked_phone"])
def test_each_suspicion_signal_keeps_it_manual(ax, signal):
    c = ax.user()
    u = _user(ax, c)
    if signal == "flagged_account":
        r = ax.adm.post(f"/api/admin/users/{u.id}/suspicious", json={"on": True, "reason": "تجربة"})
        assert r.status_code == 200, r.text
    elif signal == "flagged_device":
        dev = _device(ax, u.id)
        assert ax.adm.post(f"/api/admin/devices/{dev}/suspicious", json={"on": True, "reason": "تجربة"}).status_code == 200
    elif signal == "device_of_banned":
        dev = _device(ax, u.id)
        other = ax.user()
        o = _user(ax, other)
        with ax.db() as db:
            now = clock.utcnow()
            db.add(UserDevice(user_id=o.id, device_id=dev, first_seen_at=now, last_seen_at=now))
            db.get(User, o.id).status = "banned"
            db.commit()
    elif signal == "open_report":
        reporter = _user(ax, ax.user())
        with ax.db() as db:
            db.add(Report(reporter_id=reporter.id, reported_user_id=u.id, reason="spam", status="open",
                          expires_at=clock.utcnow().replace(year=clock.utcnow().year + 1)))
            db.commit()
    elif signal == "unlocked_phone":
        _device(ax, u.id, locked=False, boot="unverified")
    ax.chain.tron_tx(_tx(30), PRICE)
    rid = _pay(ax, c, _tx(30))
    why = _manual(ax, rid)
    assert why, signal
    card = next(r for r in ax.adm.get("/api/admin/membership").json()["requests"] if r["id"] == rid)
    assert card["auto"]["suspicion"] or card["auto"]["manual"]
    assert any("للمراجعة اليدوية" in t for t in ax.tg.texts())


def test_a_shared_network_alone_is_not_suspicious(ax):
    a = ax.user(ip="10.9.9.9")
    b = ax.user(ip="10.9.9.9")  # the same carrier address
    with ax.db() as db:
        db.get(User, _user(ax, b).id).status = "banned"
        db.commit()
    ax.chain.tron_tx(_tx(40), PRICE)
    rid = _pay(ax, a, _tx(40))
    assert _granted(ax, rid)


def test_the_daily_limit_sends_the_rest_to_the_owner(make_harness, smtp):  # noqa: F811
    hx = _harness(make_harness, smtp, AUTO_APPROVE_MEMBERSHIP=True, AUTO_APPROVE_DAILY_MAX=1)
    a, b = hx.user(), hx.user()
    hx.chain.tron_tx(_tx(50), PRICE)
    hx.chain.tron_tx(_tx(51), PRICE)
    first = _pay(hx, a, _tx(50))
    second = _pay(hx, b, _tx(51))
    assert _granted(hx, first)
    assert any("حده اليومي" in x for x in _manual(hx, second))
    clock.advance(seconds=86400)  # a new day: the owner can re-check it and it is granted
    hx.adm.post(f"/api/admin/membership/requests/{second}/check")
    assert _granted(hx, second)


def test_two_checks_at_once_grant_once(ax):
    c = ax.user()
    ax.chain.tron_tx(_tx(60), PRICE)
    rid = _pay(ax, c, _tx(60))
    assert _granted(ax, rid)
    for _ in range(3):  # more checks, from the panel and the loop: nothing more happens
        payment_check.run(ax.state, rid, force=True)
    with ax.db() as db:
        u = db.scalar(select(User).where(User.email == c.email))
        assert len(db.scalars(select(LedgerEntry).where(LedgerEntry.user_id == u.id, LedgerEntry.kind == "mem_pay")).all()) == 1
        assert len(db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "membership_auto_granted")).all()) == 1


@pytest.mark.skipif("not __import__('os').environ.get('DZ_TEST_DATABASE_URL')")
def test_concurrent_checks_grant_once_and_keep_the_daily_limit(make_harness, smtp):  # noqa: F811
    """PostgreSQL: the chain loop and the panel check the same request at the same moment — one grant, one ledger
    entry; two requests racing for the last slot of the day — only one is granted."""
    hx = _harness(make_harness, smtp, AUTO_APPROVE_DAILY_MAX=1)
    a, b, c = hx.user(), hx.user(), hx.user()
    for n in (80, 81, 82):
        hx.chain.tron_tx(_tx(n), PRICE)
    ra, rb, rc = _pay(hx, a, _tx(80)), _pay(hx, b, _tx(81)), _pay(hx, c, _tx(82))
    assert all(_req(hx, r).check_status == "match" and _req(hx, r).status == "pending" for r in (ra, rb, rc))
    hx.state.settings.AUTO_APPROVE_MEMBERSHIP = True  # switched on now: the three are checked again, all together

    def run(rid):
        payment_check.run(hx.state, rid, force=True)

    threads = [threading.Thread(target=run, args=(rid,)) for rid in (ra, ra, ra, rb, rc)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    granted = [r for r in (ra, rb, rc) if _granted(hx, r)]
    assert len(granted) == 1  # the daily limit (1), whoever came first
    with hx.db() as db:
        assert len(db.scalars(select(LedgerEntry).where(LedgerEntry.kind == "mem_pay")).all()) == 1
        assert len(db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "membership_auto_granted")).all()) == 1
    for r in {ra, rb, rc} - set(granted):
        assert _req(hx, r).status == "pending" and _req(hx, r).auto_state is None


def test_the_owner_takes_it_back_from_telegram(ax):
    c = ax.user()
    ax.chain.tron_tx(_tx(70), PRICE)
    rid = _pay(ax, c, _tx(70))
    assert _granted(ax, rid)
    _tg(ax, f"ms:revoke:{rid}")
    assert _req(ax, rid).auto_state == "granted"  # the first tap only asks for a confirmation
    assert f"ms:revokeyes:{rid}" in json.dumps(ax.tg.edited[-1].get("reply_markup"))
    _tg(ax, f"ms:revokeyes:{rid}")
    assert _req(ax, rid).auto_state == "revoked"
    assert c.get("/api/me").json()["member"] is False
    with ax.db() as db:
        assert db.scalar(select(AdminAuditLog).where(AdminAuditLog.action == "membership_auto_revoked"))
        u = db.scalar(select(User).where(User.email == c.email))
        assert db.scalar(select(Notification).where(Notification.user_id == u.id, Notification.kind == "membership_revoked"))


def test_the_owner_takes_it_back_from_the_panel_and_the_invitation_reward_goes(ax):
    inviter = ax.user()
    c = _invited(ax, inviter)
    ax.chain.tron_tx(_tx(80), PRICE)
    rid = _pay(ax, c, _tx(80))
    assert _granted(ax, rid)
    with ax.db() as db:
        assert db.scalar(select(Referral.status)) in ("rewarded", "review")
    r = ax.adm.post(f"/api/admin/membership/requests/{rid}/revoke", json={"reason": "مراجعة"})
    assert r.status_code == 200, r.text
    assert r.json()["auto"]["state"] == "revoked"
    with ax.db() as db:
        assert db.scalar(select(Referral.status)) == "reversed"
    assert ax.adm.post(f"/api/admin/membership/requests/{rid}/revoke", json={}).status_code == 409


def test_a_manual_decision_meanwhile_wins(ax):
    c = ax.user()
    rid = _pay(ax, c, _tx(90))  # not on the chain yet: waiting
    r = ax.adm.post(f"/api/admin/membership/requests/{rid}/decide", json={"action": "reject", "note": "لا"})
    assert r.status_code == 200
    ax.chain.tron_tx(_tx(90), PRICE)
    payment_check.run(ax.state, rid, force=True)
    req = _req(ax, rid)
    assert req.status == "rejected" and req.auto_state is None
