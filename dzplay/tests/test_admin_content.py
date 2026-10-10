"""Admin panel content tools: official DZPLAY account, comment library, Ideas management,
users, network blocks, extended stats."""

from __future__ import annotations

from sqlalchemy import select

from app import clock
from app.models import User
from tests.conftest import send


def idea(c, text="فكرة للتجربة"):
    return c.post("/api/posts", json={"content": text}).json()["id"]


# ----------------------------------------------------------------- official account


def test_official_comment_account_and_audit(hx):
    owner = hx.user()
    pid = idea(owner)
    admin = hx.admin()
    r = admin.post("/api/admin/official-comment", json={"target": "idea", "target_id": pid, "text": "أهلًا بكم في DZPLAY 👋"})
    assert r.status_code == 201
    with hx.db() as db:
        official = db.scalars(select(User).where(User.is_official.is_(True))).all()
        assert len(official) == 1 and official[0].password_hash is None and official[0].google_sub is None
    audit = admin.get("/api/admin/audit", params={"action": "official_comment"}).json()["entries"]
    assert audit[0]["target_type"] == "idea" and audit[0]["actor"] == "owner"
    # V6: Reels are gone
    assert admin.post("/api/admin/official-comment", json={"target": "reel", "target_id": pid, "text": "x"}).status_code == 400


def test_official_comment_on_idea_is_public_with_the_official_badge(hx):
    owner, other = hx.user(), hx.user()
    pid = idea(owner)
    admin = hx.admin()
    assert admin.post("/api/admin/official-comment", json={"target": "idea", "target_id": pid, "text": "فكرة رائعة!"}).status_code == 201
    assert owner.get("/api/me").json()["unseen_comments"] == 1
    mine = owner.get(f"/api/posts/{pid}/comments").json()["comments"]
    assert mine[0]["author"]["name"] == f"{hx.settings.APP_NAME} الرسمي" and mine[0]["official"] and mine[0]["team"]
    assert mine[0]["content"] == "فكرة رائعة!" and mine[0]["author"]["public_id"] is None
    seen = other.get(f"/api/posts/{pid}/comments").json()["comments"]  # V6: public, with the badge
    assert seen[0]["team"] is True and seen[0]["content"] == "فكرة رائعة!"
    assert [n["kind"] for n in owner.get("/api/notifications").json()["notifications"]] == ["comment"]
    assert admin.post("/api/admin/official-comment", json={"target": "idea", "target_id": "nope", "text": "x"}).status_code == 404
    assert admin.post("/api/admin/official-comment", json={"target": "user", "target_id": pid, "text": "x"}).status_code == 400


def test_official_account_is_not_a_user(hx):
    a = hx.user()
    hx.admin().post("/api/admin/official-comment", json={"target": "idea", "target_id": idea(a), "text": "مرحبًا"})
    # never a message recipient: its public ID is unknown to the messaging
    b = hx.user()
    with hx.db() as db:
        official_pid = db.scalar(select(User.public_id).where(User.is_official.is_(True)))
    assert official_pid and send(a, official_pid, "مرحبا").status_code == 404
    assert send(a, b, "مرحبا").status_code == 201
    # cannot sign in, not counted, not listed
    assert hx.login(hx.client(), "official@dzplay.invalid", "anything-at-all").status_code == 401
    assert hx.admin().get("/api/admin/stats").json()["users"]["total"] == 2
    assert all(u["ref"] for u in hx.admin().get("/api/admin/users", params={"filter": "reported"}).json()["users"])


def test_cannot_edit_reaction_counters(hx):
    """V6: the V5 boost is gone — counts are the real reactions only, and no admin route edits them."""
    owner, u = hx.user(), hx.user()
    pid = idea(owner)
    u.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    admin = hx.admin()
    for method, path, body in (("put", f"/api/admin/ideas/{pid}/likes", {"likes": 999}),
                               ("post", "/api/admin/engagement/boost", {"target_type": "idea", "ids": [pid], "likes": 999})):
        assert getattr(admin, method)(path, json=body).status_code in (404, 405)
    shown = next(i for i in admin.get("/api/admin/ideas").json()["ideas"] if i["id"] == pid)
    assert shown["likes"] == 1 and "boost" not in shown


# ----------------------------------------------------------------- comment library


def test_library_crud_and_use(hx):
    admin = hx.admin()
    assert admin.post("/api/admin/library", json={"category": "spam", "text": "x"}).status_code == 400
    item = admin.post("/api/admin/library", json={"category": "welcome", "text": "مرحبًا بك في DZPLAY!"}).json()
    admin.post("/api/admin/library", json={"category": "encourage", "text": "استمر، أفكارك ملهمة"})
    lib = admin.get("/api/admin/library").json()
    assert {i["category"] for i in lib["items"]} == {"welcome", "encourage"}
    assert {c["id"]: c["name"] for c in lib["categories"]}["welcome"] == "ترحيب"
    edited = admin.put(f"/api/admin/library/{item['id']}", json={"category": "welcome", "text": "أهلًا وسهلًا!"}).json()
    assert edited["text"] == "أهلًا وسهلًا!"
    owner = hx.user()
    pid = idea(owner)
    assert admin.post("/api/admin/official-comment", json={"target": "idea", "target_id": pid, "library_id": item["id"]}).status_code == 201
    assert owner.get(f"/api/posts/{pid}/comments").json()["comments"][0]["content"] == "أهلًا وسهلًا!"
    assert admin.get("/api/admin/library", params={"q": "أهلًا"}).json()["items"][0]["usage_count"] == 1
    assert admin.delete(f"/api/admin/library/{item['id']}").status_code == 200
    assert len(admin.get("/api/admin/library").json()["items"]) == 1
    actions = {e["action"] for e in admin.get("/api/admin/audit").json()["entries"]}
    assert {"library_add", "library_edit", "library_delete"} <= actions


# ----------------------------------------------------------------- previews


def test_admin_previews_need_an_admin_session(hx):
    u = hx.user()
    assert hx.client().get(f"/test-panel/api/admin/media/{'a' * 32}/img").status_code == 401
    assert u.get(f"/test-panel/api/admin/media/{'a' * 32}/img").status_code == 401
    assert hx.admin().get("/api/admin/reels").status_code == 404  # V6: Reels are gone


def test_admin_ideas_list_is_public_content_only(hx):
    owner = hx.user()
    pid = idea(owner, "فكرة عامة")
    owner2 = hx.user()
    idea(owner2, "فكرة أخرى")
    data = hx.admin().get("/api/admin/ideas", params={"limit": 1}).json()
    assert len(data["ideas"]) == 1 and data["next_before"]
    more = hx.admin().get("/api/admin/ideas", params={"limit": 5, "before": data["next_before"]}).json()
    assert {i["id"] for i in data["ideas"] + more["ideas"]} >= {pid}
    assert "@example.com" not in str(data) and "author" not in str(data)


# ----------------------------------------------------------------- users & network blocks


def test_users_lists_and_unban(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, b, "يا كلب").json()["conversation"]["id"]
    b.post(f"/api/conversations/{cid}/report", json={"reason": "harassment"})
    admin = hx.admin()
    reported = admin.get("/api/admin/users", params={"filter": "reported"}).json()["users"]
    assert len(reported) == 1 and reported[0]["reports"] == 1 and reported[0]["flags"] == 1
    ref = reported[0]["ref"]
    assert "@" not in str(reported) and "password" not in str(reported)
    admin.post(f"/api/admin/users/{ref}/status", json={"status": "banned"})
    assert [u["ref"] for u in admin.get("/api/admin/users", params={"filter": "banned"}).json()["users"]] == [ref]
    assert a.get("/api/me").status_code == 401
    admin.post(f"/api/admin/users/{ref}/status", json={"status": "active"})  # lift the ban
    assert admin.get("/api/admin/users", params={"filter": "banned"}).json()["users"] == []
    assert hx.login(hx.client(), a.email).status_code == 200
    assert admin.get("/api/admin/users", params={"filter": "everyone"}).status_code == 400


def test_lift_login_ip_block(make_harness):
    hx = make_harness(LOGIN_BLOCK_SCOPE="ip")
    hx.user("me@example.com")
    c = hx.client(ip="203.0.113.9")
    for _ in range(hx.settings.LOGIN_MAX_ATTEMPTS):
        hx.login(c, "me@example.com", "wrong-password-1")
        clock.advance(hx.settings.LOGIN_BACKOFF_MAX + 1)  # past the progressive delay, so each failure counts
    assert hx.login(c, "me@example.com").status_code == 429
    admin = hx.admin()
    blocks = admin.get("/api/admin/ip-blocks").json()["blocks"]
    assert blocks and blocks[0]["kind"] == "login" and "203.0.113.9" not in str(blocks)
    assert admin.get("/api/admin/stats").json()["ip_blocks_active"] >= 1
    for blk in blocks:
        assert admin.delete(f"/api/admin/ip-blocks/{blk['id']}").status_code == 200
    assert hx.login(c, "me@example.com").status_code == 200
    assert admin.delete("/api/admin/ip-blocks/login:nope").status_code == 404


def test_stats_include_v3_sections(hx):
    s = hx.admin().get("/api/admin/stats").json()
    assert "reels" not in s and "reel_comments" not in s
    assert set(s["password_resets"]) == {"requests_24h", "waiting_admin", "completed_24h"}
    assert "media_cache_bytes" in s and "ip_blocks_active" in s
