"""Owner dashboard: /admin page, activity counts, admin-token brute-force protection."""

from __future__ import annotations

import json

from app import clock
from app.api.admin import ADMIN_FAILURE_WINDOW, ADMIN_MAX_FAILURES
from tests.conftest import send

TOKEN = "admin-secret-token"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def post(c, content="فكرة عامة للتجربة."):
    return c.post("/api/posts", json={"content": content})


def test_admin_page_served_with_strict_csp(make_harness):
    hx = make_harness(ADMIN_API_TOKEN=TOKEN)
    r = hx.client().get("/admin")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert '<script type="module" src="/js/admin.js">' in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    # The page itself holds no data; everything comes from the token-protected API.
    assert TOKEN not in r.text


def test_admin_page_hidden_when_admin_disabled(hx):
    assert hx.client().get("/admin").status_code == 404


def test_activity_counts_only(make_harness):
    hx = make_harness(ADMIN_API_TOKEN=TOKEN)
    a, b = hx.user(), hx.user()
    assert post(a, "فكرة سرية لا يجب أن تظهر في اللوحة").status_code == 201
    send(b, "رسالة خاصة جدًا")
    hx.login(hx.client(), a.email, "wrong-password-1")

    c = hx.client()
    assert c.get("/api/admin/activity").status_code == 401
    r = c.get("/api/admin/activity", params={"days": 7, "tz": -60}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert len(data["days"]) == 7 and set(data["series"]) == {"users", "posts", "conversations", "failed_logins"}
    assert all(len(v) == 7 for v in data["series"].values())
    assert data["series"]["users"][-1] == 2
    assert data["series"]["posts"][-1] == 1
    assert data["series"]["conversations"][-1] == 1
    assert data["series"]["failed_logins"][-1] == 1
    for secret in ("فكرة سرية", "رسالة خاصة", "@example.com", a.email):
        assert secret not in r.text

    # Older days fall out of the window; today's bucket starts empty again.
    clock.advance(8 * 86400)
    later = c.get("/api/admin/activity", params={"days": 7}, headers=AUTH).json()
    assert sum(sum(v) for v in later["series"].values()) == 0
    assert c.get("/api/admin/activity", params={"days": 365}, headers=AUTH).status_code == 400


def test_wrong_admin_token_is_rate_limited_and_logged(make_harness):
    hx = make_harness(ADMIN_API_TOKEN=TOKEN)
    attacker = hx.client(ip="203.0.113.7")
    for _ in range(ADMIN_MAX_FAILURES):
        assert attacker.get("/api/admin/stats", headers={"Authorization": "Bearer guess"}).status_code == 401
    # Locked out: even the correct token is refused, so further guesses reveal nothing.
    r = attacker.get("/api/admin/stats", headers=AUTH)
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0
    # Other networks (the real owner) are not affected.
    owner = hx.client(ip="198.51.100.9")
    assert owner.get("/api/admin/stats", headers=AUTH).status_code == 200

    events = owner.get("/api/admin/security-events", params={"type": "admin_auth_failed"}, headers=AUTH).json()["events"]
    assert len(events) == ADMIN_MAX_FAILURES
    assert all(e["ip_ref"] and len(e["ip_ref"]) == 12 for e in events)
    assert "203.0.113.7" not in json.dumps(events)

    clock.advance(ADMIN_FAILURE_WINDOW + 1)
    assert attacker.get("/api/admin/stats", headers=AUTH).status_code == 200


def test_admin_post_requires_csrf_header(make_harness):
    hx = make_harness(ADMIN_API_TOKEN=TOKEN)
    c = hx.client()
    r = c.post("/api/admin/cleanup", headers={**AUTH, "X-DZ-Requested": ""})
    assert r.status_code == 403
    r = c.post("/api/admin/cleanup", headers=AUTH)
    assert r.status_code == 200 and "deleted" in r.json()

