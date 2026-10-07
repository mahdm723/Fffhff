"""V4: /download page, /api/app/version, Android FCM tokens, and the closed-app ring (no identity inside)."""

from __future__ import annotations

import hashlib

from sqlalchemy import func, select

from app.models import FcmToken
from app.services import app_release, fcm
from tests.conftest import send

TOKEN = "fGx1:APA91b" + "x" * 140


def test_download_page_and_version(hx):
    c = hx.client()
    r = c.get("/download")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    rel = app_release.info()
    assert rel and rel["sha256"] == hashlib.sha256(app_release.APK.read_bytes()).hexdigest()
    assert rel["sha256"] in r.text and "<svg" in r.text and "إضافة إلى الشاشة الرئيسية" in r.text
    assert "Content-Security-Policy" in r.headers and "style=" not in r.text  # no inline styles (CSP)
    v = c.get("/api/app/version").json()
    assert v["available"] is True and v["url"] == "/download/dzplay.apk" and v["version_code"] >= 3
    assert c.get("/download/").status_code == 404  # no directory listing


def test_fcm_token_register_validate_reassign_remove(hx):
    a, b = hx.user(), hx.user()
    assert a.post("/api/push/fcm", json={"token": "bad token!"}).status_code == 400
    assert hx.client().post("/api/push/fcm", json={"token": TOKEN}).status_code == 401
    assert a.post("/api/push/fcm", json={"token": TOKEN}).json() == {"ok": True, "enabled": False}
    assert b.post("/api/push/fcm", json={"token": TOKEN}).status_code == 200  # same phone, other account
    with hx.db() as db:
        assert db.scalar(select(func.count()).select_from(FcmToken)) == 1
    assert a.post("/api/push/fcm/remove", json={"token": TOKEN}).status_code == 200  # not a's any more: no-op
    with hx.db() as db:
        assert db.scalar(select(func.count()).select_from(FcmToken)) == 1
    b.post("/api/push/fcm/remove", json={"token": TOKEN})
    with hx.db() as db:
        assert db.scalar(select(func.count()).select_from(FcmToken)) == 0


def test_push_payloads_carry_no_identity():
    m = fcm.chat_message(TOKEN)["message"]
    assert m["data"] == {"type": "message"} and "notification" not in m
    assert not hasattr(fcm, "call_message")  # V6: calls were removed
    from app.services import push

    assert not hasattr(push, "CALL_PAYLOAD") and set(push.PUSH_PAYLOAD) == {"title", "body", "url"}


def test_offline_recipient_gets_a_message_alert_only(hx):
    sent: list = []

    class FakeFcm:
        def notify_message(self, user_id):
            sent.append(user_id)

        def shutdown(self):
            pass

    hx.state.fcm = FakeFcm()
    a, b = hx.user(), hx.user()
    send(a, "مرحبًا")
    with hx.db() as db:
        from app.models import User

        b_id = db.query(User).filter_by(email=b.email).one().id
    assert sent == [b_id]
    assert a.post("/api/calls", json={"conversation_id": "x" * 22, "kind": "audio"}).status_code in (404, 405)
