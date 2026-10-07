"""Engagement control: comment library v2, team comments from system accounts (immediate or spread out),
privacy of Ideas comments, exclusions. (V6 removed the V5 "boost": counts are real only.)"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import clock
from app.models import Post, User
from app.services import engagement
from app.services.messaging import Effects
from tests.conftest import send


@pytest.fixture
def hx(make_harness):
    # Gradual jobs move the clock by hours: keep the admin session alive meanwhile.
    return make_harness(ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)


def idea(c, text="فكرة للتجربة"):
    return c.post("/api/posts", json={"content": text}).json()["id"]


def tick(hx) -> None:
    with hx.db() as db:
        engagement.tick(db, hx.settings, Effects())


def shown(c, pid):
    p = c.get(f"/api/posts/{pid}").json()
    return p["likes"], p["dislikes"]


# ----------------------------------------------------------------- no more boosts


def test_boost_is_gone_and_counts_are_real(hx):
    owner, fan = hx.user(), hx.user()
    pid = idea(owner)
    fan.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    with hx.db() as db:  # a V5 boost left in the database is never shown
        db.get(Post, pid).boost_likes = 500
    admin = hx.admin()
    assert admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "likes": 9}).status_code in (404, 405)
    assert shown(owner, pid) == (1, 0)
    detail = admin.get(f"/api/admin/access/ideas/{pid}").json()["idea"]
    assert detail["real"] == {"likes": 1, "dislikes": 0} and "boost" not in detail


# ----------------------------------------------------------------- library v2


def test_categories_import_search_usage(hx):
    admin = hx.admin()
    cats = admin.get("/api/admin/library/categories").json()["categories"]
    assert {"ترحيب", "تشجيع", "دعم", "تفاعل", "فكاهة"} <= {c["name"] for c in cats}
    new = admin.post("/api/admin/library/categories", json={"name": "تحفيز"}).json()
    assert admin.post("/api/admin/library/categories", json={"name": "تحفيز"}).status_code == 409
    res = admin.post("/api/admin/library/import", json={"category": new["id"],
                                                        "text": "واصل!\n\nأنت رائع\nواصل!\n<script>x</script>\nفكرة جميلة"}).json()
    assert res == {"added": 3, "skipped": 2}
    assert admin.post("/api/admin/library/import", json={"category": new["id"], "text": "واصل!"}).json() == {"added": 0, "skipped": 1}
    items = admin.get("/api/admin/library", params={"category": new["id"]}).json()["items"]
    assert len(items) == 3 and all(i["usage_count"] == 0 for i in items)
    assert len(admin.get("/api/admin/library", params={"q": "رائع"}).json()["items"]) == 1
    assert admin.delete(f"/api/admin/library/categories/{new['id']}").status_code == 409  # not empty
    renamed = admin.put(f"/api/admin/library/categories/{new['id']}", json={"name": "تحفيز وتشجيع"}).json()
    assert renamed["name"] == "تحفيز وتشجيع"


# ----------------------------------------------------------------- team comments


def _library(admin, texts, category="encourage"):
    admin.post("/api/admin/library/import", json={"category": category, "text": "\n".join(texts)})
    return admin.get("/api/admin/library", params={"category": category}).json()["items"]


def test_manual_library_comments_no_duplicates(hx):
    owner = hx.user()
    pid = idea(owner)
    admin = hx.admin()
    items = _library(admin, ["رائع جدًا", "استمروا"])
    body = {"target_type": "idea", "ids": [pid], "source": {"kind": "library", "library_ids": [i["id"] for i in items]}}
    r = admin.post("/api/admin/engagement/comments", json=body).json()
    assert r["targets"][0]["posted"] == 2
    again = admin.post("/api/admin/engagement/comments", json=body).json()
    assert again["targets"][0]["planned"] == 0  # never the same comment twice on the same post
    comments = owner.get(f"/api/posts/{pid}/comments").json()["comments"]
    assert {c["content"] for c in comments} == {"رائع جدًا", "استمروا"}
    assert all(c["author"] == "dzplay" for c in comments)  # default appearance
    usage = {i["text"]: i["usage_count"] for i in admin.get("/api/admin/library").json()["items"]}
    assert usage["رائع جدًا"] == 1
    # the panel knows which ones came from the team
    detail = admin.get(f"/api/admin/access/ideas/{pid}").json()["comments"]
    assert all(c["author"]["team"] == "system" for c in detail)
    assert admin.post("/api/admin/engagement/comments", json={**body, "target_type": "reel"}).status_code == 400


def test_random_from_category_official_badge_and_ideas_privacy(hx):
    owner, other = hx.user(), hx.user()
    pids = [idea(owner, "فكرة أ"), idea(owner, "فكرة ب")]
    admin = hx.admin()
    _library(admin, ["تعليق 1", "تعليق 2", "تعليق 3", "تعليق 4"], "support")
    r = admin.post("/api/admin/engagement/comments", json={
        "target_type": "idea", "ids": pids, "source": {"kind": "random", "category": "support", "count": 3},
        "appearance": "official"}).json()
    assert [t["posted"] for t in r["targets"]] == [3, 3]
    mine = owner.get(f"/api/posts/{pids[0]}/comments").json()["comments"]
    assert len(mine) == 3 and len({c["content"] for c in mine}) == 3
    assert all(c["author"] == "DZPLAY الرسمي" and c["official"] for c in mine)
    assert other.get(f"/api/posts/{pids[0]}/comments").status_code == 403  # still owner-only
    assert owner.get("/api/me").json()["unseen_comments"] == 3  # post A's were just read; post B's 3 are new
    # random again can only use what is left (one unused text per post)
    more = admin.post("/api/admin/engagement/comments", json={
        "target_type": "idea", "ids": pids, "source": {"kind": "random", "category": "support", "count": 3}}).json()
    assert [t["posted"] for t in more["targets"]] == [1, 1]


def test_spread_comments_and_new_text(hx):
    owner = hx.user()
    pid = idea(owner)
    admin = hx.admin()
    _library(admin, [f"تعليق موزع {i}" for i in range(6)], "engage")
    r = admin.post("/api/admin/engagement/comments", json={
        "target_type": "idea", "ids": [pid], "source": {"kind": "random", "category": "engage", "count": 6},
        "duration_minutes": 60}).json()
    assert r["gradual"] and r["targets"][0]["planned"] == 6

    def count() -> int:
        return len(admin.get(f"/api/admin/access/ideas/{pid}").json()["comments"])

    assert count() == 0
    clock.advance(30 * 60)
    tick(hx)
    assert count() == 3
    clock.advance(31 * 60)
    tick(hx)
    assert count() == 6
    assert admin.post("/api/admin/engagement/comments", json={
        "target_type": "idea", "ids": [pid], "source": {"kind": "text", "text": "تعليق جديد مكتوب"}}).json()["targets"][0]["posted"] == 1
    assert admin.post("/api/admin/engagement/comments", json={
        "target_type": "idea", "ids": [pid], "source": {"kind": "text", "text": "<b>x</b>"}}).status_code == 400


# ----------------------------------------------------------------- system accounts are not users


def test_system_accounts_excluded_everywhere(make_harness):
    hx = make_harness(ADMIN_SESSION_IDLE=7 * 24 * 3600, ADMIN_SESSION_TTL=7 * 24 * 3600)
    admin = hx.admin()
    with hx.db() as db:
        engagement._system_pool(db, hx.settings)  # created on first use (a team comment)
    with hx.db() as db:
        system = db.scalars(select(User).where(User.is_system.is_(True))).all()
        assert len(system) == hx.settings.SYSTEM_ACCOUNTS and all(u.password_hash is None for u in system)
        sys_email = system[0].email
    a = hx.user()
    assert send(a, "مرحبا").status_code == 409  # nobody real available: never matched with a system account
    stats = admin.get("/api/admin/stats").json()
    assert stats["users"]["total"] == 1
    assert admin.get("/api/admin/access/users").json()["total"] == 1
    assert admin.get("/api/admin/access/users", params={"include_team": True}).json()["total"] > 1
    assert hx.login(hx.client(), sys_email, "whatever-password").status_code == 401
    c = hx.client()
    assert c.post("/api/auth/reset/request", json={"email": sys_email}).status_code in (200, 400, 503)
