"""V6 phase 5c: «الظرف الأحمر» — one entry per person, e-mail confirmed by code, fair random draw with an audit
trail, prize codes sealed and visible to admins only, sent by e-mail with an in-app notification."""

from __future__ import annotations

import json
import random
import re
from collections import Counter
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.models import AppSetting, GiveawayWinner, User
from app.security import totp
from app.services import giveaway
from tests.smtp_sink import SmtpSink


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def gx(make_harness, smtp):
    hx = make_harness(SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
                      ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)
    hx.smtp = smtp
    hx.adm = hx.admin()
    return hx


def _mail_code(hx, to=None) -> str:
    for m in reversed(hx.smtp.messages):
        if to and to not in m["to"]:
            continue
        body = m["msg"].get_body(preferencelist=("plain",)).get_content()
        found = re.search(r"\b(\d{6})\b", body)
        if found and "رمز التأكيد" in body:
            return found.group(1)
    raise AssertionError("no code mail")


def _round(hx, winners=2, hours=24, show=True):
    ends = (clock.utcnow() + timedelta(hours=hours)).isoformat() + "Z"
    r = hx.adm.post("/api/admin/giveaway", json={"title": "ظرف رمضان", "winners_count": winners, "ends_at": ends,
                                                 "show_winners": show})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _enter(hx, c, rid, email=None):
    r = c.post(f"/api/giveaway/{rid}/enter", json={"email": email} if email else {})
    assert r.status_code == 200, r.text
    if not r.json()["confirmed"]:
        assert c.post(f"/api/giveaway/{rid}/confirm", json={"code": _mail_code(hx, email or c.email)}).status_code == 200


def _step(hx) -> str:
    clock.advance(totp.STEP)
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


def test_enter_with_account_email_confirmed_once(gx):
    rid = _round(gx)
    a = gx.user()
    d = a.get("/api/giveaway").json()
    assert d["round"]["open"] is True and d["entry"] is None and d["account_email_verified"] is False
    r = a.post(f"/api/giveaway/{rid}/enter", json={})
    assert r.json()["confirmed"] is False and r.json()["sent_to"].endswith("@example.com")
    assert a.post(f"/api/giveaway/{rid}/confirm", json={"code": "000000"}).status_code == 400
    assert a.post(f"/api/giveaway/{rid}/confirm", json={"code": _mail_code(gx, a.email)}).status_code == 200
    d = a.get("/api/giveaway").json()
    assert d["entry"]["confirmed"] is True and d["account_email_verified"] is True
    assert a.post(f"/api/giveaway/{rid}/enter", json={}).json()["error"]["code"] == "already_entered"
    # next round: the account e-mail is already confirmed — no code
    gx.adm.post(f"/api/admin/giveaway/{rid}/cancel")
    rid2 = _round(gx)
    n = len(gx.smtp.messages)
    assert a.post(f"/api/giveaway/{rid2}/enter", json={}).json() == {"confirmed": True}
    assert len(gx.smtp.messages) == n


def test_another_email_needs_its_own_code_and_is_unique_per_round(gx):
    rid = _round(gx)
    a, b = gx.user(), gx.user()
    _enter(gx, a, rid, "prize.box@example.org")
    r = b.post(f"/api/giveaway/{rid}/enter", json={"email": "prize.box@example.org"})
    assert r.json()["error"]["code"] == "email_taken"
    with gx.db() as db:
        assert db.scalar(select(User.email_verified_at).where(User.email == a.email)) is None  # other address


def test_closed_after_end_and_no_entry_needed_membership(gx):
    rid = _round(gx, hours=1)
    a = gx.user()
    assert a.get("/api/me").json()["member"] is False  # anyone can enter
    clock.advance(3601)
    assert a.post(f"/api/giveaway/{rid}/enter", json={}).json()["error"]["code"] == "giveaway_closed"


def test_draw_rules_audit_and_codes_stay_with_admins(gx):
    rid = _round(gx, winners=2)
    people = [gx.user() for _ in range(5)]
    for p in people[:4]:
        _enter(gx, p, rid)
    people[4].post(f"/api/giveaway/{rid}/enter", json={})  # never confirmed: excluded
    with gx.db() as db:  # one entrant banned meanwhile: excluded
        db.scalar(select(User).where(User.email == people[3].email)).status = "banned"
        db.commit()
    assert gx.adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": _step(gx)}).json()["error"]["code"] == "not_ended"
    clock.advance(25 * 3600)
    assert gx.adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": "000000"}).status_code == 403  # fresh 2FA
    r = gx.adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": _step(gx)})
    assert r.status_code == 200, r.text
    d = r.json()
    log = d["draw_log"]
    assert log["entrants"] == 3 and len(log["winners"]) == 2 and len(log["entrants_sha256"]) == 64
    winners = {w["entry"]["public_id"] for w in d["winners"]}
    banned_pid = people[3].get("/api/me").json()
    assert len(winners) == 2
    assert any(e["action"] == "giveaway_draw" for e in gx.adm.get("/api/admin/audit").json()["entries"])
    assert gx.adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": _step(gx)}).json()["error"]["code"] == "already_drawn"
    # codes: one per winner (or one for all); sealed in the database; never in a user response
    assert gx.adm.post(f"/api/admin/giveaway/{rid}/codes", json={"codes": ["A", "B", "C"]}).status_code == 400
    r = gx.adm.post(f"/api/admin/giveaway/{rid}/codes", json={"codes": ["GIFT-AAAA-1111", "GIFT-BBBB-2222"]})
    assert sorted(w["code"] for w in r.json()["winners"]) == ["GIFT-AAAA-1111", "GIFT-BBBB-2222"]
    with gx.db() as db:
        sealed = [w.code_sealed for w in db.execute(select(GiveawayWinner)).scalars()]
        assert all("GIFT" not in s for s in sealed)
        assert not db.scalar(select(AppSetting).where(AppSetting.value.like("%GIFT%")))
    r = gx.adm.post(f"/api/admin/giveaway/{rid}/send")
    assert r.status_code == 200, r.text
    gx.state.pipeline.wait_idle()
    bodies = [m["msg"].get_body(preferencelist=("plain",)).get_content() for m in gx.smtp.messages]
    assert sum("GIFT-" in b for b in bodies) == 2
    active = [p for i, p in enumerate(people) if i != 3]  # people[3] is banned (signed out)
    for p in active:
        text = json.dumps([p.get("/api/giveaway").json(), p.get("/api/notifications").json(), p.get("/api/me").json()])
        assert "GIFT-" not in text
    won = [p for p in active if p.get("/api/giveaway").json()["round"]["i_won"]]
    assert len(won) == 2 and banned_pid
    assert [n["kind"] for n in won[0].get("/api/notifications").json()["notifications"]] == ["giveaway_won"]
    shown = people[0].get("/api/giveaway").json()["round"]["winners"]
    assert len(shown) == 2  # names shown (show_winners on), never e-mails
    assert not any("@" in n for n in shown)


def test_same_network_entries_are_flagged_and_excluded(gx):
    rid = _round(gx, winners=5)
    a, b = gx.user(ip="10.20.30.40"), gx.user(ip="10.20.30.40")
    _enter(gx, a, rid)
    _enter(gx, b, rid)
    clock.advance(25 * 3600)
    d = gx.adm.post(f"/api/admin/giveaway/{rid}/draw", json={"code": _step(gx)}).json()
    assert d["draw_log"]["entrants"] == 1  # the second entry from the same network is flagged
    flagged = [e for e in d["entrants"] if e["flags"]]
    assert len(flagged) == 1 and flagged[0]["flags"] == "dup_network"


def test_draw_is_fair():
    """2000 draws of 1 winner among 10: every entrant wins about 200 times (cryptographic randomness)."""
    ids = [f"e{i}" for i in range(10)]
    counts = Counter(giveaway.pick(ids, 1)[0] for _ in range(2000))
    assert set(counts) == set(ids)
    assert all(120 < n < 290 for n in counts.values()), counts
    # distinct winners, never more than the entrants
    assert len(set(giveaway.pick(ids, 3))) == 3 and len(giveaway.pick(ids[:2], 5)) == 2
    # reproducible with a seeded generator (for the audit trail explanation)
    assert giveaway.pick(ids, 2, random.Random(7)) == giveaway.pick(ids, 2, random.Random(7))


def test_users_cannot_reach_admin_routes(gx):
    rid = _round(gx)
    from tests.conftest import ADMIN_PATH

    a = gx.user()
    for path in (f"/api/admin/giveaway/{rid}", f"/api/admin/giveaway/{rid}/draw"):
        assert a.get(ADMIN_PATH + path).status_code in (401, 403, 404, 405)
        assert a.post(ADMIN_PATH + path, json={}).status_code in (401, 403, 404, 405)
