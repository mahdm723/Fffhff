"""Engagement control: boosts (real vs boost kept apart), gradual jobs, comment library v2,
team comments from system accounts, privacy of Ideas comments, exclusions."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app import clock
from app.models import PostReaction, User
from app.services import engagement
from app.services.messaging import Effects
from tests.conftest import send
from tests.test_reels import make_reels


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


# ----------------------------------------------------------------- boost


def test_immediate_boost_keeps_real_and_boost_apart(hx):
    owner, fan, viewer = hx.user(), hx.user(), hx.user()
    pid = idea(owner)
    fan.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    admin = hx.admin()
    r = admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "mode": "add", "likes": 200,
                                                         "dislikes": 3}).json()
    t = r["targets"][0]
    assert t["real"] == {"likes": 1, "dislikes": 0} and t["boost"] == {"likes": 200, "dislikes": 3}
    assert shown(viewer, pid) == (201, 3)
    # the real user's toggle still works exactly, on top of the boost
    assert fan.put(f"/api/posts/{pid}/reaction", json={"reaction": "dislike"}).json() == {"likes": 200, "dislikes": 4, "my_reaction": "dislike"}
    assert fan.put(f"/api/posts/{pid}/reaction", json={"reaction": None}).json()["likes"] == 200
    with hx.db() as db:
        assert db.scalar(select(PostReaction.id)) is None  # no fake reaction rows, ever
    detail = admin.get(f"/api/admin/access/ideas/{pid}").json()["idea"]
    assert detail["real"] == {"likes": 0, "dislikes": 0} and detail["boost"] == {"likes": 200, "dislikes": 3}
    # profile totals use the displayed numbers
    assert owner.get("/api/profile").json()["ideas"]["likes"] == 200


def test_set_mode_and_never_negative(hx):
    owner, a, b = hx.user(), hx.user(), hx.user()
    pid = idea(owner)
    a.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    b.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    admin = hx.admin()
    admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "mode": "set", "likes": 50})
    assert shown(owner, pid)[0] == 50
    admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "mode": "set", "likes": 0})
    assert shown(owner, pid)[0] == 0
    admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "mode": "add", "likes": -500})
    assert shown(owner, pid)[0] == 0  # displayed number never goes below zero
    assert a.put(f"/api/posts/{pid}/reaction", json={"reaction": None}).json()["likes"] == 0
    for bad in ({"mode": "set", "likes": -1}, {"mode": "jump", "likes": 1}, {"mode": "add"},
                {"mode": "add", "likes": 10 ** 9}, {"mode": "add", "likes": 1, "duration_minutes": 10 ** 7}):
        assert admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], **bad}).status_code == 400
    assert admin.post("/api/admin/engagement/boost", json={"target_type": "post", "ids": [pid], "likes": 1}).status_code == 400
    assert admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": ["nope"], "likes": 1}).status_code == 404


def test_gradual_bulk_boost_on_ideas_and_reels(hx):
    owner = hx.user()
    pids = [idea(owner, f"فكرة {i}") for i in range(3)]
    rid = make_reels(hx, 1)[0]
    admin = hx.admin()
    r = admin.post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": pids, "likes": 200,
                                                         "duration_minutes": 360}).json()
    assert r["gradual"] is True
    admin.post("/api/admin/engagement/boost", json={"target_type": "reel", "ids": [rid], "dislikes": 60, "duration_minutes": 60})
    viewer = hx.user()
    assert shown(viewer, pids[0])[0] == 0
    clock.advance(90 * 60)  # 1/4 of 6 hours
    tick(hx)
    assert all(shown(viewer, p)[0] == 50 for p in pids)
    reel = viewer.get("/api/reels/feed").json()["reels"][0]
    assert reel["dislikes"] == 60  # its 1-hour job is complete
    # the short id shown by the Telegram bot works as a target too
    short = admin.get(f"/api/admin/access/reels/{rid}").json()["reel"]["short_id"]
    by_short = admin.post("/api/admin/engagement/boost", json={"target_type": "reel", "ids": [short.upper()], "likes": 5})
    assert by_short.status_code == 200 and by_short.json()["targets"][0]["id"] == rid
    jobs = admin.get("/api/admin/engagement/jobs").json()["jobs"]
    assert {j["status"] for j in jobs if j["target_type"] == "idea"} == {"running"}
    # cancel the batch: the progress so far stays, nothing more is added
    assert admin.post(f"/api/admin/engagement/jobs/{r['batch_id']}/cancel").json()["cancelled"] == 3
    clock.advance(10 * 3600)
    tick(hx)
    assert shown(viewer, pids[1])[0] == 50


def test_gradual_set_reaches_target(hx):
    owner = hx.user()
    pid = idea(owner)
    hx.admin().post("/api/admin/engagement/boost", json={"target_type": "idea", "ids": [pid], "mode": "set", "likes": 120,
                                                        "duration_minutes": 120})
    clock.advance(60 * 60)
    tick(hx)
    assert shown(owner, pid)[0] == 60
    clock.advance(61 * 60)
    tick(hx)
    assert shown(owner, pid)[0] == 120


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


def test_manual_library_comments_on_reels_public_no_duplicates(hx):
    rid = make_reels(hx, 1)[0]
    admin = hx.admin()
    items = _library(admin, ["رائع جدًا", "استمروا"])
    body = {"target_type": "reel", "ids": [rid], "source": {"kind": "library", "library_ids": [i["id"] for i in items]}}
    r = admin.post("/api/admin/engagement/comments", json=body).json()
    assert r["targets"][0]["posted"] == 2
    again = admin.post("/api/admin/engagement/comments", json=body).json()
    assert again["targets"][0]["planned"] == 0  # never the same comment twice on the same post
    viewer = hx.user()
    comments = viewer.get(f"/api/reels/{rid}/comments").json()["comments"]
    assert {c["content"] for c in comments} == {"رائع جدًا", "استمروا"}
    assert all(c["author"] == {"name": "dzplay", "official": False, "gender": None, "verified": False} for c in comments)  # default appearance
    usage = {i["text"]: i["usage_count"] for i in admin.get("/api/admin/library").json()["items"]}
    assert usage["رائع جدًا"] == 1
    # the panel knows which ones came from the team
    detail = admin.get(f"/api/admin/access/reels/{rid}").json()["comments"]
    assert all(c["author"]["team"] == "system" for c in detail)


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
    rid = make_reels(hx, 1)[0]
    admin = hx.admin()
    _library(admin, [f"تعليق موزع {i}" for i in range(6)], "engage")
    r = admin.post("/api/admin/engagement/comments", json={
        "target_type": "reel", "ids": [rid], "source": {"kind": "random", "category": "engage", "count": 6},
        "duration_minutes": 60}).json()
    assert r["gradual"] and r["targets"][0]["planned"] == 6
    viewer = hx.user()
    assert viewer.get(f"/api/reels/{rid}/comments").json()["total"] == 0
    clock.advance(30 * 60)
    tick(hx)
    assert viewer.get(f"/api/reels/{rid}/comments").json()["total"] == 3
    clock.advance(31 * 60)
    tick(hx)
    assert viewer.get(f"/api/reels/{rid}/comments").json()["total"] == 6
    assert admin.post("/api/admin/engagement/comments", json={
        "target_type": "reel", "ids": [rid], "source": {"kind": "text", "text": "تعليق جديد مكتوب"}}).json()["targets"][0]["posted"] == 1
    assert admin.post("/api/admin/engagement/comments", json={
        "target_type": "reel", "ids": [rid], "source": {"kind": "text", "text": "<b>x</b>"}}).status_code == 400


# ----------------------------------------------------------------- system accounts are not users


def test_system_accounts_excluded_everywhere(hx):
    rid = make_reels(hx, 1)[0]
    admin = hx.admin()
    admin.post("/api/admin/engagement/comments", json={"target_type": "reel", "ids": [rid], "source": {"kind": "text", "text": "مرحبًا"}})
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
