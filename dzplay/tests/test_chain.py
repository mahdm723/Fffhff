"""V6 phase 13: the automatic check of membership payments on the chain (fake TronGrid and BSC nodes).

The system checks and shows the result — match, under, over, mismatch (wrong recipient, fake token, failed, used,
before the account), waiting then found — and NEVER accepts or rejects by itself. The owner decides (panel or
Telegram: accept / reject / ban); an underpaid request can be topped up with a second TXID, the amounts summed."""

from __future__ import annotations

import json
import re
from datetime import timedelta

import httpx
import pytest
from sqlalchemy import select

from app import clock
from app.models import MembershipRequest, MembershipTx, User
from app.services import chain, payment_check
from tests import fake_telegram as tg
from tests.smtp_sink import SmtpSink

OURS_HEX = "11" * 20
WALLET = chain.tron_from_hex20(OURS_HEX)
OTHER_HEX = "44" * 20
FAKE_HEX = "22" * 20
USDT_HEX = chain.tron_to_hex20(chain.USDT["TRC20"]["contract"])
BSC_WALLET = "0x" + "55" * 20
BSC_USDT = chain.USDT["BEP20"]["contract"].lower()
HEAD = 5000
HOOK = "hook-secret-0123456789"


class FakeChain:
    """TronGrid (POST /wallet/...) and a BSC JSON-RPC node, from a table of transactions."""

    def __init__(self):
        self.tron: dict[str, dict] = {}
        self.bsc: dict[str, dict] = {}
        self.calls: list[str] = []
        self.down = False
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            return httpx.Response(503)
        body = json.loads(request.content or b"{}")
        if request.url.host == "trongrid.test":
            self.calls.append(request.url.path)
            if request.url.path == "/wallet/gettransactioninfobyid":
                info = dict(self.tron.get(body["value"], {}))
                if info:  # dated when asked: «age_s» before now (0 = just now, after the account was created)
                    info["blockTimeStamp"] = int((clock.timestamp() - info.pop("_age")) * 1000)
                return httpx.Response(200, json=info)
            if request.url.path == "/wallet/getnowblock":
                return httpx.Response(200, json={"block_header": {"raw_data": {"number": HEAD}}})
            return httpx.Response(404)
        self.calls.append(body["method"])
        if body["method"] == "eth_getTransactionReceipt":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": self.bsc.get(body["params"][0])})
        if body["method"] == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(HEAD)})
        if body["method"] == "eth_getBlockByNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": {"timestamp": hex(int(clock.timestamp()))}})
        return httpx.Response(400)

    def tron_tx(self, txid: str, usdt: float, *, to: str = OURS_HEX, contract: str = USDT_HEX, result: str = "SUCCESS",
                block: int = HEAD - 30, age_s: int = 0) -> None:
        micro = round(usdt * 1_000_000)
        self.tron[txid] = {"id": txid, "blockNumber": block, "_age": age_s, "receipt": {"result": result},
                           "log": [{"address": contract, "topics": [chain.TRANSFER_TOPIC, "0" * 24 + "33" * 20, "0" * 24 + to],
                                    "data": f"{micro:064x}"}]}

    def bsc_tx(self, txid: str, usdt: float, *, to: str = BSC_WALLET[2:], contract: str = BSC_USDT, status: str = "0x1") -> None:
        raw = round(usdt * 10 ** 18)
        self.bsc[txid] = {"status": status, "blockNumber": hex(HEAD - 40),
                          "logs": [{"address": contract, "topics": ["0x" + chain.TRANSFER_TOPIC, "0x" + "0" * 24 + "66" * 20,
                                                                     "0x" + "0" * 24 + to], "data": hex(raw)}]}


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


def _harness(make_harness, smtp, network="TRC20", wallet=WALLET, **extra):
    fake_tg, fake = tg.FakeTelegram(), FakeChain()
    settings = dict(TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=HOOK,
                    SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
                    CHAIN_CHECK_ENABLED=True, TRONGRID_URL="https://trongrid.test", BSC_RPC_URL="https://bsc.test/rpc",
                    ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)  # the clock jumps hours
    settings.update(extra)
    hx = make_harness(telegram_transport=fake_tg.transport, chain_transport=fake.transport, **settings)
    hx.default_member = False
    hx.tg, hx.chain, hx.smtp = fake_tg, fake, smtp
    hx.adm = hx.admin()
    r = hx.adm.put("/api/admin/payment-settings", json={"currency": "USDT", "network": network, "wallet": wallet})
    assert r.status_code == 200, r.text
    return hx


@pytest.fixture
def cx(make_harness, smtp):
    return _harness(make_harness, smtp)


def _tx(n: int) -> str:
    return f"{n:064x}"


def _pay(hx, c, txid):
    r = c.post("/api/membership/requests", json={"txid": txid})
    assert r.status_code == 201, r.text
    hx.state.pipeline.wait_idle()  # the first check runs in the background
    return r.json()["request"]["id"]


def _req(hx, rid) -> MembershipRequest:
    with hx.db() as db:
        req = db.get(MembershipRequest, rid)
        db.expunge(req)
        return req


def _check(hx, rid) -> dict:
    return next(r for r in hx.adm.get("/api/admin/membership").json()["requests"] if r["id"] == rid)["check"]


def _never_decided(hx, rid):
    req = _req(hx, rid)
    assert req.status == "pending" and req.decided_at is None and req.decided_by is None  # THE RULE
    with hx.db() as db:
        user = db.get(User, req.user_id)
        assert user.member_since is None


# ------------------------------------------------------------------ every case, never a decision


def test_match_is_shown_and_nothing_is_decided(cx):
    cx.chain.tron_tx(_tx(1), 50)
    rid = _pay(cx, cx.user(), _tx(1))
    c = _check(cx, rid)
    assert c["status"] == "match" and c["icon"] == "✅" and c["received"] == "50.00"
    assert c["txs"][0]["status"] == "ok" and c["txs"][0]["confirmations"] == 31
    assert "tronscan.org" in c["txs"][0]["explorer_url"]
    _never_decided(cx, rid)
    owner = cx.tg.last_text()  # the owner's Telegram message carries the result and the buttons
    assert "✅ مطابق — وصل 50.00 USDT" in owner
    kb = json.dumps(cx.tg.sent[-1]["reply_markup"])
    for action in ("accept", "reject", "ban", "recheck"):
        assert f"ms:{action}:{rid}" in kb
    assert "ms:topup" not in kb


@pytest.mark.parametrize("amount,status,diff", [(40, "under", "10.00"), (55.5, "over", "5.50")])
def test_under_and_over_show_the_difference(cx, amount, status, diff):
    cx.chain.tron_tx(_tx(2), amount)
    rid = _pay(cx, cx.user(), _tx(2))
    c = _check(cx, rid)
    assert c["status"] == status and c["diff"] == diff and c["icon"] == "⚠️"
    assert c["can_request_topup"] == (status == "under")
    assert ("ms:topup" in json.dumps(cx.tg.sent[-1]["reply_markup"])) == (status == "under")
    _never_decided(cx, rid)


def test_a_tolerance_makes_a_small_difference_a_match(make_harness, smtp):
    hx = _harness(make_harness, smtp, PAYMENT_AMOUNT_TOLERANCE=0.5)
    hx.chain.tron_tx(_tx(3), 49.7)
    assert _check(hx, _pay(hx, hx.user(), _tx(3)))["status"] == "match"


@pytest.mark.parametrize("kwargs,reason", [
    ({"to": OTHER_HEX}, "المستلم ليس محفظة المنصة"),
    ({"contract": FAKE_HEX}, "عملة مزيفة"),
    ({"result": "REVERT"}, "العملية فاشلة"),
    ({"age_s": 10 * 86400}, "أقدم من إنشاء الحساب"),
])
def test_mismatch_reasons(cx, kwargs, reason):
    cx.chain.tron_tx(_tx(4), 50, **kwargs)
    rid = _pay(cx, cx.user(), _tx(4))
    c = _check(cx, rid)
    assert c["status"] == "mismatch" and c["icon"] == "❌" and any(reason in r for r in c["reasons"]), c
    assert reason in cx.tg.last_text()
    _never_decided(cx, rid)


def test_a_txid_used_elsewhere_is_shown_as_used(cx):
    from app.models import VerificationRequest

    cx.chain.tron_tx(_tx(5), 50)
    a = cx.user()
    rid = _pay(cx, a, _tx(5))
    with cx.db() as db:  # the same TXID turns up in another record (an old star request)
        u = db.scalar(select(User).where(User.email == a.email))
        db.add(VerificationRequest(user_id=u.id, account_type="other", description="x", reason="x", wallet=WALLET, amount=50, currency="USDT",
                                   network="TRC20", txid=_tx(5).upper(), status="pending", created_at=clock.utcnow()))
        db.commit()
    r = cx.adm.post(f"/api/admin/membership/requests/{rid}/check")
    assert r.status_code == 200 and r.json()["check"]["status"] == "mismatch"
    assert "رقم العملية مستخدم من قبل" in r.json()["check"]["reasons"]
    _never_decided(cx, rid)
    # and the API still refuses the same TXID for a new request
    assert cx.user().post("/api/membership/requests", json={"txid": _tx(5)}).status_code == 409


def test_not_found_then_found_by_the_loop(cx):
    rid = _pay(cx, cx.user(), _tx(6))
    c = _check(cx, rid)
    assert c["status"] == "waiting" and c["icon"] == "⏳" and c["txs"][0]["status"] == "not_found"
    assert payment_check.due(cx.state) == []  # not due before the interval
    cx.chain.tron_tx(_tx(6), 50)  # the transfer reaches the chain
    clock.advance(cx.settings.MEMBERSHIP_CHECK_INTERVAL_MIN * 60 + 1)
    due = payment_check.due(cx.state)
    assert due == [rid] and payment_check.due(cx.state) == []  # claimed once (atomic): never checked twice
    payment_check.run(cx.state, rid)
    assert _check(cx, rid)["status"] == "match"
    assert cx.tg.calls.count("sendMessage") == 1 and "editMessageText" in cx.tg.calls  # the same message, updated
    _never_decided(cx, rid)


def test_waiting_for_confirmations_then_ok(cx):
    cx.chain.tron_tx(_tx(7), 50, block=HEAD - 2)  # 3 confirmations of 19
    rid = _pay(cx, cx.user(), _tx(7))
    c = _check(cx, rid)
    assert c["status"] == "waiting" and c["txs"][0]["status"] == "pending" and "3/19" in c["text"]
    cx.chain.tron_tx(_tx(7), 50, block=HEAD - 25)
    clock.advance(61)
    assert payment_check.due(cx.state) == [rid]
    payment_check.run(cx.state, rid)
    assert _check(cx, rid)["status"] == "match"


def test_not_found_for_too_long_becomes_not_found(cx):
    rid = _pay(cx, cx.user(), _tx(8))
    clock.advance(cx.settings.MEMBERSHIP_CHECK_MAX_HOURS * 3600 + 60)
    assert payment_check.due(cx.state) == [rid]
    payment_check.run(cx.state, rid)
    c = _check(cx, rid)
    assert c["status"] == "mismatch" and "لم يُعثر على العملية على البلوكشين" in c["reasons"]
    clock.advance(3600)
    assert payment_check.due(cx.state) == []  # stops checking
    _never_decided(cx, rid)


def test_node_down_is_retried_not_judged(cx):
    cx.chain.down = True
    rid = _pay(cx, cx.user(), _tx(9))
    assert _check(cx, rid)["txs"][0]["status"] == "error" and _check(cx, rid)["status"] == "waiting"
    cx.chain.down = False
    cx.chain.tron_tx(_tx(9), 50)
    clock.advance(cx.settings.MEMBERSHIP_CHECK_INTERVAL_MIN * 60 + 1)
    for r in payment_check.due(cx.state):
        payment_check.run(cx.state, r)
    assert _check(cx, rid)["status"] == "match"


def test_bep20_on_bsc(make_harness, smtp):
    hx = _harness(make_harness, smtp, network="BEP20", wallet=BSC_WALLET)
    good, fake, failed = "0x" + _tx(10), "0x" + _tx(11), "0x" + _tx(12)
    hx.chain.bsc_tx(good, 50)
    hx.chain.bsc_tx(fake, 50, contract="0x" + "77" * 20)
    hx.chain.bsc_tx(failed, 50, status="0x0")
    assert _check(hx, _pay(hx, hx.user(), good))["status"] == "match"
    assert "عملة مزيفة" in " ".join(_check(hx, _pay(hx, hx.user(), fake))["reasons"])
    assert "فاشلة" in " ".join(_check(hx, _pay(hx, hx.user(), failed))["reasons"])
    assert "eth_getTransactionReceipt" in hx.chain.calls


def test_the_check_can_be_turned_off(make_harness, smtp):
    hx = _harness(make_harness, smtp, CHAIN_CHECK_ENABLED=False)
    rid = _pay(hx, hx.user(), _tx(13))
    assert _check(hx, rid)["status"] == "off" and hx.chain.calls == []
    assert "الفحص الآلي متوقف" in hx.tg.last_text()


# ------------------------------------------------------------------ the owner decides


def _tg(hx, data):
    r = hx.client().post("/api/telegram/webhook", json=tg.callback(data),
                         headers={"X-Telegram-Bot-Api-Secret-Token": HOOK, "X-DZ-Requested": ""})
    assert r.status_code == 200
    hx.state.bot.wait_idle()
    hx.state.pipeline.wait_idle()


def test_owner_buttons_accept_and_ban(cx):
    cx.chain.tron_tx(_tx(20), 50)
    a = cx.user()
    rid = _pay(cx, a, _tx(20))
    _tg(cx, f"ms:accept:{rid}")
    assert _req(cx, rid).status == "accepted" and a.get("/api/me").json()["member"] is True
    assert "القرار: مقبول" in cx.tg.texts()[-1] or any("القرار: مقبول" in e.get("text", "") for e in cx.tg.edited)

    cx.chain.tron_tx(_tx(21), 50, contract=FAKE_HEX)
    b = cx.user()
    rid2 = _pay(cx, b, _tx(21))
    _tg(cx, f"ms:ban:{rid2}")  # first tap: asks to confirm, nothing happens yet
    assert _req(cx, rid2).status == "pending"
    assert "ms:banyes" in json.dumps(cx.tg.edited[-1].get("reply_markup"))
    _tg(cx, f"ms:banyes:{rid2}")
    assert _req(cx, rid2).status == "rejected"
    with cx.db() as db:
        assert db.scalar(select(User.status).where(User.email == b.email)) == "banned"
    actions = [e["action"] for e in cx.adm.get("/api/admin/audit").json()["entries"]]
    assert "membership_ban" in actions and "membership_reject" in actions


def test_panel_ban_and_recheck(cx):
    cx.chain.tron_tx(_tx(22), 50, to=OTHER_HEX)
    b = cx.user()
    rid = _pay(cx, b, _tx(22))
    r = cx.adm.post(f"/api/admin/membership/requests/{rid}/ban", json={"action": "ban", "note": "احتيال"})
    assert r.status_code == 200 and r.json()["status"] == "rejected" and r.json()["user_status"] == "banned"
    assert cx.adm.post(f"/api/admin/membership/requests/{rid}/ban", json={"action": "ban"}).status_code == 409


# ------------------------------------------------------------------ the missing amount


def test_underpaid_owner_asks_then_the_user_adds_a_second_txid(cx):
    cx.chain.tron_tx(_tx(30), 40)
    a = cx.user()
    a.put("/api/me/language", json={"lang": "fr"})
    rid = _pay(cx, a, _tx(30))
    assert a.get("/api/membership").json()["request"]["topup"] is None  # nothing shown before the owner asks
    assert a.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(31)}).status_code == 409
    r = cx.adm.post(f"/api/admin/membership/requests/{rid}/topup")
    assert r.status_code == 200 and r.json()["check"]["underpay_notified_at"]
    cx.state.pipeline.wait_idle()
    # e-mail and notification in the user's language: the exact amount, the same address and network
    text = cx.smtp.messages[-1]["msg"].get_body(("plain",)).get_content()
    assert "10.00 USDT" in text and WALLET in text and "TRC20" in text and "Montant manquant" in text
    notes = a.get("/api/notifications", headers={"X-DZ-Lang": "fr"}).json()["notifications"]
    assert "10.00" in notes[0]["data"]["text"] or "10,00" in notes[0]["data"]["text"]
    top = a.get("/api/membership").json()["request"]["topup"]
    assert top["missing"] == "10.00" and top["wallet"] == WALLET and top["network"] == "TRC20" and top["can_add"]
    # the second TXID: checked, summed → match; still never decided
    cx.chain.tron_tx(_tx(31), 10)
    r = a.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(31)})
    assert r.status_code == 201, r.text
    cx.state.pipeline.wait_idle()
    c = _check(cx, rid)
    assert c["status"] == "match" and c["received"] == "50.00" and [t["kind"] for t in c["txs"]] == ["primary", "topup"]
    _never_decided(cx, rid)
    assert "membership_topup_request" in [e["action"] for e in cx.adm.get("/api/admin/audit").json()["entries"]]
    # the rules: the same TXID twice, someone else's request, too many
    assert a.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(31)}).status_code == 409
    assert cx.user().post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(32)}).status_code == 404
    assert a.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(33)}).status_code == 201
    assert a.post(f"/api/membership/requests/{rid}/txids", json={"txid": _tx(34)}).status_code == 409  # 3 at most


def test_topup_from_telegram_and_only_when_underpaid(cx):
    cx.chain.tron_tx(_tx(40), 50)
    rid = _pay(cx, cx.user(), _tx(40))
    assert cx.adm.post(f"/api/admin/membership/requests/{rid}/topup").status_code == 409  # not underpaid
    cx.chain.tron_tx(_tx(41), 45)
    rid2 = _pay(cx, cx.user(), _tx(41))
    _tg(cx, f"ms:topup:{rid2}")
    assert _req(cx, rid2).underpay_notified_micro == 5_000_000


def test_auto_notify_underpayment(make_harness, smtp):
    hx = _harness(make_harness, smtp, AUTO_NOTIFY_UNDERPAYMENT=True)
    hx.chain.tron_tx(_tx(50), 30)
    a = hx.user()
    rid = _pay(hx, a, _tx(50))
    hx.state.pipeline.wait_idle()
    assert _req(hx, rid).underpay_notified_micro == 20_000_000
    assert a.get("/api/membership").json()["request"]["topup"]["missing"] == "20.00"
    assert re.search(r"20\.00 USDT", hx.smtp.messages[-1]["msg"].get_body(("plain",)).get_content())
    sent = len(hx.smtp.messages)
    payment_check.run(hx.state, rid, force=True)  # the same difference: asked once only
    hx.state.pipeline.wait_idle()
    assert len(hx.smtp.messages) == sent
    _never_decided(hx, rid)


def test_old_requests_are_checked_on_demand(cx):
    with cx.db() as db:  # a request from before phase 13 (no MembershipTx row)
        a = db.scalar(select(User).limit(1))
    c = cx.user()
    cx.chain.tron_tx(_tx(60), 50)
    rid = _pay(cx, c, _tx(60))
    with cx.db() as db:
        for t in db.execute(select(MembershipTx)).scalars():
            db.delete(t)
        req = db.get(MembershipRequest, rid)
        req.check_status = req.check_summary = req.received_micro = req.diff_micro = None
        db.commit()
    assert _check(cx, rid)["status"] is None and "لم يُفحص" in _check(cx, rid)["text"]
    r = cx.adm.post(f"/api/admin/membership/requests/{rid}/check")
    assert r.json()["check"]["status"] == "match"
    assert a is None or a  # (any user row; the query only proves the table is readable)


def test_base58_round_trip():
    assert chain.tron_to_hex20(chain.USDT["TRC20"]["contract"]) == "a614f803b6fd780986a42c78ec9c7f77e6ded13c"
    assert chain.tron_from_hex20("a614f803b6fd780986a42c78ec9c7f77e6ded13c") == chain.USDT["TRC20"]["contract"]
    with pytest.raises(ValueError):
        chain.tron_to_hex20("TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6x")  # bad checksum
    assert payment_check.usdt(12_345_678) == "12.34" and payment_check.usdt(-5_000_000) == "-5.00"
    assert clock.utcnow() - timedelta(seconds=1) < clock.utcnow()
