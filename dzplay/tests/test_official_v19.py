"""V6 phase 19: the official account (DALTA.BIT) with its gold badge — reserved name, posting from the panel (2FA)
and from the bot (/post, confirmed), pinning, visibility, no messages, outside the money."""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.errors import AppError
from app.main import _ADMIN_ASSETS, ADMIN_STATIC_DIR
from app.models import AdminAuditLog, OfficialDraft, Post, User
from app.services import admin_content, ledger, names, verification
from tests import fake_telegram as tg
from tests.conftest import send
from tests.test_uploads import MODCHAT, STORAGE, deliver, jpeg


def _step_up(hx) -> str:
    from app.security import totp

    hx.admin()
    clock.advance(totp.STEP)  # one code per step
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


def _official(hx) -> User:
    with hx.db() as db:
        u = db.scalar(select(User).where(User.is_official.is_(True)))
        db.expunge(u)
        return u


def _publish(hx, content="إعلان رسمي", media_id=None, code=None):
    return hx.admin().post("/api/admin/official/posts",
                           json={"content": content, "media_id": media_id, "code": code or _step_up(hx)})


def _audit(hx, action: str) -> list[AdminAuditLog]:
    with hx.db() as db:
        return db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == action)).all()


@pytest.fixture
def bx(make_harness):
    fake = tg.FakeTelegram()
    hx = make_harness(telegram_transport=fake.transport, TELEGRAM_BOT_TOKEN=tg.TOKEN,
                      TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET="hook-secret-0123456789",
                      TELEGRAM_STORAGE_CHANNEL_ID=STORAGE, TELEGRAM_MODERATION_CHAT_ID=MODCHAT)
    hx.tg = fake
    return hx


# ----------------------------------------------------------------- the account


def test_one_official_account_from_startup_named_after_the_app(hx):
    u = _official(hx)
    assert u.display_name == hx.settings.APP_NAME and u.public_id and u.password_hash is None and u.google_sub is None
    # an old row (the V5 «DZPLAY» account) is renamed; a second official row is never kept
    with hx.db() as db:
        row = db.get(User, u.id)
        row.display_name, row.password_hash = "DZPLAY", "x"
        db.add(User(email="second@dzplay.invalid", status="active", is_official=True, privacy_ack_version=1))
        db.commit()
        admin_content.ensure_official(db, hx.settings)
        db.commit()
        rows = db.scalars(select(User).where(User.is_official.is_(True))).all()
        assert [r.id for r in rows] == [u.id] and rows[0].display_name == hx.settings.APP_NAME
        assert rows[0].password_hash is None and rows[0].status == "active"
    assert hx.login(hx.client(), "official@dzplay.invalid", "anything-at-all").status_code == 401


@pytest.mark.parametrize("name", [
    "DALTA.BIT", "dalta bit", "Dalta_Bit", "dalta-bit", "D4LTA.B1T", "DELTA BIT", "Team DALTA BIT", "DaltaBit Official",
    "دلتا بت", "دلتا بت الرسمي", "دالتا بيت", "دلتابيت", "دالتابت", "ديلتا بيت", "فريق دالتا بيت",
])
def test_the_official_name_is_reserved_in_every_spelling(hx, name):
    with pytest.raises(AppError) as err:
        names.clean(name, hx.settings)
    assert err.value.code == "reserved_name", name


@pytest.mark.parametrize("name", ["Ali ⭐", "Ali ★", "Ali ✪", "Ali ✔", "Ali ✓", "Ali ✅", "Ali ☑", "Ali 🏅", "Ali 🎖",
                                  "Ali 🥇", "Ali 👑", "Ali 🏆", "Ali 🌟", "Ali ✨", "علي √"])
def test_badge_symbols_are_refused_with_their_own_message(hx, name):
    with pytest.raises(AppError) as err:
        names.clean(name, hx.settings)
    assert err.value.code == "badge_symbol_name", name


def test_ordinary_names_still_pass(hx):
    for ok in ("Delta Trader", "Bit Master", "دلتا", "أحمد بن علي", "Yacine_88"):
        assert names.clean(ok, hx.settings) == ok
    a = hx.user()
    r = a.patch("/api/me/profile", json={"display_name": "دلتا بت الرسمي"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "reserved_name"


# ----------------------------------------------------------------- posting from the panel


def test_panel_post_needs_the_2fa_code_and_is_audited(hx):
    adm = hx.admin()
    r = adm.post("/api/admin/official/posts", json={"content": "x", "code": "000000"})
    assert r.status_code in (400, 401, 403) and not _audit(hx, "official_post")
    assert hx.user().post("/test-panel/api/admin/official/posts", json={"content": "x", "code": "1"}).status_code == 401
    r = _publish(hx, "مرحبًا بكم في المنصة 👋")
    assert r.status_code == 201, r.text
    p = r.json()
    assert p["author"]["name"] == hx.settings.APP_NAME and p["author"]["official"] is True and p["author"]["verified"] is False
    assert [a.actor for a in _audit(hx, "official_post")] == ["admin:owner"]
    home = adm.get("/api/admin/official").json()
    assert home["account"]["public_id"] == _official(hx).public_id and home["account"]["posts_today"] == 1
    assert [x["id"] for x in home["posts"]] == [p["id"]]


def test_panel_post_with_a_picture_goes_through_the_same_checks(bx):
    adm = bx.admin()
    r = adm.post("/api/admin/official/uploads", content=jpeg(), headers={"Content-Type": "application/octet-stream"})
    assert r.status_code == 202, r.text
    mid = r.json()["upload"]["id"]
    bx.state.pipeline.wait_idle()
    assert adm.get(f"/api/admin/official/uploads/{mid}").json()["upload"]["state"] == "ready"
    assert bx.tg.documents and bx.tg.documents[-1]["chat_id"] == STORAGE  # stored in Telegram like every picture
    r = _publish(bx, "", media_id=mid)
    assert r.status_code == 201, r.text
    assert r.json()["media"]["kind"]  # (no signed URL in the panel: URLs are bound to a user session)
    seen = bx.user().get(f"/api/posts/{r.json()['id']}").json()
    assert seen["media"]["url"].startswith(f"/media/{mid}/img?") and seen["author"]["official"] is True
    # a user's upload is not the official account's, and someone else's id is refused
    assert adm.get("/api/admin/official/uploads/" + "a" * 32).status_code == 404
    assert adm.post("/api/admin/official/uploads", content=b"", headers={"Content-Type": "application/octet-stream"}).status_code == 413


def test_daily_limit_is_off_by_default_and_adjustable(make_harness):
    hx = make_harness(OFFICIAL_POSTS_PER_DAY=0)
    for i in range(4):
        assert _publish(hx, f"منشور رقم {i}").status_code == 201
    hx.settings.OFFICIAL_POSTS_PER_DAY = 5
    assert _publish(hx, "الخامس").status_code == 201
    r = _publish(hx, "السادس")
    assert r.status_code == 429 and r.json()["error"]["code"] == "official_limit"


# ----------------------------------------------------------------- pinning, the feed, deleting


def test_pin_puts_the_post_first_then_it_expires(hx):
    a, b = hx.user(), hx.user()
    for i in range(3):
        a.post("/api/posts", json={"content": f"فكرة عادية رقم {i}"})
    pid = _publish(hx, "إعلان مثبّت").json()["id"]
    adm = hx.admin()
    assert adm.post(f"/api/admin/official/posts/{pid}/pin", json={"hours": 999}).status_code in (400, 422)
    r = adm.post(f"/api/admin/official/posts/{pid}/pin", json={"hours": 24})
    assert r.status_code == 200 and r.json()["pinned_until"]
    for _ in range(3):  # always first on the first page, and only there
        data = b.get("/api/posts/feed", params={"limit": 2}).json()
        assert data["posts"][0]["id"] == pid and data["posts"][0]["pinned"] is True
        more = b.get("/api/posts/feed", params={"limit": 10, "cursor": data["next_cursor"]}).json()
        assert pid not in [p["id"] for p in more["posts"]]
    assert {x.action for x in _audit(hx, "official_pin")} == {"official_pin"}
    # only the official account's posts
    mine = a.post("/api/posts", json={"content": "فكرة مستخدم"}).json()["id"]
    assert adm.post(f"/api/admin/official/posts/{mine}/pin", json={"hours": 6}).status_code == 404
    # one pinned post at a time; «إلغاء التثبيت»
    other = _publish(hx, "إعلان آخر").json()["id"]
    adm.post(f"/api/admin/official/posts/{other}/pin", json={"hours": 6})
    with hx.db() as db:
        assert db.scalars(select(Post.id).where(Post.pinned_until.is_not(None))).all() == [other]
    assert adm.post(f"/api/admin/official/posts/{other}/pin", json={"hours": 0}).json()["pinned_until"] is None
    assert _audit(hx, "official_unpin")
    adm.post(f"/api/admin/official/posts/{pid}/pin", json={"hours": 24})
    clock.advance(25 * 3600)  # unpins itself
    data = b.get("/api/posts/feed").json()
    assert all(p["pinned"] is False for p in data["posts"]) and pid in [p["id"] for p in data["posts"]]


def test_delete_an_official_post(hx):
    pid = _publish(hx, "سيُحذف").json()["id"]
    adm = hx.admin()
    assert adm.delete(f"/api/admin/official/posts/{pid}").status_code == 200
    assert hx.user().get(f"/api/posts/{pid}").status_code == 404
    assert _audit(hx, "official_delete") and adm.get("/api/admin/official").json()["posts"] == []


def test_official_reply_to_a_comment(hx):
    owner, fan = hx.user(), hx.user()
    pid = owner.post("/api/posts", json={"content": "فكرة للنقاش"}).json()["id"]
    cid = fan.post(f"/api/posts/{pid}/comments", json={"content": "سؤال للإدارة"}).json()["comment"]["id"]
    r = hx.admin().post("/api/admin/official-comment",
                        json={"target": "idea", "target_id": pid, "text": "شكرًا على سؤالك", "parent_id": cid})
    assert r.status_code == 201, r.text
    rows = owner.get(f"/api/posts/{pid}/comments").json()["comments"]
    assert [c["id"] for c in rows] == [cid]  # a reply sits in its thread
    reply = rows[0]["replies"][0]
    assert reply["content"] == "شكرًا على سؤالك"
    assert reply["parent_id"] == cid and reply["author"]["official"] is True and reply["author"]["name"] == hx.settings.APP_NAME
    kinds = [n["kind"] for n in fan.get("/api/notifications").json()["notifications"]]
    assert "reply" in kinds
    assert hx.admin().post("/api/admin/official-comment", json={
        "target": "idea", "target_id": pid, "text": "x", "parent_id": "nope"}).status_code == 404


# ----------------------------------------------------------------- visibility


def test_the_gold_badge_everywhere_and_users_first(hx):
    viewer, other = hx.user(), hx.user()
    pid = _publish(hx, "منشور رسمي").json()["id"]
    off = _official(hx)
    post = viewer.get(f"/api/posts/{pid}").json()
    assert post["author"]["official"] is True and post["author"]["name"] == hx.settings.APP_NAME
    profile = viewer.get(f"/api/profiles/{post['author']['ref']}").json()
    assert profile["official"] is True and profile["verified"] is False and profile["public_id"] == off.public_id
    listed = viewer.get("/api/people").json()["results"]
    assert listed[0]["public_id"] == off.public_id and listed[0]["official"] is True
    assert all(p["official"] is False for p in listed[1:])
    found = viewer.get("/api/people/search", params={"q": off.public_id.lower()}).json()["results"]
    assert [p["public_id"] for p in found] == [off.public_id] and found[0]["official"] is True
    by_name = viewer.get("/api/people/search", params={"q": "dalta"}).json()["results"]
    assert by_name and by_name[0]["official"] is True
    # never the blue star, even if someone tried to grant it
    with hx.db() as db, pytest.raises(AppError):
        verification.grant(db, db.get(User, off.id), "admin:owner")
    assert other.get(f"/api/posts/{pid}").json()["author"]["verified"] is False


def test_messages_to_it_are_refused_with_the_support_reply(hx):
    a = hx.user()
    off = _official(hx)
    r = send(a, off.public_id, "مرحبا")
    assert r.status_code == 403 and r.json()["error"]["code"] == "official_no_dm"
    assert "الدعم" in r.json()["error"]["message"]
    card = a.get(f"/api/people/{off.public_id}").json()
    assert card.get("official") is True and card.get("can_message") is False


def test_outside_the_money(hx):
    off = _official(hx)
    with hx.db() as db, pytest.raises(AppError) as err:
        ledger.post(db, user_id=off.id, account="rewards", kind="contest", amount_minor=100, source_type="test",
                    source_id="x", actor="admin:owner")
    assert err.value.code == "official_account"
    assert hx.admin().get("/api/admin/stats").json()["users"]["total"] == 0


# ----------------------------------------------------------------- the bot: /post


def _draft(hx) -> OfficialDraft:
    with hx.db() as db:
        d = db.scalars(select(OfficialDraft).order_by(OfficialDraft.created_at.desc())).first()
        db.expunge(d)
        return d


def test_bot_post_text_is_confirmed_before_publishing(bx):
    deliver(bx, tg.text("/post أخبار السوق اليوم"))
    d = _draft(bx)
    assert d.status == "pending" and d.content == "أخبار السوق اليوم"
    assert "انشره؟" in bx.tg.last_text()
    with bx.db() as db:
        assert db.scalar(select(Post).where(Post.author_id == _official(bx).id)) is None  # nothing before «نشر»
    deliver(bx, tg.callback(f"op:yes:{d.id[:24]}"))
    bx.state.bot.wait_idle()
    assert _draft(bx).status == "posted" and "✅ نُشر" in bx.tg.last_text()
    with bx.db() as db:
        post = db.scalar(select(Post).where(Post.author_id == _official(bx).id))
        assert post.content == "أخبار السوق اليوم"
    assert _audit(bx, "official_post")[0].actor == f"telegram:{tg.ADMIN_ID}"
    deliver(bx, tg.callback(f"op:yes:{d.id[:24]}"))  # a second tap publishes nothing more
    with bx.db() as db:
        assert len(db.scalars(select(Post).where(Post.author_id == _official(bx).id)).all()) == 1


def test_bot_post_cancel_and_strangers(bx):
    deliver(bx, tg.text("/post لن يُنشر"))
    d = _draft(bx)
    deliver(bx, tg.callback(f"op:no:{d.id[:24]}"))
    assert _draft(bx).status == "cancelled"
    deliver(bx, tg.text("/post من شخص غريب", sender=tg.STRANGER_ID))
    deliver(bx, tg.photo(bx.tg.add_file(jpeg()), 50_000, caption="غريب", sender=tg.STRANGER_ID))
    with bx.db() as db:
        assert len(db.scalars(select(OfficialDraft)).all()) == 1
        assert db.scalar(select(Post).where(Post.author_id == _official(bx).id)) is None
    deliver(bx, tg.text("/post"))  # empty: the help line, no draft
    assert "/post" in bx.tg.last_text() and len(bx.tg.texts()) >= 1


def test_bot_photo_with_a_caption_goes_through_the_picture_checks(bx):
    data = jpeg()
    deliver(bx, tg.photo(bx.tg.add_file(data), len(data), caption="صورة اليوم"))
    d = _draft(bx)
    assert d.tg_file_id and d.content == "صورة اليوم" and "مع صورة" in bx.tg.last_text()
    deliver(bx, tg.callback(f"op:yes:{d.id[:24]}"))
    bx.state.bot.wait_idle()
    assert _draft(bx).status == "posted", bx.tg.texts()[-3:]
    with bx.db() as db:
        post = db.scalar(select(Post).where(Post.author_id == _official(bx).id))
        assert post.media_id and post.content == "صورة اليوم"
    viewer = bx.user()
    shown = viewer.get(f"/api/posts/{post.id}").json()
    assert shown["media"]["url"].startswith(f"/media/{post.media_id}/img?")


def test_an_old_draft_expires(bx):
    deliver(bx, tg.text("/post قديم"))
    d = _draft(bx)
    clock.advance(int(timedelta(hours=25).total_seconds()))
    deliver(bx, tg.callback(f"op:yes:{d.id[:24]}"))
    assert _draft(bx).status == "pending"
    with bx.db() as db:
        assert db.scalar(select(Post).where(Post.author_id == _official(bx).id)) is None


# ----------------------------------------------------------------- the panel files


def test_every_panel_module_is_served():
    """A panel module missing from the allowlist breaks the whole panel (one failed import)."""
    files = {p.name for p in ADMIN_STATIC_DIR.iterdir() if p.suffix in (".js", ".css")}
    assert files <= set(_ADMIN_ASSETS), files - set(_ADMIN_ASSETS)
