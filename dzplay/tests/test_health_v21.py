"""V6 phase 21: request timings per endpoint, «صحة النظام» in the panel, the Telegram alert check, the open WebSockets
count, and the load-test collector's view (staging only)."""

from __future__ import annotations

import os
import uuid

import pytest

from app.services import health
from app.services.metrics import BOUNDS_MS, Metrics, percentile

REDIS = os.environ.get("DZ_TEST_REDIS_URL", "")
TOKEN = "lt-" + "y" * 40


def test_percentiles_from_the_histogram():
    hist = [0] * (len(BOUNDS_MS) + 1)
    hist[BOUNDS_MS.index(50)] = 90  # 90 answers between 25 and 50 ms
    hist[BOUNDS_MS.index(1000)] = 10  # 10 between 750 and 1000 ms
    assert 25 <= percentile(hist, 100, 0.5) <= 50
    assert 750 <= percentile(hist, 100, 0.95) <= 1000
    assert percentile(hist, 0, 0.95) == 0


def _fill(m: Metrics) -> None:
    for _ in range(90):
        m.observe("GET /api/posts/feed", 200, 40)
    for _ in range(10):
        m.observe("GET /api/posts/feed", 500, 2500)
    for _ in range(20):
        m.observe("POST /api/posts", 201, 120)


def test_snapshot_totals_slowest_and_errors_in_one_process():
    m = Metrics()
    _fill(m)
    m.flush()
    snap = m.snapshot(5)
    t = snap["totals"]
    assert t["count"] == 120 and t["errors"] == 10 and round(t["error_pct"], 1) == 8.3
    feed = next(e for e in snap["endpoints"] if e["endpoint"] == "GET /api/posts/feed")
    assert feed["count"] == 100 and feed["p50_ms"] <= 50 and feed["p99_ms"] >= 2000
    assert snap["slowest"][0]["endpoint"] == "GET /api/posts/feed"


@pytest.mark.skipif(not REDIS, reason="needs DZ_TEST_REDIS_URL")
def test_several_workers_add_up_in_redis():
    ns = f"t{uuid.uuid4().hex[:8]}"
    a, b = Metrics(REDIS, namespace=ns), Metrics(REDIS, namespace=ns)
    b._me = "another-worker"  # two processes (here: one test process)
    _fill(a)
    _fill(b)
    a.ws_changed(3)
    b.ws_changed(2)
    a.flush()
    b.flush()
    snap = Metrics(REDIS, namespace=ns).snapshot(5)
    assert snap["totals"]["count"] == 240 and snap["totals"]["errors"] == 20 and snap["websockets"] == 5


def test_middleware_labels_are_route_templates_never_urls(hx):
    c = hx.user()
    pid = c.post("/api/posts", json={"content": "فكرة للقياس"}).json()["id"]
    c.get(f"/api/posts/{pid}")
    c.get("/api/me")
    hx.admin().get("/api/admin/stats")
    hx.state.metrics.flush()
    snap = hx.state.metrics.snapshot(5)
    labels = {e["endpoint"] for e in snap["endpoints"]}
    assert {"GET /api/posts/{post_id}", "POST /api/posts", "GET /api/me", "GET /api/admin/stats"} <= labels
    text = str(snap)
    assert pid not in text and "/test-panel" not in text and "@example.com" not in text


def test_panel_health_page_is_for_admins(hx):
    u = hx.user()
    assert u.get("/test-panel/api/admin/health").status_code == 401
    d = hx.admin().get("/api/admin/health", params={"minutes": 5}).json()
    assert set(d) == {"metrics", "system", "database", "redis", "thresholds"}
    assert d["metrics"]["minutes"] == 5 and d["system"]["cpus"] >= 1 and d["thresholds"]["p95_ms"] == 1000
    assert hx.admin().get("/api/admin/health", params={"minutes": 999}).status_code in (400, 422)


def test_alert_rules(hx):
    s = hx.settings
    m = Metrics()
    for _ in range(200):
        m.observe("GET /api/posts/feed", 200, 2500)
    m.flush()
    keys = [k for k, _ in health.problems(s, m.snapshot(5))]
    assert keys == ["app:latency"]
    for _ in range(50):
        m.observe("POST /api/posts", 500, 30)
    m.flush()
    found = dict(health.problems(s, m.snapshot(5)))
    assert set(found) == {"app:latency", "app:errors"} and "GET /api/posts/feed" in found["app:latency"]
    quiet = Metrics()
    for _ in range(10):  # too few requests: no alert on percentages
        quiet.observe("GET /api/me", 500, 9000)
    quiet.flush()
    assert health.problems(s, quiet.snapshot(5)) == []


@pytest.mark.skipif(not REDIS, reason="needs DZ_TEST_REDIS_URL (the CLI reads the workers' timings in Redis)")
def test_health_check_command_prints_monitor_lines(hx, capsys):
    from app.admin_cli import _health_check

    hx.admin().put("/api/admin/settings", json={"changes": {"HEALTH_P95_MS": 200}})
    m = Metrics(REDIS)
    for _ in range(150):
        m.observe("GET /api/slow-for-test", 200, 900)
    m.flush()
    _health_check(hx.settings.model_copy(), hx.state.database)
    out = capsys.readouterr().out
    assert out.startswith("app:latency\t") and "200 ms" in out


def test_open_websockets_are_counted(hx):
    c = hx.user()
    with c.websocket_connect("/api/ws", headers={"Origin": "http://testserver"}) as ws:
        assert ws.receive_json()["type"] == "hello"
        assert hx.state.metrics.ws_open == 1
    assert hx.state.metrics.ws_open == 0


def test_collector_view_in_staging_only(make_harness, hx):
    st = make_harness(ENV="staging", LOADTEST_MODE=True, LOADTEST_TOKEN=TOKEN, COOKIE_SECURE=False)
    c = st.client()
    assert c.get("/healthz/metrics").status_code == 404
    d = c.get("/healthz/metrics", headers={"X-Loadtest-Token": TOKEN}).json()
    assert d["metrics"]["minutes"] == 1 and "database" in d
    assert hx.client().get("/healthz/metrics", headers={"X-Loadtest-Token": TOKEN}).status_code == 404
