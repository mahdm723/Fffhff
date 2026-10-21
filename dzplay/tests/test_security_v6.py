"""V6 phase 9: the security audit, as tests (see docs/SECURITY_AUDIT.md, section V6).

Money (only the ledger service writes amounts; ids of others are never reachable; extra fields are ignored),
every admin route closed to users, removed features really gone, privacy (no e-mail or internal id in public
answers), people listing rate limit, headers and cookies, no secrets in the code or the git history, no
"Trojan Source" characters in the source, and the Android signing key never in the repository."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import MembershipRequest, User, WithdrawalRequest
from tests.conftest import ADMIN_PATH

ROOT = Path(__file__).resolve().parent.parent  # dzplay/
REPO = ROOT.parent
SOURCE_DIRS = ("app", "static", "deploy", "tests", "e2e", "android")
SOURCE_EXT = {".py", ".js", ".html", ".css", ".sh", ".java", ".xml", ".gradle", ".md", ".json", ".yml", ".yaml", ".toml"}


def _sources():
    for d in SOURCE_DIRS:
        for p in (ROOT / d).rglob("*"):
            if p.is_file() and p.suffix in SOURCE_EXT and "__pycache__" not in p.parts and ".venv" not in p.parts \
                    and "node_modules" not in p.parts and "build" not in p.parts:
                yield p


# ------------------------------------------------------------------ money


def test_only_the_ledger_service_creates_ledger_entries():
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "app").rglob("*.py")
                 if "LedgerEntry(" in p.read_text() and p.name not in ("ledger.py", "models.py")]
    assert offenders == [], offenders
    # and nothing updates an amount in place
    for p in (ROOT / "app").rglob("*.py"):
        text = p.read_text()
        assert not re.search(r"\.amount_minor\s*[+\-]?=", text), p
        assert not re.search(r"update\(LedgerEntry\)", text), p


def test_money_endpoints_ignore_extra_fields_and_other_peoples_ids(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    # extra fields in a membership request are ignored (status, amount, member flags)
    r = a.post("/api/membership/requests", json={"txid": "a" * 64, "status": "accepted", "amount": 0,
                                                 "member_since": "2020-01-01", "verified": True})
    assert r.status_code in (200, 201, 400, 409, 503), r.text
    with hx.db() as db:
        u = db.scalar(select(User).where(User.email == a.email))
        assert u.member_since is None and u.verified_at is None
        req = db.scalar(select(MembershipRequest).where(MembershipRequest.user_id == u.id))
        assert req is None or req.status == "pending"
    # nothing about another user can be read through the money endpoints (they have no ids: only "mine")
    for path in ("/api/membership", "/api/rewards", "/api/rewards/referrals"):
        ra, rb = a.get(path), b.get(path)
        assert ra.status_code == rb.status_code == 200
        assert a.email not in rb.text and b.email not in ra.text
    # onboarding / profile updates cannot set membership, the star or a balance
    a.post("/api/me/onboarding", json={"display_name": "Safe Name", "age_confirmed": True, "verified": True,
                                       "member_since": "2020-01-01", "role": "super_admin", "balance": 1000})
    me = a.get("/api/me").json()
    assert me["verified"] is False and me["member"] is False
    with hx.db() as db:
        assert db.scalar(select(WithdrawalRequest)) is None


# ------------------------------------------------------------------ admin routes


def all_routes(app):
    """(method, full path) of every route, including those of included routers."""
    for r in app.routes:
        if type(r).__name__ == "_IncludedRouter":
            prefix = r.include_context.prefix or ""
            for sub in r.original_router.routes:
                for m in getattr(sub, "methods", ()) or ():
                    yield m, prefix + sub.path
        else:
            for m in getattr(r, "methods", ()) or ():
                yield m, getattr(r, "path", "")


def test_every_admin_route_is_closed_to_users(make_harness):
    hx = make_harness()
    user = hx.user()
    anon = hx.client()
    routes = sorted({(m, p) for m, p in all_routes(hx.app) if p.startswith(ADMIN_PATH + "/api/admin")
                     and m in ("GET", "POST", "PUT", "DELETE")})
    assert len(routes) > 60, len(routes)
    # login and session are public by design; logout only clears the (absent) cookie
    open_ok = {ADMIN_PATH + "/api/admin/login", ADMIN_PATH + "/api/admin/session", ADMIN_PATH + "/api/admin/logout"}
    for method, path in routes:
        if path in open_ok:
            continue
        url = re.sub(r"\{[^}]+\}", "1", path)
        for c in (user, anon):
            r = c.request(method, url, json={})
            assert r.status_code in (401, 403, 404, 405, 429), (method, path, r.status_code, r.text[:120])  # 429: refused faster


# ------------------------------------------------------------------ removed features


def test_removed_features_have_no_route_and_no_service():
    from app.main import create_app

    app = create_app()
    paths = " ".join(p for _m, p in all_routes(app)).lower()
    assert "/api/posts" in paths  # the enumeration sees the included routers
    for word in ("/calls", "/call/", "/reels", "/turn", "/studio", "/anonymous", "/match", "/reveal", "/earnings"):
        assert word not in paths, word
    compose = (ROOT / "docker-compose.yml").read_text()
    assert not re.search(r"^\s+coturn:", compose, re.M) and "3478" not in compose


# ------------------------------------------------------------------ privacy


def test_public_answers_never_carry_emails_or_internal_ids(make_harness):
    hx = make_harness()
    a, b = hx.user(), hx.user()
    pid = a.post("/api/posts", json={"content": "تحليل اليوم: BTC فوق الدعم"}).json()["id"]
    b.put(f"/api/posts/{pid}/reaction", json={"reaction": "like"})
    b.post(f"/api/posts/{pid}/comments", json={"content": "رأي جميل"})
    with hx.db() as db:
        a_id = db.scalar(select(User.id).where(User.email == a.email))
        b_id = db.scalar(select(User.id).where(User.email == b.email))
    texts = [b.get("/api/posts").text, b.get(f"/api/posts/{pid}/comments").text, b.get(f"/api/posts/{pid}/likers").text,
             b.get("/api/people").text, a.get("/api/notifications").text]
    for t in texts:
        for secret in (a.email, b.email, a_id, b_id):
            assert secret not in t


def test_dislikers_and_old_private_comments_never_leak(make_harness):
    """Who disliked is never shown, and a comment from before V6 (private) stays between its writer and the post
    owner: not in the feed, the post, the comments, the profile, the people pages or anyone's notifications."""
    from app import clock
    from app.models import Comment

    hx = make_harness()
    owner, d, stranger = hx.user(), hx.user(), hx.user()
    pid = owner.post("/api/posts", json={"content": "فكرة عن السوق اليوم"}).json()["id"]
    assert d.put(f"/api/posts/{pid}/reaction", json={"reaction": "dislike"}).status_code == 200
    with hx.db() as db:
        d_id = db.scalar(select(User.id).where(User.email == d.email))
        db.add(Comment(post_id=pid, author_id=d_id, content="ملاحظة خاصة قديمة", created_at=clock.utcnow()))
        db.commit()
    d_pub, o_pub = d.get("/api/me").json()["public_id"], owner.get("/api/me").json()["public_id"]
    post_views = [f"/api/posts/{pid}", f"/api/posts/{pid}/likers", "/api/posts/feed", f"/api/profiles/{o_pub}",
                  f"/api/profiles/{o_pub}/posts"]
    for viewer in (owner, stranger):
        for path in post_views:
            assert d_pub not in viewer.get(path).text, (path, "the disliker is exposed")
    for path in [*post_views, f"/api/posts/{pid}/comments", "/api/people", f"/api/people/{d_pub}",
                 f"/api/profiles/{d_pub}", f"/api/profiles/{d_pub}/posts", "/api/notifications"]:
        assert "ملاحظة خاصة قديمة" not in stranger.get(path).text, path
    assert "ملاحظة خاصة قديمة" not in owner.get("/api/notifications").text


def test_people_listing_is_rate_limited(make_harness):
    hx = make_harness(SEARCH_PER_MINUTE=5)
    a = hx.user()
    codes = [a.get("/api/people").status_code for _ in range(8)]
    assert codes[:5] == [200] * 5 and 429 in codes[5:], codes


# ------------------------------------------------------------------ headers and cookies


def test_security_headers_and_cookie_flags(make_harness):
    hx = make_harness(COOKIE_SECURE=True)
    c = hx.client()
    r = c.get("/")
    h = r.headers
    assert "script-src 'self'" in h["content-security-policy"] and "unsafe-inline" not in h["content-security-policy"].split("script-src")[1].split(";")[0]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert h["referrer-policy"] == "no-referrer" and "camera=()" in h["permissions-policy"]
    assert "max-age=31536000" in h["strict-transport-security"]
    resp = hx.register(c, "cookie@example.com")
    assert resp.status_code == 201, resp.text
    cookie = resp.headers.get("set-cookie", "")
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=" in cookie.lower()


# ------------------------------------------------------------------ secrets and source hygiene


_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----"),
    re.compile(r"\b\d{8,10}:AA[A-Za-z0-9_-]{33}\b"),  # a Telegram bot token
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"\bxox[abpr]-[0-9A-Za-z-]{10,}\b"),  # Slack
    re.compile(r"\bghp_[0-9A-Za-z]{36}\b"),  # GitHub token
]


def test_no_secrets_in_the_source_tree():
    hits = []
    for p in _sources():
        text = p.read_text(errors="ignore")
        for pat in _SECRET_PATTERNS:
            if pat.search(text):
                hits.append((str(p.relative_to(ROOT)), pat.pattern[:30]))
    assert hits == [], hits


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def test_no_secrets_or_signing_keys_in_the_git_history():
    files = _git("log", "--all", "--name-only", "--pretty=format:")
    if files is None:
        pytest.skip("not a git checkout")
    names = {f.strip() for f in files.splitlines() if f.strip()}
    bad = sorted(n for n in names if re.search(r"\.(jks|keystore|p12|pem|key)$", n) or re.search(r"(^|/)\.env$", n))
    assert bad == [], bad
    # Commits whose diff adds a secret-looking value. The only known one is the owner's first upload of
    # bot.py(0) (2025-11-22, three Telegram bot tokens, removed in V3): see docs/SECURITY_AUDIT.md — the tokens
    # must be revoked in @BotFather. Any other commit fails this test.
    known = {"ced8f5e"}
    found = set()
    for pat in _SECRET_PATTERNS:
        out = _git("log", "--all", "--format=%h", "-G", pat.pattern.replace("\\b", ""), "--perl-regexp") or ""
        for c in out.split():
            diff = _git("show", "--format=", "--no-color", c) or ""
            if any(pat.search(line) for line in diff.splitlines() if line.startswith("+")):
                found.add(c[:7])
    assert found <= known, sorted(found - known)


def test_no_trojan_source_characters():
    """Bidirectional control characters can make code read differently from how it runs (CVE-2021-42574)."""
    bidi = re.compile("[\u202a-\u202e\u2066-\u2069]")
    hits = [str(p.relative_to(ROOT)) for p in _sources() if bidi.search(p.read_text(errors="ignore"))]
    assert hits == [], hits


def test_no_bybit_or_exchange_api_key_anywhere():
    for p in _sources():
        text = p.read_text(errors="ignore")
        assert not re.search(r"BYBIT_(API_)?(KEY|SECRET)\s*[=:]", text), p
    cfg = (ROOT / "app" / "config.py").read_text()
    assert "BYBIT_API" not in cfg  # the market uses public endpoints only


def test_privacy_texts_do_not_mention_engagement_boosting():
    """V6: no managed accounts and no boosted engagement — no text may claim otherwise."""
    for p in (ROOT / "static" / "js").rglob("*.js"):
        assert "لتنشيط المحتوى" not in p.read_text(), p
    from app.services import cms

    for key, sp in cms.registry().items():
        if isinstance(sp.default, str):
            assert "تنشيط" not in sp.default and "تعزيز التفاعل" not in sp.default, key


def test_json_answers_are_json(make_harness):
    """Error bodies never leak a stack trace or internal details."""
    hx = make_harness()
    c = hx.user()
    r = c.post("/api/posts", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code in (400, 422)
    body = json.dumps(r.json())
    assert "Traceback" not in body and "sqlalchemy" not in body.lower() and "/home/" not in body
