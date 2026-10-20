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


# ----------------------------------------------------------------------------------------- Argon2 gate
class _SlowHasher:
    def __init__(self, seconds=0.05):
        self.seconds, self.running, self.peak, self.lock = seconds, 0, 0, threading.Lock()

    def _work(self):
        with self.lock:
            self.running += 1
            self.peak = max(self.peak, self.running)
        time.sleep(self.seconds)
        with self.lock:
            self.running -= 1

    def hash(self, pw):
        self._work()
        return "h"

    def verify(self, h, pw):
        self._work()
        return False


@pytest.fixture
def slow_hasher(monkeypatch):
    fake = _SlowHasher()
    monkeypatch.setattr(passwords, "_hasher", fake)
    yield fake
    passwords.configure()


def _hash_all(n):
    errors = []

    def one():
        try:
            passwords.hash_password("x")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=one) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errors


def test_never_more_hashes_at_once_than_the_slots(slow_hasher):
    passwords.configure(2, queue=20, wait=5)
    assert _hash_all(8) == [] and slow_hasher.peak == 2


def test_beyond_the_queue_the_answer_is_busy_at_once(slow_hasher):
    from app.errors import AppError

    slow_hasher.seconds = 0.3
    passwords.configure(1, queue=2, wait=5)
    t0 = time.monotonic()
    errors = _hash_all(8)  # 1 running + 2 waiting; the other 5 are refused without waiting
    assert len(errors) == 5 and all(isinstance(e, AppError) and e.status == 503 and e.code == "busy" for e in errors)
    assert all(e.retry_after == passwords.RETRY_AFTER for e in errors)
    assert time.monotonic() - t0 < 1.5 and passwords.gate().inside == 0


def test_a_short_wait_then_busy(slow_hasher):
    slow_hasher.seconds = 0.5
    passwords.configure(1, queue=5, wait=0.05)
    errors = _hash_all(3)  # the 2 waiting give up after 50 ms
    assert len(errors) == 2 and passwords.gate().inside == 0


def test_default_slots_share_the_cpus_between_workers(monkeypatch):
    monkeypatch.setattr(passwords.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3}, raising=False)
    assert passwords.default_slots(1) == 4 and passwords.default_slots(2) == 2 and passwords.default_slots(8) == 1
    monkeypatch.setattr(passwords.os, "sched_getaffinity", lambda _pid: {0, 1}, raising=False)
    monkeypatch.setattr(passwords.os, "cpu_count", lambda: 16)  # a container pinned to 2 cores of a 16-core host
    assert passwords.default_slots(1) == 2


def test_a_login_wave_gets_busy_while_the_rest_of_the_app_answers(hx):
    """The gate is full (as in a wave): a login is answered 503 + Retry-After at once, and browsing still works."""
    a = hx.user()
    passwords.configure(1, queue=0, wait=0)
    gate = passwords.gate()
    try:
        with gate:  # one hash running: the only slot is taken
            r = hx.login(hx.client(), a.email)
            assert r.status_code == 503 and r.json()["error"]["code"] == "busy" and r.headers["Retry-After"] == "3"
            assert a.get("/api/posts/feed").status_code == 200 and a.get("/api/me").status_code == 200
        assert hx.login(hx.client(), a.email).status_code == 200  # the slot is free again
    finally:
        passwords.configure()


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


# ----------------------------------------------------------------------------------------- the pinned post, kept
def test_the_pinned_post_is_kept_and_changes_show_at_once(hx):
    from sqlalchemy import event

    from app.services import official

    from tests.test_official_v19 import _publish

    admin = hx.admin()
    reader = hx.user()
    first = _publish(hx, "إعلان أول").json()["id"]
    second = _publish(hx, "إعلان ثانٍ").json()["id"]
    assert admin.post(f"/api/admin/official/posts/{first}/pin", json={"hours": 24}).status_code == 200
    assert reader.get("/api/posts/feed").json()["posts"][0]["id"] == first

    pinned_queries = []

    def count(conn, cursor, statement, *args):
        if "pinned_until >" in statement:
            pinned_queries.append(statement)

    event.listen(hx.state.database.engine, "before_cursor_execute", count)
    try:
        for _ in range(5):
            assert reader.get("/api/posts/feed").json()["posts"][0]["id"] == first
        assert pinned_queries == []  # kept: no search of the posts for each page
        assert admin.post(f"/api/admin/official/posts/{second}/pin", json={"hours": 24}).status_code == 200
        assert reader.get("/api/posts/feed").json()["posts"][0]["id"] == second  # at once, not after a minute
        assert admin.post(f"/api/admin/official/posts/{second}/pin", json={"hours": 0}).status_code == 200
        assert all(p["id"] not in (first, second) or not p.get("pinned") for p in reader.get("/api/posts/feed").json()["posts"][:1])
    finally:
        event.remove(hx.state.database.engine, "before_cursor_execute", count)
    official.forget_pinned()


# ----------------------------------------------------------------------------------------- graceful stop
def test_uvicorn_is_pid_1_so_a_stop_is_graceful():
    """Profiling showed `sh -c uvicorn …` as PID 1: the shell ignored Docker's SIGTERM, so every update waited 10 s
    and then killed in-flight requests and open sockets. `exec` makes uvicorn PID 1 (it shuts down cleanly)."""
    from pathlib import Path

    cmd = next(line for line in (Path(__file__).resolve().parents[1] / "Dockerfile").read_text().splitlines()
               if line.startswith("CMD "))
    assert '"exec uvicorn app.main:app' in cmd


def test_each_websocket_no_longer_writes_two_log_lines(hx, caplog):
    """F (5,000 sockets): uvicorn's per-socket INFO lines were 3.5 % of the CPU and most of the capped log."""
    import logging

    log = logging.getLogger("uvicorn.error")
    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        log.info('%s - "WebSocket %s" [accepted]', "10.0.0.1:5000", "/api/ws")
        log.info("connection open")
        log.info('%s - "WebSocket %s" 403', "10.0.0.1:5000", "/api/ws")  # refusals stay
        log.warning("connection open")  # anything above INFO stays
    assert [r.getMessage() for r in caplog.records] == ['10.0.0.1:5000 - "WebSocket /api/ws" 403', "connection open"]


# ----------------------------------------------------------------------------------------- a picture view, cheaper
def test_a_popular_picture_is_not_written_on_every_view(hx):
    """Scenario A's profile: 58 % of the app's CPU served pictures; each view resolved its path (realpath) and ran
    an UPDATE of last_access. Now: one write per picture per process every 2 minutes, and the same LRU order."""
    from sqlalchemy import event

    from app.models import MediaCacheEntry

    store, key = hx.state.media, "ab" * 16
    with hx.db() as db:
        db.add(MediaCacheEntry(key=f"{key}.img.webp", asset_id=key, size=1, created_at=clock.utcnow(),
                               last_access=clock.utcnow() - timedelta(hours=1)))
    updates = []

    def count(conn, cursor, statement, *args):
        if statement.lstrip().upper().startswith("UPDATE") and "media_cache" in statement:
            updates.append(statement)

    event.listen(hx.state.database.engine, "before_cursor_execute", count)
    try:
        for _ in range(20):
            with hx.db() as db:
                store.touch(db, key, "img")
    finally:
        event.remove(hx.state.database.engine, "before_cursor_execute", count)
    assert len(updates) == 1
    with hx.db() as db:
        assert db.get(MediaCacheEntry, f"{key}.img.webp").last_access > clock.utcnow() - timedelta(minutes=1)
    assert store.path_for(key, "img") == store.dir / f"{key}.img.webp"
    for bad in ("../" + key[3:], key.upper(), key + "/x", ""):
        with pytest.raises(ValueError):
            store.path_for(bad, "img")


def test_hashes_run_at_a_low_cpu_priority():
    """G with 2 workers: one hash keeps ~1.3 cores busy, so a wave took the CPU from browsing (read p95 → ~29 s).
    The hashing threads now run at nice HASH_NICE: browsing and messages come first."""
    import os

    if not hasattr(os, "getpriority"):
        pytest.skip("no thread priorities on this system")
    base = os.getpriority(os.PRIO_PROCESS, 0)
    passwords.configure(1, queue=0, wait=0)
    try:
        nice = passwords.gate().run(lambda: os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
        assert nice == max(passwords.HASH_NICE, base)
        assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) == base  # the request thread is untouched
        assert passwords.verify_password(passwords.hash_password("pw-123456"), "pw-123456")
    finally:
        passwords.configure()


def test_a_sign_in_flood_is_refused_before_it_takes_the_worker(hx):
    """G at double load: the wave alone sent more requests than the server serves. Beyond AUTH_MAX_IN_FLIGHT the
    sign-in and sign-up calls are refused at once (no thread, no database); browsing and logout are never counted."""
    a = hx.user()
    cap = hx.state.settings.AUTH_MAX_IN_FLIGHT or max(4, hx.state.settings.THREADPOOL_SIZE // 4)
    hx.state.auth_in_flight = cap  # as if `cap` sign-ins were being served right now
    try:
        c = hx.client()
        for path, body in (("/api/auth/challenge", {"purpose": "login"}), ("/api/auth/login", {"email": a.email, "password": "x"})):
            r = c.post(path, json=body)
            assert r.status_code == 503 and r.json()["error"]["code"] == "busy" and r.headers["Retry-After"] == "3"
        assert a.get("/api/posts/feed").status_code == 200 and a.get("/api/me").status_code == 200
    finally:
        hx.state.auth_in_flight = 0
    assert hx.login(hx.client(), a.email).status_code == 200
    assert hx.state.auth_in_flight == 0  # every served call gave its place back


def test_pillow_only_ever_decodes_the_accepted_formats(hx, tmp_path):
    """pip-audit (phase 21): 8 advisories in Pillow 12.3 (TIFF, GIF, PPM, EPS, PDF decoders). Uploads are sniffed to
    JPEG/PNG/WebP/HEIC first; on top of that Pillow itself may now open only those, whatever the file contains."""
    from PIL import Image

    from app.services import media, media_check

    tiff, ok = tmp_path / "x.bin", tmp_path / "ok.bin"
    Image.new("RGB", (40, 40), "red").save(tiff, "TIFF")
    Image.new("RGB", (40, 40), "red").save(ok, "PNG")
    with pytest.raises(media_check.Rejected):
        media_check.process_image(hx.state.settings, tiff, tmp_path / "out.webp", {"kind": "image"})
    with pytest.raises(media.MediaError):
        media.prepare_image(hx.state.settings, tiff, tmp_path / "out2.webp")
    assert media_check.process_image(hx.state.settings, ok, tmp_path / "out3.webp", {"kind": "image"})["width"] == 40
    assert media.prepare_image(hx.state.settings, ok, tmp_path / "out4.webp")["width"] == 40
