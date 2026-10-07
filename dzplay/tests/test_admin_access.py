"""Full admin access (privacy policy v3): users, private content, search, deletions, system status —
and an authorization matrix proving no admin route answers anonymous or user sessions."""

from __future__ import annotations

import re

from sqlalchemy import select

from app import clock
from app.models import Post, PostReaction, User
from tests.conftest import ADMIN_PATH, reply, send


def idea(c, text="فكرة للتجربة"):
    return c.post("/api/posts", json={"content": text}).json()["id"]


# ----------------------------------------------------------------- authorization matrix


def _all_routes(app):
    for r in app.routes:
        if hasattr(r, "original_router"):  # FastAPI keeps included routers as a unit
            for sub in r.original_router.routes:
                yield (r.include_context.prefix or "") + getattr(sub, "path", ""), sub
        else:
            yield getattr(r, "path", "") or "", r


def _admin_routes(hx):
    for path, route in _all_routes(hx.app):
        if not path.startswith(ADMIN_PATH + "/api/admin") or path.endswith(("/login", "/logout")):
            continue
        concrete = re.sub(r"\{[^}]+\}", "x" * 32, path)
        for method in sorted(getattr(route, "methods", None) or ()):
            if method != "HEAD":
                yield method, concrete


def test_every_admin_route_rejects_anonymous_and_user_sessions(hx):
    user = hx.user()
    routes = list(_admin_routes(hx))
    assert len(routes) > 35
    for i, (method, path) in enumerate(routes):
        if i % 10 == 9:
            clock.advance(61)  # stay under the per-network limit for unauthenticated panel calls (tested below)
        for client in (hx.client(), user):
            r = client.request(method, path, json={})
            assert r.status_code in (401, 403), f"{method} {path} -> {r.status_code}"
    # and the old guessable paths do not exist at all
    for path in ("/api/admin/stats", "/admin", "/api/admin/access/users"):
        assert user.get(path).status_code == 404


def test_admin_api_is_rate_limited(make_harness):
    hx = make_harness(ADMIN_API_PER_MINUTE=20, ADMIN_API_ANON_PER_MINUTE=5)
    anon = hx.client()
    codes = [anon.get(f"{ADMIN_PATH}/api/admin/stats").status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5:] == [429, 429]
    admin = hx.admin()
    codes = [admin.get("/api/admin/session").status_code for _ in range(25)]
    assert codes.count(200) == 20 and codes[-1] == 429
    r = admin.get("/api/admin/session")
    assert r.status_code == 429 and int(r.headers["Retry-After"]) >= 1
    clock.advance(61)
    assert admin.get("/api/admin/session").status_code == 200


# ----------------------------------------------------------------- users


def test_users_search_detail_and_actions(hx):
    a = hx.user("alice@example.com")
    b = hx.user("bob@example.com")
    pid = idea(a, "فكرة أليس")
    b.post(f"/api/posts/{pid}/comments", json={"content": "تعليق خاص من بوب"})
    b.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    cid = send(a, "مرحبا بوب").json()["conversation"]["id"]
    reply(b, cid, "أهلًا أليس")
    admin = hx.admin()

    found = admin.get("/api/admin/access/users", params={"q": "alice@"}).json()
    assert found["total"] == 1 and found["users"][0]["email"] == "alice@example.com"
    alice_id = found["users"][0]["id"]
    assert admin.get("/api/admin/access/users", params={"q": alice_id}).json()["total"] == 1
    assert admin.get("/api/admin/access/users", params={"q": "%"}).json()["total"] == 0  # no wildcard injection
    assert admin.get("/api/admin/access/users", params={"has_posts": True}).json()["total"] == 1
    assert admin.get("/api/admin/access/users", params={"method": "email"}).json()["total"] == 2
    assert "password" not in str(found) and "argon2" not in str(found)

    detail = admin.get(f"/api/admin/access/users/{alice_id}").json()
    assert detail["user"]["email"] == "alice@example.com" and detail["user"]["sessions_active"] >= 1
    assert detail["posts"][0]["content"] == "فكرة أليس" and detail["posts"][0]["real"]["likes"] == 1
    assert detail["conversations"][0]["peer"]["email"] == "bob@example.com"
    assert any(e["type"] == "register" for e in detail["security_events"])
    assert "argon2" not in str(detail) and "password_hash" not in str(detail)
    bob_id = admin.get("/api/admin/access/users", params={"q": "bob@"}).json()["users"][0]["id"]
    bob = admin.get(f"/api/admin/access/users/{bob_id}").json()
    assert bob["idea_comments"][0]["content"] == "تعليق خاص من بوب"
    assert bob["reactions"][0] == {**bob["reactions"][0], "target": "idea", "reaction": "like"}

    # revoke sessions
    assert admin.post(f"/api/admin/access/users/{bob_id}/revoke-sessions").json()["revoked"] >= 1
    assert b.get("/api/me").status_code == 401
    # delete account: counters on other people's content are recomputed
    assert admin.delete(f"/api/admin/access/users/{bob_id}").status_code == 200
    with hx.db() as db:
        assert db.get(User, bob_id) is None
        p = db.get(Post, pid)
        assert p.likes_count == 0 and p.comments_count == 0
        assert db.scalar(select(PostReaction.id)) is None
    actions = {e["action"] for e in admin.get("/api/admin/audit").json()["entries"]}
    assert {"view_users", "view_user", "user_revoke_sessions", "user_delete"} <= actions


# ----------------------------------------------------------------- private content


def test_admin_sees_private_comments_users_still_cannot(hx):
    owner, writer, other = hx.user(), hx.user(), hx.user()
    pid = idea(owner)
    writer.post(f"/api/posts/{pid}/comments", json={"content": "سر بين الكاتب وصاحب الفكرة"})
    admin = hx.admin()
    data = admin.get(f"/api/admin/access/ideas/{pid}").json()
    assert data["comments"][0]["content"] == "سر بين الكاتب وصاحب الفكرة"
    assert data["comments"][0]["author"]["email"] == writer.email
    assert data["idea"]["author"]["email"] == owner.email
    for c in (writer, other):  # users' privacy rules unchanged
        assert c.get(f"/api/posts/{pid}/comments").status_code == 403
    listing = admin.get("/api/admin/access/ideas", params={"q": "تجربة"}).json()
    assert listing["total"] == 1 and listing["ideas"][0]["author"]["email"] == owner.email


def test_conversations_with_both_participants(hx):
    a, b = hx.user(), hx.user()
    cid = send(a, "رسالة مجهولة").json()["conversation"]["id"]
    reply(b, cid, "رد")
    admin = hx.admin()
    lst = admin.get("/api/admin/access/conversations").json()
    assert lst["total"] == 1
    conv = lst["conversations"][0]
    assert {conv["initiator"]["email"], conv["recipient"]["email"]} == {a.email, b.email}
    detail = admin.get(f"/api/admin/access/conversations/{cid}").json()
    assert [m["content"] for m in detail["messages"]] == ["رسالة مجهولة", "رد"]
    assert [m["flagged"] for m in detail["messages"]] == [False, False]
    assert admin.get("/api/admin/access/conversations", params={"q": "مجهولة"}).json()["total"] == 1
    assert admin.get("/api/admin/access/conversations", params={"q": "لا يوجد"}).json()["total"] == 0
    actions = [e["action"] for e in admin.get("/api/admin/audit").json()["entries"]]
    assert "view_conversation" in actions and "view_conversation_list" in actions


def test_search_and_delete_any_content(hx):
    a, b = hx.user(), hx.user()
    pid = idea(a, "كلمة_بحث في فكرة")
    b.post(f"/api/posts/{pid}/comments", json={"content": "كلمة_بحث في تعليق"})
    cid = send(a, "كلمة_بحث في رسالة").json()["conversation"]["id"]
    admin = hx.admin()
    res = admin.get("/api/admin/access/search", params={"q": "كلمة_بحث"}).json()
    assert len(res["ideas"]) == 1 and len(res["idea_comments"]) == 1 and len(res["messages"]) == 1
    assert "reel_comments" not in res and "reels" not in res
    assert admin.get("/api/admin/access/search", params={"q": "_"}).status_code == 400
    assert admin.get("/api/admin/access/search", params={"q": "__"}).json()["ideas"] == []  # escaped, not a wildcard

    assert admin.delete(f"/api/admin/access/content/idea_comment/{res['idea_comments'][0]['id']}").status_code == 200
    assert admin.delete(f"/api/admin/access/content/reel_comment/{'x' * 32}").status_code == 400
    assert admin.delete(f"/api/admin/access/content/message/{res['messages'][0]['id']}").status_code == 200
    assert admin.delete(f"/api/admin/access/content/conversation/{cid}").status_code == 200
    assert admin.delete(f"/api/admin/access/content/idea/{pid}").status_code == 200
    assert admin.delete(f"/api/admin/access/content/user/{pid}").status_code == 400
    assert admin.delete(f"/api/admin/access/content/idea/{pid}").status_code == 404
    again = admin.get("/api/admin/access/search", params={"q": "كلمة_بحث"}).json()
    assert all(not v for v in again.values())


def test_system_status(hx):
    data = hx.admin().get("/api/admin/system").json()
    assert data["bot"]["configured"] is False and "media_cache" in data and isinstance(data["failed_logins"], list)
