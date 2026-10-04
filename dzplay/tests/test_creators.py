"""V5 phase D: creator studio reels, monetization, immutable ledger («أموالي»)."""

from __future__ import annotations

import subprocess

import pytest

from app.models import AdminAuditLog, LedgerEntry, User
from tests import fake_telegram as tg
from tests.conftest import ffmpeg_binary
from tests.smtp_sink import SmtpSink

STORAGE, MODCHAT = "-1001111111111", "-1002222222222"


@pytest.fixture(scope="module")
def clip(tmp_path_factory) -> bytes:
    out = tmp_path_factory.mktemp("clip") / "clip.mp4"
    subprocess.run([ffmpeg_binary(), "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc=size=360x640:rate=25", "-f", "lavfi", "-i", "sine=frequency=440", "-t", "4",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(out)], check=True, timeout=120)
    return out.read_bytes()


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


@pytest.fixture
def dx(make_harness, smtp):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_WEBHOOK_SECRET="s3cret-hook", TELEGRAM_STORAGE_CHANNEL_ID=STORAGE,
                      TELEGRAM_MODERATION_CHAT_ID=MODCHAT, SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none",
                      SMTP_FROM="DZPLAY <no-reply@dzplay.test>", MONETIZE_MIN_REELS=1, MONETIZE_MIN_LIKES=0)
    hx.tg, hx.smtp = fake, smtp
    return hx


def idle(hx):
    hx.state.pipeline.wait_idle()
    hx.state.bot.wait_idle()


def deliver(hx, update):
    r = hx.client().post("/api/telegram/webhook", json=update,
                         headers={"X-Telegram-Bot-Api-Secret-Token": "s3cret-hook", "X-DZ-Requested": ""})
    assert r.status_code == 200
    idle(hx)


def uid(hx, c) -> str:
    with hx.db() as db:
        return db.query(User).filter_by(email=c.email).one().id


def creator(hx, name="Nour"):
    c = hx.user()
    c.patch("/api/me/profile", json={"display_name": name})
    assert hx.admin().post(f"/api/admin/users/{uid(hx, c)}/verified", json={"verified": True}).status_code == 200
    return c


def upload_reel(hx, c, clip) -> str:
    r = c.post("/api/uploads?purpose=reel", content=clip)
    assert r.status_code == 202, r.text
    idle(hx)
    st = c.get(f"/api/uploads/{r.json()['upload']['id']}").json()["upload"]
    assert st["state"] == "ready", st
    return st["id"]


def feed_ids(c) -> dict:
    return {r["id"]: r for r in c.get("/api/reels/feed").json()["reels"]}


def test_studio_is_for_verified_accounts_only(dx, clip):
    a = dx.user()
    assert a.get("/api/studio").status_code == 403
    assert a.post("/api/uploads?purpose=reel", content=clip).status_code == 403
    assert a.post("/api/studio/reels", json={"media_id": "a" * 32}).status_code == 403


def test_reel_review_publish_with_name_profile_and_limits(dx, clip):
    a, b = creator(dx), dx.user()
    mid = upload_reel(dx, a, clip)
    doc = dx.tg.documents[-1]
    assert doc["chat_id"] == STORAGE and doc["mime"] == "video/mp4" and len(doc["content"]) <= 18 * 1024 * 1024
    r = a.post("/api/studio/reels", json={"media_id": mid, "caption": "أول فيديو لي", "show_author": True, "show_on_profile": True})
    assert r.status_code == 201, r.text
    reel = r.json()["reel"]
    assert reel["status"] == "pending" and reel["visible"] is False and reel["poster"]
    assert a.get(reel["poster"]).status_code == 200  # the owner can preview it
    assert reel["id"] not in feed_ids(b)
    idle(dx)
    copy = dx.tg.copies[-1]
    assert copy["chat_id"] == MODCHAT and "Reel" in copy["caption"] and "أول فيديو لي" in copy["caption"]
    datas = [btn["callback_data"] for row in copy["reply_markup"]["inline_keyboard"] for btn in row]
    assert f"md:ok:{mid}" in datas and f"md:no:{mid}" in datas
    # daily limit (1) with a countdown
    assert a.post("/api/uploads?purpose=reel", content=clip).status_code == 429
    assert a.get("/api/studio").json()["quota"]["next_at"]
    # approved from the moderation group → in the random feed with name + star, on the profile
    deliver(dx, tg.callback(f"md:ok:{mid}", sender=555, chat=int(MODCHAT)))
    shown = feed_ids(b)[reel["id"]]
    assert shown["author"]["name"] == "Nour" and shown["author"]["verified"] is True and shown["reportable"] is True
    assert b.get(shown["media"][0]["src"]).status_code == 200
    ref = shown["author"]["ref"]
    assert [x["id"] for x in b.get(f"/api/profiles/{ref}/reels").json()["reels"]] == [reel["id"]]
    # the owner hides the name → looks like platform content and leaves the profile
    upd = a.patch(f"/api/studio/reels/{reel['id']}", json={"show_author": False, "show_on_profile": True}).json()["reel"]
    assert upd["show_author"] is False and upd["show_on_profile"] is False
    assert feed_ids(b)[reel["id"]]["author"] is None
    assert b.get(f"/api/profiles/{ref}/reels").json()["reels"] == []
    assert b.patch(f"/api/studio/reels/{reel['id']}", json={"show_author": True}).status_code in (403, 404)
    # delete: gone from the feed and from Telegram
    assert b.delete(f"/api/studio/reels/{reel['id']}").status_code in (403, 404)
    assert a.delete(f"/api/studio/reels/{reel['id']}").status_code == 200
    idle(dx)
    assert reel["id"] not in feed_ids(b)
    assert (STORAGE, doc["message_id"]) in dx.tg.deleted


def test_reel_without_name_rejection_and_reports(dx, clip):
    a, b, c = creator(dx, "Sara"), dx.user(), dx.user()
    with dx.db() as db:
        from app.services import tunables

        tunables.save(db, dx.settings, {"REELS_REQUIRE_APPROVAL": False, "CREATOR_REEL_LIMIT_PER_24H": 3,
                                        "REPORT_AUTO_HIDE_THRESHOLD": 2}, "owner")
    dx.state.reload_tunables()
    mid = upload_reel(dx, a, clip)
    reel = a.post("/api/studio/reels", json={"media_id": mid, "caption": "بدون اسم", "show_on_profile": True}).json()["reel"]
    assert reel["visible"] is True and reel["show_on_profile"] is False  # without the name: never on the profile
    shown = feed_ids(b)[reel["id"]]
    assert shown["author"] is None
    assert b.post(f"/api/reels/{reel['id']}/report", json={"reason": "spam"}).status_code == 201
    assert c.post(f"/api/reels/{reel['id']}/report", json={"reason": "inappropriate"}).status_code == 201
    assert reel["id"] not in feed_ids(b)  # hidden after 2 reporters, until reviewed
    idle(dx)
    deliver(dx, tg.callback(f"md:keep:{mid}"))
    assert reel["id"] in feed_ids(b)
    # rejection with a reason, visible to the owner in the studio
    mid2 = upload_reel(dx, a, clip)
    r2 = a.post("/api/studio/reels", json={"media_id": mid2, "show_author": True}).json()["reel"]
    idle(dx)
    deliver(dx, tg.callback(f"md:no:{mid2}"))
    mine = {r["id"]: r for r in a.get("/api/studio").json()["reels"]}
    assert mine[r2["id"]]["status"] == "rejected" and mine[r2["id"]]["note"] and "src" not in mine[r2["id"]]
    assert r2["id"] not in feed_ids(b)
    # platform reels cannot be reported this way
    assert b.post("/api/reels/" + "x" * 20 + "/report", json={"reason": "spam"}).status_code == 404


def test_monetization_email_code_ledger_and_red_packet(dx, clip):
    a = creator(dx, "Karim")
    aid = uid(dx, a)
    assert dx.user().get("/api/monetization").status_code == 403  # verified users only
    assert a.get("/api/money").status_code == 403
    ov = a.get("/api/monetization").json()
    assert ov["conditions"]["met"] is False
    with dx.db() as db:
        from app.services import tunables

        tunables.save(db, dx.settings, {"REELS_REQUIRE_APPROVAL": False}, "owner")
    dx.state.reload_tunables()
    mid = upload_reel(dx, a, clip)
    a.post("/api/studio/reels", json={"media_id": mid, "show_author": True})
    assert a.get("/api/monetization").json()["conditions"]["met"] is True
    body = {"content_type": "مقاطع تعليمية قصيرة", "payout_email": "wallet@pay.test", "terms": True}
    assert a.post("/api/monetization", json=body).json()["error"]["code"] == "code_required"
    assert a.post("/api/monetization/email-code", json={"email": "wallet@pay.test"}).json()["needed"] is True
    idle(dx)
    code = next(w for w in dx.smtp.last_text().split() if w.isdigit() and len(w) == 6)
    assert dx.smtp.messages[-1]["to"] == ["wallet@pay.test"]
    assert a.post("/api/monetization", json={**body, "code": "000000" if code != "000000" else "111111"}).json()["error"]["code"] == "wrong_code"
    assert a.post("/api/monetization", json={**body, "code": code, "terms": False}).json()["error"]["code"] == "terms_required"
    r = a.post("/api/monetization", json={**body, "code": code})
    assert r.status_code == 201, r.text
    idle(dx)
    notice = next(m for m in dx.tg.sent if "تحقيق دخل" in m.get("text", ""))
    app_id = r.json()["application"]["id"]
    adm = dx.admin()
    # not accepted yet: no ledger writes
    assert adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "earning", "amount": "5"}).status_code == 409
    deliver(dx, tg.callback(f"mn:accept:{app_id}"))
    assert "accept" in notice["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    assert a.get("/api/profile").json()["monetized"] is True
    e1 = adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "earning", "amount": "10.50", "note": "أرباح سبتمبر"}).json()
    assert e1["amount"] == "10.50"
    assert adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "earning", "amount": "1.005"}).status_code == 400
    assert adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "payout", "amount": "20", "paid_on": "2026-10-04"}).json()["error"]["code"] == "insufficient_balance"
    assert adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "payout", "amount": "4"}).json()["error"]["code"] == "invalid_date"
    p = adm.post(f"/api/admin/users/{aid}/ledger", json={"kind": "payout", "amount": "4", "paid_on": "2026-10-04",
                                                        "note": "Red Packet"}).json()
    assert p["amount"] == "-4.00"
    idle(dx)
    assert dx.smtp.messages[-1]["to"] == ["wallet@pay.test"] and "الظرف الأحمر" in dx.smtp.last_text()
    money = a.get("/api/money").json()
    assert money["balance"] == "6.50" and money["total_earned"] == "10.50" and len(money["history"]) == 2
    # corrections only by reversal; each entry once
    rv = adm.post(f"/api/admin/ledger/{p['id']}/reverse", json={"note": "أُرسل بالخطأ"}).json()
    assert rv["amount"] == "4.00" and rv["reverses"] == p["id"]
    assert adm.post(f"/api/admin/ledger/{p['id']}/reverse", json={}).status_code == 409
    assert adm.post(f"/api/admin/ledger/{rv['id']}/reverse", json={}).status_code == 400
    assert a.get("/api/money").json()["balance"] == "10.50"
    with dx.db() as db:
        assert db.query(LedgerEntry).filter_by(user_id=aid).count() == 3  # nothing edited or deleted
        actions = [x.action for x in db.query(AdminAuditLog).all()]
    assert {"ledger_earning", "ledger_payout", "ledger_reversal", "monetize_accept"} <= set(actions)
    # users can never write money
    assert a.post(f"/test-panel/api/admin/users/{aid}/ledger", json={"kind": "earning", "amount": "999"}).status_code == 401
    assert a.post("/api/money", json={"balance": 999}).status_code in (404, 405)
