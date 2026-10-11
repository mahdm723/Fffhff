"""V6 phase 4: public comments with one level of replies, notifications, likers (dislikers never exposed),
old private comments stay private, picture posts for members only, team comments carry a visible badge."""

from __future__ import annotations

import json

from sqlalchemy import select

from app import clock
from app.models import Comment, Notification, User
from tests.test_admin_access import _all_routes


def _post(c, text="فكرة للنقاش العام حول السوق اليوم") -> str:
    r = c.post("/api/posts", json={"content": text})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _comment(c, pid, text, parent=None):
    body = {"content": text}
    if parent:
        body["parent_id"] = parent
    return c.post(f"/api/posts/{pid}/comments", json=body)


def _me(c):
    return c.get("/api/me").json()


def _uid(hx, c) -> str:
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == c.email))


def test_public_comments_and_one_level_of_replies(hx):
    owner, b, c, stranger = hx.user(), hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    r = _comment(b, pid, "تحليل ممتاز، أتفق معك")
    assert r.status_code == 201, r.text
    top = r.json()["comment"]
    assert top["parent_id"] is None and top["author"]["name"] == _me(b)["display_name"]
    reply = _comment(c, pid, "وأنا كذلك", parent=top["id"]).json()["comment"]
    assert reply["parent_id"] == top["id"] and reply["reply_to"] == _me(b)["display_name"]
    # a reply to a reply stays in the same thread, addressed to the person answered
    deeper = _comment(owner, pid, "شكرًا لكما", parent=reply["id"]).json()["comment"]
    assert deeper["parent_id"] == top["id"] and deeper["reply_to"] == _me(c)["display_name"]
    # everyone sees them, with the author's public card
    data = stranger.get(f"/api/posts/{pid}/comments").json()
    assert [x["id"] for x in data["comments"]] == [top["id"]]
    assert [x["id"] for x in data["comments"][0]["replies"]] == [reply["id"], deeper["id"]]
    card = data["comments"][0]["author"]
    assert set(card) >= {"name", "public_id", "avatar_url", "verified"} and "email" not in json.dumps(data)
    assert data["comments"][0]["can_delete"] is False
    # the count is public now
    feed = stranger.get(f"/api/posts/{pid}").json()
    assert feed["comments"]["count"] == 3 and feed["can_comment"] is True


def test_comment_notifications(hx):
    owner, b, c = hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    top = _comment(b, pid, "سؤال: ما هو الهدف؟").json()["comment"]
    _comment(c, pid, "الهدف 70 ألف", parent=top["id"])
    _comment(owner, pid, "تعليقي على منشوري")  # no notification to myself
    n_owner = owner.get("/api/notifications").json()
    assert [n["kind"] for n in n_owner["notifications"]] == ["comment", "comment"]
    assert n_owner["unread"] == 2 and _me(owner)["unread_notifications"] == 2
    first = n_owner["notifications"][-1]
    assert first["actor"]["name"] == _me(b)["display_name"] and first["post_id"] == pid and first["read"] is False
    n_b = b.get("/api/notifications").json()
    assert [n["kind"] for n in n_b["notifications"]] == ["reply"] and n_b["notifications"][0]["comment_id"]
    assert owner.post("/api/notifications/read", json={}).status_code == 200
    assert owner.get("/api/notifications").json()["unread"] == 0
    # nobody reads someone else's notifications
    assert all(n["kind"] == "reply" for n in b.get("/api/notifications").json()["notifications"])


def test_delete_by_author_or_post_owner_only(hx):
    owner, b, c = hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    mine = _comment(b, pid, "تعليق سأحذفه").json()["comment"]
    other = _comment(c, pid, "تعليق يحذفه صاحب المنشور").json()["comment"]
    assert b.delete(f"/api/comments/{other['id']}").status_code == 404  # not mine, not my post
    assert b.delete(f"/api/comments/{mine['id']}").status_code == 200
    assert owner.delete(f"/api/comments/{other['id']}").status_code == 200
    assert owner.get(f"/api/posts/{pid}/comments").json()["comments"] == []
    with hx.db() as db:
        rows = db.execute(select(Comment).where(Comment.post_id == pid)).scalars().all()
        assert all(r.deleted_at is not None for r in rows)  # soft delete (kept for reports)


def test_report_a_public_comment_and_blocked_people_hidden(hx):
    owner, b, c = hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    cm = _comment(b, pid, "تعليق مزعج").json()["comment"]
    assert c.post(f"/api/comments/{cm['id']}/report", json={"reason": "spam"}).status_code == 201
    # owner blocks the commenter: hidden for the owner, and b can no longer comment there
    assert owner.post(f"/api/comments/{cm['id']}/block").status_code == 200
    assert owner.get(f"/api/posts/{pid}/comments").json()["comments"] == []
    assert _comment(b, pid, "مرة أخرى").status_code == 403


def test_old_private_comments_stay_private(hx):
    owner, b, c = hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    with hx.db() as db:  # a comment from before V6 phase 4 (visibility NULL)
        db.add(Comment(post_id=pid, author_id=_uid(hx, b), content="تعليق خاص قديم", created_at=clock.utcnow()))
        db.commit()
    assert [x["content"] for x in c.get(f"/api/posts/{pid}/comments").json()["comments"]] == []
    own = owner.get(f"/api/posts/{pid}/comments").json()["comments"]
    assert [(x["content"], x["private"]) for x in own] == [("تعليق خاص قديم", True)]
    assert [x["content"] for x in b.get(f"/api/posts/{pid}/comments").json()["comments"]] == ["تعليق خاص قديم"]
    # replying to it is not possible (it never becomes public)
    assert _comment(c, pid, "رد", parent=own[0]["id"]).status_code == 404
    with hx.db() as db:
        assert db.scalar(select(Comment.visibility).where(Comment.content == "تعليق خاص قديم")) is None


def test_likers_list_and_dislikers_never_exposed(hx):
    owner, b, c, d = hx.user(), hx.user(), hx.user(), hx.user()
    pid = _post(owner)
    b.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    c.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    d.put(f"/api/posts/{pid}/reaction", json={"reaction": "dislike"})
    likers = owner.get(f"/api/posts/{pid}/likers").json()
    ids = {x["public_id"] for x in likers["likers"]}
    assert ids == {_me(b)["public_id"], _me(c)["public_id"]}
    assert _me(d)["public_id"] not in json.dumps(likers)
    assert set(likers["likers"][0]) == {"public_id", "name", "avatar_url", "verified", "gender"}
    assert d.get(f"/api/posts/{pid}").json()["dislikes"] == 1
    # no route anywhere lists who disliked
    paths = [p for p, _r in _all_routes(hx.app)]
    assert not [p for p in paths if "dislik" in p]


def test_picture_posts_are_for_members_only(hx):
    from app.services import media_items

    a = hx.user()
    assert a.get("/api/uploads/config").json()["idea"]["member"] is False
    with hx.db() as db:
        user = db.scalar(select(User).where(User.email == a.email))
        try:
            media_items.begin_upload(db, hx.settings, hx.state.limiter, user, purpose="idea", conversation_id=None, length=100)
            raise AssertionError("expected members_only")
        except Exception as exc:  # noqa: BLE001
            assert getattr(exc, "code", None) == "members_only"
        user.member_since = clock.utcnow()
        db.commit()
    assert a.get("/api/uploads/config").json()["idea"]["member"] is True


def test_team_comments_carry_a_badge(hx):
    from app.services import engagement

    owner = hx.user()
    pid = _post(owner)
    with hx.db() as db:
        team = engagement._system_pool(db, hx.settings)[0]
        db.add(Comment(post_id=pid, author_id=team.id, content="تعليق من الفريق", visibility="public",
                       created_at=clock.utcnow()))
        db.commit()
    cm = hx.user().get(f"/api/posts/{pid}/comments").json()["comments"][0]
    assert cm["team"] is True and cm["author"]["public_id"] is None


def test_notifications_table_is_private_and_bounded(hx):
    hx.user()
    assert hx.client().get("/api/notifications").status_code == 401
    with hx.db() as db:
        assert db.scalar(select(Notification)) is None
