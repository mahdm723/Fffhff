"""Admin panel: secret path, admin accounts + TOTP, throttling, sessions, audit log, activity."""

from __future__ import annotations

from sqlalchemy import select, update

from app import clock
from app.models import AdminAuditLog, SecurityEvent
from app.security import totp
from tests.conftest import ADMIN_PASSWORD, ADMIN_PATH

API = f"{ADMIN_PATH}/api/admin"


def post(c, content="فكرة عامة للتجربة."):
    return c.post("/api/posts", json={"content": content})


# ----------------------------------------------------------------- page + path


def test_panel_served_only_at_secret_path_with_strict_csp(hx):
    c = hx.client()
    r = c.get(ADMIN_PATH)
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert f'<script type="module" src="{ADMIN_PATH}/assets/admin.js">' in r.text
    csp = r.headers["Content-Security-Policy"]
    assert "script-src 'self'" in csp and "unsafe-inline" not in csp and "frame-ancestors 'none'" in csp
    assert r.headers["X-Robots-Tag"] == "noindex, nofollow"
    assert r.headers["Cache-Control"] == "no-store"
    assert c.get(f"{ADMIN_PATH}/assets/admin.js").status_code == 200
    # every module the panel imports is served from the allowlist with a JS MIME type
    import re
    seen, todo = set(), ["admin.js"]
    while todo:
        name = todo.pop()
        seen.add(name)
        r = c.get(f"{ADMIN_PATH}/assets/{name}")
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/javascript"), name
        todo += [m for m in re.findall(r"from '\./([\w-]+\.js)'", r.text) if m not in seen]
    assert {"admin-common.js", "admin-users.js", "admin-content.js", "admin-engage.js", "admin-system.js"} <= seen
    # nothing at guessable places, and the asset route is an allowlist
    for path in ("/admin", "/admin.html", "/js/admin.js", "/api/admin/stats", f"{ADMIN_PATH}/assets/..%2fmain.py",
                 f"{ADMIN_PATH}/assets/admin.html", f"{ADMIN_PATH}/assets/%2e%2e%2f%2e%2e%2fconfig.py"):
        assert c.get(path).status_code == 404, path


def test_ip_allowlist_hides_the_panel(make_harness):
    hx = make_harness(ADMIN_IP_ALLOWLIST="198.51.100.0/24")
    assert hx.client(ip="203.0.113.5").get(ADMIN_PATH).status_code == 404
    assert hx.client(ip="203.0.113.5").post(f"{API}/login", json={}).status_code == 404
    assert hx.client(ip="198.51.100.7").get(ADMIN_PATH).status_code == 200
    assert hx.admin(ip="198.51.100.8").get("/api/admin/stats").status_code == 200


# ----------------------------------------------------------------- login + 2FA


def test_login_requires_password_and_totp_and_rejects_replay(hx):
    secret = hx.create_admin("owner")
    c = hx.client()
    clock.advance(totp.STEP)
    good = totp.code_at(secret, totp.current_step(clock.timestamp()))
    assert hx.admin_login(c, "owner", code="000000").status_code == 401
    assert hx.admin_login(c, "owner", code=good, password="wrong-password-123").status_code == 401
    assert hx.admin_login(c, "nobody", code=good).status_code == 401
    r = hx.admin_login(c, "owner", code=good)
    assert r.status_code == 200 and r.json() == {"username": "owner", "role": "super_admin"}
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=strict" in cookie.replace("Strict", "strict") and f"Path={ADMIN_PATH}" in cookie
    assert c.get(f"{API}/session").json() == {"username": "owner", "role": "super_admin"}
    # The same code cannot be used twice (stolen-code replay).
    other = hx.client()
    assert hx.admin_login(other, "owner", code=good).status_code == 401
    assert other.get(f"{API}/session").status_code == 401


def test_login_lockout_per_network_and_per_username(make_harness):
    hx = make_harness(ADMIN_LOGIN_MAX_FAILURES=3)
    hx.create_admin("owner")
    attacker = hx.client(ip="203.0.113.7")
    for _ in range(3):
        assert hx.admin_login(attacker, "owner", code="123456").status_code == 401
    r = hx.admin_login(attacker, "owner")  # even the right code is refused now
    assert r.status_code == 429 and int(r.headers["Retry-After"]) > 0

    # The username is also locked from other networks (distributed guessing)...
    assert hx.admin_login(hx.client(ip="198.51.100.9"), "owner").status_code == 429
    # ...but another admin account still works.
    hx.create_admin("second")
    assert hx.admin_login(hx.client(ip="198.51.100.9"), "second").status_code == 200
    with hx.db() as db:
        assert len(db.scalars(select(SecurityEvent).where(SecurityEvent.type == "admin_login_failed")).all()) == 3

    clock.advance(hx.settings.ADMIN_LOGIN_WINDOW + 1)
    assert hx.admin_login(attacker, "owner").status_code == 200


def test_session_idle_timeout_and_logout(hx):
    c = hx.admin()
    assert c.get("/api/admin/stats").status_code == 200
    clock.advance(hx.settings.ADMIN_SESSION_IDLE + 5)
    assert c.get("/api/admin/stats").status_code == 401

    c2 = hx.client()
    assert hx.admin_login(c2, "owner").status_code == 200
    assert c2.post(f"{API}/logout").status_code == 200
    assert c2.get(f"{API}/stats").status_code == 401


def test_session_absolute_lifetime(make_harness):
    hx = make_harness(ADMIN_SESSION_IDLE=10 * 3600, ADMIN_SESSION_TTL=3600)
    c = hx.admin()
    clock.advance(1800)
    assert c.get("/api/admin/stats").status_code == 200
    clock.advance(1900)
    assert c.get("/api/admin/stats").status_code == 401


def test_admin_post_requires_csrf_header(hx):
    c = hx.admin()
    assert c.post("/api/admin/cleanup", headers={"X-DZ-Requested": ""}).status_code == 403
    assert c.post("/api/admin/cleanup", headers={"Origin": "https://evil.example"}).status_code == 403
    r = c.post("/api/admin/cleanup")
    assert r.status_code == 200 and "deleted" in r.json()


def test_user_session_is_not_an_admin_session(hx):
    u = hx.user()
    assert u.get(f"{API}/stats").status_code == 401


# ----------------------------------------------------------------- audit log


def test_actions_are_audited_and_chain_detects_tampering(hx):
    c = hx.admin()
    c.post("/api/admin/cleanup")
    c.get("/api/admin/reports")
    log = c.get("/api/admin/audit").json()
    actions = [e["action"] for e in log["entries"]]
    assert actions[:3] == ["view_reports", "cleanup", "login"]
    assert all(e["actor"] == "owner" for e in log["entries"])
    assert log["chain"] == {"ok": True, "checked": 3}

    with hx.db() as db:  # someone edits a row directly in the database
        db.execute(update(AdminAuditLog).where(AdminAuditLog.action == "cleanup").values(detail="999"))
    chain = c.get("/api/admin/audit").json()["chain"]
    assert chain["ok"] is False


def test_no_api_to_change_the_audit_log(hx):
    c = hx.admin()
    for method in ("put", "delete"):
        assert getattr(c, method)("/api/admin/audit").status_code in (404, 405)


# ----------------------------------------------------------------- activity


def test_activity_counts_only(hx):
    a, b = hx.user(), hx.user()
    assert post(a, "فكرة سرية لا يجب أن تظهر في اللوحة").status_code == 201
    from tests.conftest import send
    send(b, "رسالة خاصة جدًا")
    hx.login(hx.client(), a.email, "wrong-password-1")

    c = hx.admin()
    r = c.get("/api/admin/activity", params={"days": 7, "tz": -60})
    assert r.status_code == 200
    data = r.json()
    assert len(data["days"]) == 7 and set(data["series"]) == {"users", "posts", "conversations", "failed_logins"}
    assert data["series"]["users"][-1] == 2
    assert data["series"]["posts"][-1] == 1
    assert data["series"]["conversations"][-1] == 1
    assert data["series"]["failed_logins"][-1] == 1
    for secret in ("فكرة سرية", "رسالة خاصة", "@example.com", a.email):
        assert secret not in r.text
    assert c.get("/api/admin/activity", params={"days": 365}).status_code == 400


def test_cli_admin_creation_rules(hx):
    from app.services import admin_auth

    with hx.db() as db:
        for name, pw in (("ab", ADMIN_PASSWORD), ("good-name", "short"), ("bad name!", ADMIN_PASSWORD)):
            try:
                admin_auth.create_admin(db, hx.settings, name, pw)
            except ValueError:
                continue
            raise AssertionError(f"accepted {name!r}")
