"""V6 phase 21: the capacity fixes found by the load tests — open sockets told at once when a session ends (and no longer
re-checked at every ping), the Argon2 slots, one runner per background task, the startup lock, the new indexes, the
capacity warning, the shared market snapshot, and the cleanup in short batches."""

from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

import pytest
from sqlalchemy import delete, inspect, select
from starlette.websockets import WebSocketDisconnect

from app import clock
from app.models import AuthSession, MediaItem, Notification, User
from app.security import passwords

ORIGIN = {"Origin": "http://testserver"}


def _uid(hx, c) -> str:
    with hx.db() as db:
        return db.scalar(select(User.id).where(User.email == c.email))


def _expect_close(ws, code=4401):
    with pytest.raises(WebSocketDisconnect) as e:
        for _ in range(5):
            ws.receive_json()
    assert e.value.code == code


# ----------------------------------------------------------------------------------------- WebSockets
def test_pings_no_longer_read_the_session(make_harness):
    hx = make_harness(WS_PING_SECONDS=1, WS_SESSION_RECHECK_SECONDS=3600)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "hello"}
        with hx.db() as db:  # the session disappears without anyone saying so (e.g. expired)
            db.execute(delete(AuthSession).where(AuthSession.user_id == _uid(hx, a)))
        assert ws.receive_json() == {"type": "ping"}
        assert ws.receive_json() == {"type": "ping"}  # still open: pings no longer query the database


def test_the_regular_recheck_still_closes_an_expired_session(make_harness):
    hx = make_harness(WS_PING_SECONDS=1, WS_SESSION_RECHECK_SECONDS=1)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "hello"}
        with hx.db() as db:
            db.execute(delete(AuthSession).where(AuthSession.user_id == _uid(hx, a)))
        _expect_close(ws)


def test_logout_closes_the_open_socket_at_once(make_harness):
    hx = make_harness(WS_PING_SECONDS=30, WS_SESSION_RECHECK_SECONDS=3600)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "hello"}
        t0 = time.monotonic()
        assert a.post("/api/auth/logout").status_code in (200, 204)
        _expect_close(ws)
        assert time.monotonic() - t0 < 10  # not the 30 s ping, not the hour of re-check


def test_a_ban_closes_every_socket_of_the_user(make_harness):
    hx = make_harness(WS_PING_SECONDS=30, WS_SESSION_RECHECK_SECONDS=3600)
    a, b = hx.user(), hx.user()
    admin = hx.admin()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws, b.websocket_connect("/api/ws", headers=ORIGIN) as other:
        assert ws.receive_json() == {"type": "hello"} and other.receive_json() == {"type": "hello"}
        assert admin.post(f"/api/admin/users/{_uid(hx, a)}/status", json={"status": "banned"}).status_code == 200
        _expect_close(ws)
        b.post("/api/auth/logout")  # b was not banned: its socket only closes now, on its own logout
        _expect_close(other)


def test_the_session_event_never_reaches_the_app(make_harness):
    hx = make_harness(WS_PING_SECONDS=1, WS_SESSION_RECHECK_SECONDS=3600)
    a = hx.user()
    with a.websocket_connect("/api/ws", headers=ORIGIN) as ws:
        assert ws.receive_json() == {"type": "hello"}
        hx.state.hub.notify([_uid(hx, a)], {"type": "session"})  # still valid: re-checked, nothing sent
        assert ws.receive_json() == {"type": "ping"}


# ----------------------------------------------------------------------------------------- Argon2 slots
def test_hashes_wait_for_a_free_slot(monkeypatch):
    running, peak, lock = [0], [0], threading.Lock()

    class SlowHasher:
        def hash(self, pw):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            time.sleep(0.05)
            with lock:
                running[0] -= 1
            return "h"

    monkeypatch.setattr(passwords, "_hasher", SlowHasher())
    passwords.configure(2)
    try:
        threads = [threading.Thread(target=passwords.hash_password, args=("x",)) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert peak[0] == 2
    finally:
        passwords.configure(0)


def test_a_full_queue_answers_busy(monkeypatch):
    from app.errors import AppError

    passwords.configure(1)
    monkeypatch.setattr(passwords, "HASH_WAIT_SECONDS", 0.05)
    try:
        held = passwords._slots
        held.acquire()
        try:
            with pytest.raises(AppError) as e:
                passwords.hash_password("x")
            assert e.value.status == 503 and e.value.code == "busy"
        finally:
            held.release()
        assert passwords.verify_password(passwords.hash_password("pass-1234"), "pass-1234")
    finally:
        passwords.configure(0)


# ----------------------------------------------------------------------------------------- one runner per task
def test_leader_on_sqlite_is_always_this_process(hx):
    from app.services import leader

    if hx.state.database.engine.dialect.name != "sqlite":
        pytest.skip("SQLite only")
    with hx.db() as db1, hx.db() as db2:
        assert leader.take(db1, "cleanup") and leader.take(db2, "cleanup")


def test_leader_is_exclusive_on_postgresql(hx):
    from app.services import leader

    if hx.state.database.engine.dialect.name != "postgresql":
        pytest.skip("needs DZ_TEST_DATABASE_URL (PostgreSQL)")
    db1 = hx.state.database.SessionLocal()
    db2 = hx.state.database.SessionLocal()
    try:
        assert leader.take(db1, "cleanup") is True
        assert leader.take(db2, "cleanup") is False  # another worker skips this tick
        assert leader.take(db2, "market") is True  # other tasks are independent
        db1.commit()  # the lock ends with the transaction
        db2.rollback()
        assert leader.take(db2, "cleanup") is True
    finally:
        db1.close()
        db2.close()


def test_startup_lock_serializes_workers(hx):
    if hx.state.database.engine.dialect.name != "postgresql":
        with hx.state.database.startup_lock():  # nothing to do on SQLite
            return
    order: list[str] = []

    def worker(name: str) -> None:
        with hx.state.database.startup_lock():
            order.append(f"{name}+")
            time.sleep(0.2)
            order.append(f"{name}-")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert order in (["a+", "a-", "b+", "b-"], ["b+", "b-", "a+", "a-"])


# ----------------------------------------------------------------------------------------- schema and settings
def test_the_new_indexes_exist(hx):
    insp = inspect(hx.state.database.engine)
    assert "ix_posts_pinned" in {i["name"] for i in insp.get_indexes("posts")}
    assert "ix_notifications_created_at" in {i["name"] for i in insp.get_indexes("notifications")}
    if hx.state.database.engine.dialect.name == "postgresql":
        assert "ix_users_name_trgm" in {i["name"] for i in insp.get_indexes("users")}


def test_capacity_warning_when_the_pools_do_not_fit(hx, caplog):
    from app.main import _check_capacity

    if hx.state.database.engine.dialect.name != "postgresql":
        pytest.skip("needs DZ_TEST_DATABASE_URL (PostgreSQL)")
    s = hx.settings
    s.WEB_CONCURRENCY, s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW = 50, 10, 20
    with caplog.at_level(logging.WARNING, logger="dzplay"):
        _check_capacity(hx.state)
    assert "max_connections" in caplog.text
    caplog.clear()
    s.WEB_CONCURRENCY = 1
    with caplog.at_level(logging.WARNING, logger="dzplay"):
        _check_capacity(hx.state)
    assert "max_connections" not in caplog.text


def test_workers_that_skip_a_market_round_read_the_shared_snapshot(hx):
    import httpx

    from app.services import market
    from tests.test_market import FakeBybit

    with hx.db() as db:
        market.refresh(db, hx.settings, transport=httpx.MockTransport(FakeBybit()), force=True)
    saved = market._mem.snapshot
    assert saved
    market.reset_memory()
    assert not market._mem.snapshot  # this worker did not refresh
    with hx.db() as db:
        market.load_shared(db)
    assert market._mem.snapshot == saved
    market.reset_memory()


# ----------------------------------------------------------------------------------------- cleanup in batches
def test_cleanup_purges_big_backlogs_in_short_batches(hx, monkeypatch):
    from app.services import cleanup, notify

    a = hx.user()
    uid = _uid(hx, a)
    old = clock.utcnow() - timedelta(days=notify.KEEP_DAYS + 5)
    with hx.db() as db:
        for _ in range(25):
            db.add(Notification(user_id=uid, kind="comment", created_at=old))
        db.add(Notification(user_id=uid, kind="comment", created_at=clock.utcnow()))
    monkeypatch.setattr(cleanup, "BATCH", 10)
    left = []
    for _ in range(3):
        with hx.db() as db:
            cleanup.run_cleanup(db, hx.settings)
        with hx.db() as db:
            left.append(db.scalar(select(Notification.id).where(Notification.created_at == old).limit(1)) is not None)
    assert left == [True, True, False]  # 10 + 10 + 5: three runs, each one short
    with hx.db() as db:
        assert len(db.execute(select(Notification.id).where(Notification.user_id == uid)).all()) == 1  # the recent one


def test_missing_cached_pictures_lose_ready_in_one_statement(hx):
    a = hx.user()
    uid = _uid(hx, a)
    with hx.db() as db:
        for i in range(30):
            db.add(MediaItem(id=f"{i:032x}", owner_id=uid, purpose="idea", kind="image", state="attached", ready=True,
                             created_at=clock.utcnow()))
    with hx.db() as db:
        hx.state.media.purge(db)
    with hx.db() as db:
        assert db.scalar(select(MediaItem.id).where(MediaItem.owner_id == uid, MediaItem.ready.is_(True))) is None


# ----------------------------------------------------------------------------------------- pictures in one query
def _media_selects(hx, fn) -> int:
    from sqlalchemy import event

    seen = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("SELECT") and "FROM media_items" in statement:
            seen.append(statement)

    event.listen(hx.state.database.engine, "before_cursor_execute", count)
    try:
        fn()
    finally:
        event.remove(hx.state.database.engine, "before_cursor_execute", count)
    return len(seen)


def test_a_page_reads_every_picture_in_one_query(hx):
    viewer = hx.user()
    authors = [hx.user() for _ in range(6)]
    with hx.db() as db:
        for i, c in enumerate(authors):
            u = db.scalar(select(User).where(User.email == c.email))
            mid = f"{i + 100:032x}"
            db.add(MediaItem(id=mid, owner_id=u.id, purpose="avatar", kind="image", state="attached", ready=False,
                             attached_type="user", attached_id=u.id, attached_at=clock.utcnow(), tg_file_id=f"f{i}",
                             created_at=clock.utcnow()))
            u.avatar_media_id = mid
    for i, c in enumerate(authors):
        assert c.post("/api/posts", json={"content": f"فكرة رقم {i} للاختبار"}).status_code == 201
    pages = {}
    n = _media_selects(hx, lambda: pages.update(feed=viewer.get("/api/posts/feed").json()))
    assert sum(1 for p in pages["feed"]["posts"] if p["author"].get("avatar_url")) == 6
    assert n <= 1  # one query for the six avatars (it was one per author)
    assert _media_selects(hx, lambda: viewer.get("/api/people")) <= 1


def test_a_picture_prepared_a_moment_ago_is_not_an_orphan(hx):
    """The cleanup ran while a picture was being prepared and served: its row was not committed yet, and the file was
    deleted as an "orphan" (500 under load). Only old files without a row are orphans."""
    import os

    cache = hx.state.media.dir
    cache.mkdir(parents=True, exist_ok=True)
    fresh, old = cache / ("a" * 32 + ".img.webp"), cache / ("b" * 32 + ".img.webp")
    fresh.write_bytes(b"x")
    old.write_bytes(b"x")
    os.utime(old, (time.time() - 3600, time.time() - 3600))
    with hx.db() as db:
        hx.state.media.purge(db)
    assert fresh.exists() and not old.exists()
