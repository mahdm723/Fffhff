"""V4 identity: display names, public ID, gender, 18+ confirmation, privacy switches."""

from __future__ import annotations

import re

import pytest
from sqlalchemy import select, text

from app import clock
from app.models import NameHistory, User

NAME = "/api/me/profile"


def me(c) -> dict:
    return c.get("/api/me").json()


# ----------------------------------------------------------------- names


def test_default_name_change_and_back_to_dzplay(hx):
    c = hx.user()
    assert me(c)["display_name"] == "dzplay" and me(c)["has_custom_name"] is False
    r = c.patch(NAME, json={"display_name": "  أحمد   علي  "})
    assert r.status_code == 200 and r.json()["display_name"] == "أحمد علي"
    # back to the default at any time, even inside the cooldown
    assert c.patch(NAME, json={"display_name": ""}).json()["display_name"] == "dzplay"
    with hx.db() as db:
        hist = [(h.old_name, h.new_name) for h in db.execute(select(NameHistory).order_by(NameHistory.id)).scalars()]
    assert hist == [(None, "أحمد علي"), ("أحمد علي", None)]


def test_name_change_cooldown(hx):
    c = hx.user()
    assert c.patch(NAME, json={"display_name": "Yacine"}).status_code == 200
    r = c.patch(NAME, json={"display_name": "Yacine2"})
    assert r.status_code == 429 and r.json()["error"]["code"] == "name_cooldown"
    assert me(c)["next_name_change_at"] is not None
    clock.advance(hx.settings.NAME_CHANGE_COOLDOWN_DAYS * 86400 + 1)
    assert c.patch(NAME, json={"display_name": "Yacine2"}).status_code == 200


@pytest.mark.parametrize("bad", [
    "DZPLAY الرسمي", "DzP1ay", "dzplay_dz", "d z p l a y", "Adm1n", "ADMIN", "support", "Moderator", "الإدارة", "مشرف",
    "‮nimda", "Ahm​ed", "Аdmin",  # RTL override, zero-width, Cyrillic А
    "ab", "x" * 30, "Ahmed😀", "<b>x</b>", "12345", "0552123456 ali",
])
def test_bad_names_are_refused(hx, bad):
    c = hx.user()
    r = c.patch(NAME, json={"display_name": bad})
    assert r.status_code in (400, 422), (bad, r.text)
    assert me(c)["display_name"] == "dzplay"


def test_names_are_plain_text_everywhere(hx):
    a, b = hx.user(), hx.user()
    a.patch(NAME, json={"display_name": "Sara_22"})
    pid = a.post("/api/posts", json={"content": "فكرة من سارة للتجربة"}).json()["id"]
    post = b.get(f"/api/posts/{pid}").json()
    assert post["author"]["name"] == "Sara_22"
    prof = b.get(f"/api/profiles/{post['author']['ref']}").json()
    assert prof["name"] == "Sara_22" and prof["public_id"].startswith("DZ-") and "email" not in str(prof)


# ----------------------------------------------------------------- public id


def test_public_id_is_random_stable_unique(hx):
    ids = set()
    for _ in range(5):
        c = hx.user()
        pid = me(c)["public_id"]
        assert re.fullmatch(r"DZ-[2-9A-HJ-NP-Z]{6}", pid)  # no 0/O 1/I
        assert me(c)["public_id"] == pid
        ids.add(pid)
    assert len(ids) == 5
    with hx.db() as db:
        internal = set(db.execute(select(User.id)).scalars())
    assert not any(p[3:] in i for p in ids for i in internal)


def test_existing_accounts_get_a_public_id_and_defaults(tmp_path):
    from app.db import Base, Database

    db = Database(f"sqlite:///{tmp_path}/old.db")
    Base.metadata.create_all(db.engine)
    with db.engine.begin() as conn:  # a V3 database: none of the V4 columns
        conn.execute(text("DROP INDEX IF EXISTS ix_users_name_norm"))
        conn.execute(text("DROP INDEX IF EXISTS ux_users_public_id"))
        for col in ("display_name", "name_norm", "name_changed_at", "public_id", "gender", "gender_asked_at",
                    "age_confirmed_at", "accept_anonymous", "accept_direct", "accept_calls", "searchable_by_name"):
            conn.execute(text(f"ALTER TABLE users DROP COLUMN {col}"))
        for i in range(3):
            conn.execute(text("INSERT INTO users (id, email, status, created_at, last_active_at, messages_sent, messages_received, "
                              f"conversations_count) VALUES ('u{i}', 'u{i}@example.com', 'active', '2026-01-01', '2026-01-01', 0, 0, 0)"))
    db.create_all()
    db.create_all()  # idempotent
    with db.session() as s:
        users = list(s.execute(select(User)).scalars())
        assert len({u.public_id for u in users}) == 3 and all(u.public_id.startswith("DZ-") for u in users)
        assert all(u.display_name is None and u.gender is None and u.age_confirmed_at is None for u in users)
    from sqlalchemy import inspect

    assert "ix_users_name_norm" in {i["name"] for i in inspect(db.engine).get_indexes("users")}


# ----------------------------------------------------------------- gender + 18+


def test_registration_requires_gender_and_age(hx):
    c = hx.client()
    assert hx.register(c, "g1@example.com", gender=None).status_code == 400
    c = hx.client()
    assert hx.register(c, "g2@example.com", age_confirmed=False).json()["error"]["code"] == "age_required"
    c = hx.client()
    r = hx.register(c, "g3@example.com", gender="female")
    assert r.status_code == 201
    m = me(c)
    assert m["gender"] == "female" and m["age_confirmed"] is True and m["needs_gender"] is False


def test_gender_icon_hidden_when_not_given_and_editable(hx):
    a, b = hx.user(), hx.user()
    a.patch(NAME, json={"display_name": "Karim"})
    pid = a.post("/api/posts", json={"content": "فكرة كريم للتجربة"}).json()["id"]
    assert b.get(f"/api/posts/{pid}").json()["author"]["gender"] is None  # "unspecified" is never shown
    assert a.patch(NAME, json={"gender": "male"}).status_code == 200
    assert b.get(f"/api/posts/{pid}").json()["author"]["gender"] == "male"
    assert a.patch(NAME, json={"gender": "robot"}).status_code == 400


def test_existing_users_get_one_gentle_prompt_and_confirm_age_once(hx):
    c = hx.user()
    with hx.db() as db:
        db.execute(text("UPDATE users SET gender = NULL, gender_asked_at = NULL, age_confirmed_at = NULL"))
    assert me(c)["needs_gender"] is True and me(c)["age_confirmed"] is False
    c.post("/api/me/gender-later")
    assert me(c)["needs_gender"] is False and me(c)["gender"] == "unspecified"
    assert c.post("/api/me/confirm-age", json={"confirm": True}).json() == {"age_confirmed": True}
    assert me(c)["age_confirmed"] is True


# ----------------------------------------------------------------- privacy switches


def test_privacy_switches_server_side(hx):
    c = hx.user()
    assert me(c)["privacy"] == {"accept_anonymous": True, "accept_direct": "everyone", "searchable_by_name": True}
    r = c.patch("/api/me/privacy", json={"accept_anonymous": False, "accept_direct": "nobody", "accept_calls": False,
                                         "searchable_by_name": False})  # an old app may still send accept_calls: ignored
    assert r.json() == {"accept_anonymous": False, "accept_direct": "nobody", "searchable_by_name": False}
    assert c.patch("/api/me/privacy", json={"accept_direct": "friends"}).status_code == 400


def test_admin_sees_name_history_id_and_search_by_name_or_id(make_harness):
    hx = make_harness()
    c = hx.user()
    c.patch(NAME, json={"display_name": "Yacine"})
    with hx.db() as db:
        uid = db.scalar(select(User.id).where(User.display_name == "Yacine"))
        pid = db.get(User, uid).public_id
    from app.services import admin_access

    with hx.db() as db:
        d = admin_access.user_detail(db, uid)
        assert d["user"]["display_name"] == "Yacine" and d["user"]["public_id"] == pid
        assert d["name_history"][0]["old"] is None and d["name_history"][0]["new"] == "Yacine"
        assert "accept_calls" not in d["user"]["privacy"]
        assert [u["id"] for u in admin_access.users_search(db, q=pid.lower())["users"]] == [uid]
        assert [u["id"] for u in admin_access.users_search(db, q="yaci")["users"]] == [uid]
        admin_access.delete_account(db, uid)
    with hx.db() as db:
        assert db.scalar(select(NameHistory).where(NameHistory.user_id == uid)) is None
