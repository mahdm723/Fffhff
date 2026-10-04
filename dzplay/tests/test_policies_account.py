"""V5 phase E/F: policy pages (live values, no JavaScript), «حذف حسابي», panel V5 endpoints."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from app.api.policies import PRINCIPLE
from app.models import AdminAuditLog, LedgerEntry, MediaItem, Post, User
from app.services import tunables
from tests import fake_telegram as tg
from tests.conftest import PASSWORD

STORAGE = "-1001111111111"


@pytest.fixture
def px(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID),
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE)
    hx.tg = fake
    return hx


def jpeg() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (400, 300), (200, 100, 50)).save(buf, "JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("slug", ["privacy", "terms", "guidelines", "verification", "earnings"])
def test_policy_pages_start_with_the_principle_and_have_no_script(hx, slug):
    r = hx.client().get(f"/policies/{slug}")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert PRINCIPLE in r.text and "<script" not in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]


def test_policies_index_404_and_live_values(hx):
    c = hx.client()
    assert c.get("/policies").status_code == 200
    assert c.get("/policies/nope").status_code == 404
    page = c.get("/policies/privacy").text
    assert f"{hx.settings.CHAT_IMAGE_TTL_AFTER_VIEW} ثانية" in page and "لا نؤرشف صور المحادثات" in page
    assert "Telegram" in page and "GPS" in page and "18" in page and "حذف حسابي" in page
    with hx.db() as db:
        tunables.save(db, hx.settings, {"CHAT_IMAGE_TTL_AFTER_VIEW": 45, "CHAT_IMAGE_ARCHIVE": True}, "owner")
    hx.state.reload_tunables()
    page = c.get("/policies/privacy").text
    assert "45 ثانية" in page and "إعداد الأرشفة مفعّل" in page
    ver = c.get("/policies/verification").text
    assert "ليست تحققًا من الهوية الحقيقية" in ver and "نهائي" in ver and "شبكة خاطئة" in ver
    assert "/policies/privacy" in c.get("/download").text


def test_privacy_notice_shown_again_after_v5(hx):
    a = hx.user()
    with hx.db() as db:
        db.query(User).filter_by(email=a.email).one().privacy_ack_version = 4  # acknowledged the V4 policy
    assert a.get("/api/me").json()["privacy_notice"] is True
    a.post("/api/me/privacy-ack")
    assert a.get("/api/me").json()["privacy_notice"] is False


def test_delete_my_account_removes_everything_and_storage(px):
    a, b = px.user(), px.user()
    r = a.post("/api/uploads?purpose=idea", content=jpeg())
    px.state.pipeline.wait_idle()
    mid = r.json()["upload"]["id"]
    a.post("/api/posts", json={"content": "سأحذف حسابي", "media_id": mid})
    a.post("/api/support/tickets", json={"category": "other", "body": "تجربة"})
    px.state.pipeline.wait_idle()
    with px.db() as db:
        uid = db.query(User).filter_by(email=a.email).one().id
    assert a.post("/api/me/delete", json={"password": "wrong-password"}).status_code == 403
    assert b.post("/api/me/delete", json={}).status_code == 403  # b keeps its account
    r = a.post("/api/me/delete", json={"password": PASSWORD})
    assert r.status_code == 200
    px.state.pipeline.wait_idle()
    assert a.get("/api/me").status_code == 401
    with px.db() as db:
        assert db.get(User, uid) is None
        assert db.query(Post).filter_by(author_id=uid).count() == 0
        item = db.get(MediaItem, mid)
        assert item.state == "removed" and item.owner_id is None and item.tg_file_id is None
        assert db.query(LedgerEntry).filter_by(user_id=uid).count() == 0
        assert db.query(AdminAuditLog).filter_by(action="account_self_delete").count() == 1
    assert (STORAGE, px.tg.documents[0]["message_id"]) in px.tg.deleted
    assert px.login(px.client(), a.email).status_code == 401


def test_google_only_account_confirms_with_text(hx):
    a = hx.user()
    with hx.db() as db:
        u = db.query(User).filter_by(email=a.email).one()
        u.password_hash, u.google_sub = None, "google-sub-123"
    assert a.post("/api/me/delete", json={"confirm": "نعم"}).status_code == 400
    assert a.post("/api/me/delete", json={"confirm": "حذف حسابي"}).status_code == 200


def test_admin_media_preview_and_user_v5_page(px):
    a = px.user()
    r = a.post("/api/uploads?purpose=idea", content=jpeg())
    px.state.pipeline.wait_idle()
    mid = r.json()["upload"]["id"]
    a.post("/api/posts", json={"content": "x", "media_id": mid})
    assert a.get(f"/test-panel/api/admin/media/{mid}/img").status_code == 401
    adm = px.admin()
    img = adm.get(f"/api/admin/media/{mid}/img")
    assert img.status_code == 200 and img.headers["cache-control"] == "no-store"
    with px.db() as db:
        uid = db.query(User).filter_by(email=a.email).one().id
    page = adm.get(f"/api/admin/users/{uid}/v5").json()
    assert page["verified"] is False and page["media"][0]["id"] == mid and page["media"][0]["previewable"] is True
    assert adm.get("/api/admin/media?filter=published").json()["items"][0]["id"] == mid
