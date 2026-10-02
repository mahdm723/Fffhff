"""Public ideas: posts, feed, reactions, owner-only comments, public profiles."""

from __future__ import annotations

import json

from sqlalchemy import select, text

from app import clock
from app.models import Post, PostReaction, User
from tests.conftest import reply, send


def post(c, content="أحيانًا أفضل حل هو أن تتوقف عن التفكير في رأي الآخرين.", **extra):
    return c.post("/api/posts", json={"content": content, **extra})


def react(c, pid, reaction):
    return c.put(f"/api/posts/{pid}/reaction", json={"reaction": reaction})


def feed_ids(c, **params):
    data = c.get("/api/posts/feed", params=params).json()
    return [p["id"] for p in data["posts"]], data["next_cursor"]


def _secrets(hx) -> set[str]:
    with hx.db() as db:
        users = db.scalars(select(User)).all()
        return {u.id for u in users} | {u.email for u in users}


# ----------------------------------------------------------------- posts & feed


def test_create_post_and_see_it_in_feed(hx):
    a, b = hx.user(), hx.user()
    r = post(a)
    assert r.status_code == 201
    p = r.json()
    assert p["author"]["name"] == "dzplay" and p["mine"] is True
    assert p["likes"] == 0 and p["dislikes"] == 0 and p["created_at"]
    ids, _ = feed_ids(b)
    assert ids == [p["id"]]
    seen_by_b = b.get(f"/api/posts/{p['id']}").json()
    assert seen_by_b["mine"] is False and seen_by_b["comments"] is None and seen_by_b["can_comment"] is True
    # No identity data anywhere in public payloads.
    payload = json.dumps([b.get("/api/posts/feed").json(), seen_by_b, b.get(f"/api/profiles/{p['author']['ref']}").json()])
    assert not any(s in payload for s in _secrets(hx))


def test_post_validation_and_limits(make_harness):
    hx = make_harness(MAX_POST_LENGTH=50, MAX_POSTS_PER_HOUR=2)
    a = hx.user()
    assert post(a, "").json()["error"]["code"] == "empty_message"
    assert post(a, "x" * 51).json()["error"]["code"] == "message_too_long"
    assert post(a, "<script>alert(1)</script>").json()["error"]["code"] == "html_not_allowed"
    assert post(a, "فكرة أولى").status_code == 201
    assert post(a, "فكرة أولى").status_code == 429  # duplicate
    assert post(a, "فكرة ثانية").status_code == 201
    assert post(a, "فكرة ثالثة").status_code == 429  # hourly limit
    assert hx.client().post("/api/posts", json={"content": "x"}).status_code == 401


def test_post_idempotent_client_id(hx):
    a = hx.user()
    r1 = post(a, "مرة واحدة", client_id="post-client-0001")
    r2 = post(a, "مرة واحدة", client_id="post-client-0001")
    assert r1.json()["id"] == r2.json()["id"]


def test_feed_is_randomized_paginated_and_complete(make_harness):
    hx = make_harness(MAX_POSTS_PER_HOUR=100, MAX_POSTS_PER_DAY=100, FEED_PAGE_SIZE=5)
    authors = [hx.user() for _ in range(3)]
    created = []
    for i in range(18):
        clock.advance(60)
        created.append(post(authors[i % 3], f"فكرة رقم {i}").json()["id"])
    reader = hx.user()

    # Pagination: pages of 5 with a cursor, no duplicates, nothing missing.
    seen, cursor, pages = [], None, 0
    while True:
        ids, cursor = feed_ids(reader, **({"cursor": cursor} if cursor else {}))
        assert len(ids) <= 5
        seen += ids
        pages += 1
        if not cursor:
            break
    assert pages == 4 and len(seen) == 18 and set(seen) == set(created)

    # Randomized: not plain reverse-chronological, and a new session gives a new order.
    orders = {tuple(feed_ids(reader, limit=18)[0]) for _ in range(6)}
    assert len(orders) > 1
    assert any(list(o) != list(reversed(created)) for o in orders)


def test_popular_posts_do_not_always_win(make_harness):
    hx = make_harness(MAX_POSTS_PER_HOUR=100, MAX_REACTIONS_PER_MINUTE=1000)
    author = hx.user()
    popular = post(author, "منشور مشهور").json()["id"]
    quiet = [post(author, f"منشور هادئ {i}").json()["id"] for i in range(5)]
    likers = [hx.user() for _ in range(12)]
    for u in likers:
        react(u, popular, "like")
    reader = hx.user()
    firsts = [feed_ids(reader, limit=6)[0][0] for _ in range(40)]
    assert firsts.count(popular) < 40  # engagement helps, but it is not a fixed top slot
    assert any(f in quiet for f in firsts)


def test_feed_excludes_blocked_authors(hx):
    a, b = hx.user(), hx.user()
    pid = post(a, "منشور من a").json()["id"]
    cid = send(b, "مرحبا").json()["conversation"]["id"]
    a.post(f"/api/conversations/{cid}/block")  # a blocks b
    assert pid not in feed_ids(b)[0]


def test_delete_own_post_only(hx):
    a, b = hx.user(), hx.user()
    pid = post(a).json()["id"]
    assert b.delete(f"/api/posts/{pid}").status_code == 404
    assert a.delete(f"/api/posts/{pid}").status_code == 200
    assert b.get(f"/api/posts/{pid}").status_code == 404


# ----------------------------------------------------------------- reactions


def test_like_dislike_switch_remove_and_no_duplicates(hx):
    a, b, c = hx.user(), hx.user(), hx.user()
    pid = post(a).json()["id"]
    assert react(b, pid, "like").json() == {"likes": 1, "dislikes": 0, "my_reaction": "like"}
    assert react(b, pid, "like").json() == {"likes": 1, "dislikes": 0, "my_reaction": "like"}  # no double count
    assert react(c, pid, "like").json()["likes"] == 2
    assert react(b, pid, "dislike").json() == {"likes": 1, "dislikes": 1, "my_reaction": "dislike"}  # switch
    assert react(b, pid, None).json() == {"likes": 1, "dislikes": 0, "my_reaction": None}  # remove
    assert react(b, pid, "love").status_code == 400
    assert react(a, pid, "like").status_code == 400  # not on your own post
    view = c.get(f"/api/posts/{pid}").json()
    assert view["likes"] == 1 and view["my_reaction"] == "like"
    with hx.db() as db:
        rows = db.scalars(select(PostReaction)).all()
        assert len(rows) == 1  # one row per user per post, never like+dislike together
        stored = db.get(Post, pid)
        assert (stored.likes_count, stored.dislikes_count) == (1, 0)


def test_unique_constraint_blocks_duplicate_reaction_rows(hx):
    a, b = hx.user(), hx.user()
    pid = post(a).json()["id"]
    react(b, pid, "like")
    with hx.db() as db:
        uid = db.scalar(select(PostReaction.user_id))
    import pytest
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):
        with hx.db() as db:
            db.add(PostReaction(post_id=pid, user_id=uid, reaction_type="dislike"))


# ----------------------------------------------------------------- comments


def test_comments_visible_only_to_post_owner(hx):
    owner, b, c, d = hx.user(), hx.user(), hx.user(), hx.user()
    pid = post(owner).json()["id"]
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "تعليق من B"}).status_code == 201
    assert c.post(f"/api/posts/{pid}/comments", json={"content": "تعليق من C"}).status_code == 201

    data = owner.get(f"/api/posts/{pid}/comments").json()
    assert sorted(x["content"] for x in data["comments"]) == ["تعليق من B", "تعليق من C"]
    assert all(x["author"] == "dzplay" and set(x) == {"id", "author", "content", "created_at"} for x in data["comments"])

    # Everyone else: no comment content, no count — in any API.
    ref = owner.get("/api/profile").json()["ref"]
    for other in (b, c, d):
        r = other.get(f"/api/posts/{pid}/comments")
        assert r.status_code == 403
        payload = json.dumps([other.get(f"/api/posts/{pid}").json(), other.get("/api/posts/feed").json(),
                              other.get(f"/api/profiles/{ref}/posts").json()])
        assert "تعليق من" not in payload
        assert other.get(f"/api/posts/{pid}").json()["comments"] is None

    # Owner sees the count and the unseen badge resets after opening.
    mine = owner.get(f"/api/posts/{pid}").json()
    assert mine["comments"] == {"count": 2, "unseen": 0}
    b.post(f"/api/posts/{pid}/comments", json={"content": "تعليق آخر"})
    assert owner.get("/api/profile").json()["unseen_comments"] == 1


def test_comment_rules(make_harness):
    hx = make_harness(MAX_COMMENT_LENGTH=20, MAX_COMMENTS_PER_MINUTE=2)
    owner, b = hx.user(), hx.user()
    pid = post(owner).json()["id"]
    assert owner.post(f"/api/posts/{pid}/comments", json={"content": "تعليقي"}).status_code == 400
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "x" * 21}).status_code == 400
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "واحد"}).status_code == 201
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "اثنان"}).status_code == 201
    assert b.post(f"/api/posts/{pid}/comments", json={"content": "ثلاثة"}).status_code == 429


def test_owner_can_report_delete_and_block_commenter(hx):
    owner, b = hx.user(), hx.user()
    pid = post(owner).json()["id"]
    b.post(f"/api/posts/{pid}/comments", json={"content": "تعليق مسيء"})
    cid = owner.get(f"/api/posts/{pid}/comments").json()["comments"][0]["id"]
    assert b.post(f"/api/comments/{cid}/report", json={"reason": "spam"}).status_code == 404  # not the owner
    assert owner.post(f"/api/comments/{cid}/report", json={"reason": "harassment"}).status_code == 201
    assert owner.post(f"/api/comments/{cid}/block").status_code == 200
    r = b.post(f"/api/posts/{pid}/comments", json={"content": "مرة أخرى"})
    assert r.status_code == 403
    assert owner.delete(f"/api/comments/{cid}").status_code == 200
    assert owner.get(f"/api/posts/{pid}/comments").json()["comments"] == []
    # Blocking also applies to anonymous messaging (same block list).
    assert send(b, "رسالة").json()["error"]["code"] == "no_recipient"


def test_report_post_and_admin_remove(make_harness):
    hx = make_harness()
    owner, b = hx.user(), hx.user()
    pid = post(owner, "منشور مخالف").json()["id"]
    assert owner.post(f"/api/posts/{pid}/report", json={"reason": "spam"}).status_code == 400
    assert b.post(f"/api/posts/{pid}/report", json={"reason": "inappropriate"}).status_code == 201
    admin = hx.admin()
    reports = admin.get("/api/admin/reports").json()["reports"]
    assert reports[0]["target"] == "post" and reports[0]["evidence"][0]["content"] == "منشور مخالف"
    admin.post(f"/api/admin/reports/{reports[0]['id']}/resolve", json={"action": "remove"})
    assert b.get(f"/api/posts/{pid}").status_code == 404
    assert pid not in feed_ids(b)[0]


# ----------------------------------------------------------------- profiles


def test_profiles_show_only_public_idea_stats(hx):
    owner, b, c = hx.user("owner@example.com"), hx.user(), hx.user()
    p1 = post(owner, "الفكرة الأولى").json()
    clock.advance(5)
    p2 = post(owner, "الفكرة الثانية").json()
    react(b, p1["id"], "like")
    react(c, p1["id"], "like")
    react(c, p2["id"], "dislike")

    ref = p1["author"]["ref"]
    assert p2["author"]["ref"] == ref
    prof = b.get(f"/api/profiles/{ref}").json()
    assert prof == {"ref": ref, "name": "dzplay", "is_me": False, "stats": {"posts": 2, "likes": 2, "dislikes": 1}}
    posts = b.get(f"/api/profiles/{ref}/posts").json()["posts"]
    assert [p["content"] for p in posts] == ["الفكرة الثانية", "الفكرة الأولى"]

    me = owner.get("/api/profile").json()
    assert me["ref"] == ref and me["ideas"] == {"posts": 2, "likes": 2, "dislikes": 1}
    assert owner.get(f"/api/profiles/{ref}").json()["is_me"] is True
    raw = json.dumps([prof, posts, me])
    assert "owner@example.com" not in raw and not any(s in raw for s in _secrets(hx))
    assert b.get("/api/profiles/not-a-real-ref").status_code == 404


def test_profile_ref_is_not_linked_to_messaging(hx):
    a, b = hx.user(), hx.user()
    ref = post(a).json()["author"]["ref"]
    cid = send(a, "رسالة مجهولة").json()["conversation"]["id"]
    reply(b, cid, "رد")
    for c in (a, b):
        payload = json.dumps([c.get("/api/conversations").json(), c.get(f"/api/conversations/{cid}").json(), c.get("/api/sync").json()])
        assert ref not in payload


def test_profile_posts_pagination(hx):
    a = hx.user()
    hx.settings.MAX_POSTS_PER_HOUR = 50
    hx.settings.MAX_POSTS_PER_DAY = 50
    for i in range(7):
        clock.advance(10)
        post(a, f"منشور {i}")
    ref = a.get("/api/profile").json()["ref"]
    page1 = a.get(f"/api/profiles/{ref}/posts", params={"limit": 4}).json()
    page2 = a.get(f"/api/profiles/{ref}/posts", params={"limit": 4, "before": page1["next_before"]}).json()
    assert [p["content"] for p in page1["posts"] + page2["posts"]] == [f"منشور {i}" for i in range(6, -1, -1)]
    assert page2["next_before"] is None


# ----------------------------------------------------------------- migration


def test_existing_database_gets_new_columns(tmp_path):
    """A database created by the previous release (no reports.post_id/comment_id) is upgraded in place."""
    from app import models  # noqa: F401
    from app.db import Base, Database
    from app.migrations import add_missing_columns

    db = Database(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(db.engine)
    with db.engine.begin() as conn:  # turn it into the previous release's schema
        conn.execute(text("ALTER TABLE reports DROP COLUMN post_id"))
        conn.execute(text("ALTER TABLE reports DROP COLUMN comment_id"))
        conn.execute(text("INSERT INTO users (id, email, status, created_at, last_active_at, messages_sent, messages_received, "
                          "conversations_count) VALUES ('u1', 'a@example.com', 'active', '2026-01-01', '2026-01-01', 0, 0, 0)"))
        conn.execute(text("INSERT INTO reports (id, reporter_id, reported_user_id, reason, snapshot, status, created_at, expires_at) "
                          "VALUES ('r1', 'u1', 'u1', 'spam', '[]', 'open', '2026-01-01', '2027-01-01')"))
    added = add_missing_columns(db.engine)
    assert "reports.post_id" in added and "reports.comment_id" in added
    with db.engine.begin() as conn:
        assert conn.execute(text("SELECT reason, post_id FROM reports")).one() == ("spam", None)
    assert add_missing_columns(db.engine) == []  # idempotent
