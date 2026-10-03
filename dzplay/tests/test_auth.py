from __future__ import annotations

from sqlalchemy import select

from app import clock
from app.models import SecurityEvent, User
from app.services import google_auth
from tests.conftest import PASSWORD

HOUR = 3600


def test_register_creates_session_and_hides_identity(hx):
    c = hx.client()
    r = hx.register(c, "Alice@Example.com")
    assert r.status_code == 201
    body = r.json()
    assert body["display_name"] == "dzplay"
    assert "email" not in r.text.lower() and "alice" not in r.text.lower()
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    me = c.get("/api/me")
    assert me.status_code == 200 and me.json()["display_name"] == "dzplay"
    with hx.db() as db:
        user = db.scalar(select(User))
        assert user.email == "alice@example.com"
        assert user.password_hash.startswith("$argon2id$")  # never plain text


def test_register_validation(hx):
    c = hx.client()
    ch = hx.challenge(c, "register")
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": "nope-nope", "antibot": ch, "gender": "male", "age_confirmed": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "password_mismatch"
    r = hx.register(c, "a@example.com", password="short")
    assert r.json()["error"]["code"] == "weak_password"
    r = hx.register(c, "not-an-email")
    assert r.json()["error"]["code"] == "invalid_email"


def test_antibot_required_single_use_and_purpose_bound(hx):
    c = hx.client()
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "gender": "male", "age_confirmed": True})
    assert r.status_code == 400 and r.json()["error"]["code"] == "antibot_required"

    ch = hx.challenge(c, "login")  # wrong purpose
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "antibot": ch, "gender": "male", "age_confirmed": True})
    assert r.json()["error"]["code"] == "antibot_invalid"

    ch = hx.challenge(c, "register")
    bad = dict(ch, number=ch["number"] + 1)
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "antibot": bad, "gender": "male", "age_confirmed": True})
    assert r.json()["error"]["code"] == "antibot_invalid"

    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "antibot": ch, "gender": "male", "age_confirmed": True})
    assert r.status_code == 201
    r = c.post("/api/auth/register", json={"email": "b@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "antibot": ch, "gender": "male", "age_confirmed": True})
    assert r.json()["error"]["code"] == "antibot_used"  # replay refused


def test_antibot_challenge_expires(hx):
    c = hx.client()
    ch = hx.challenge(c, "register")
    clock.advance(hx.settings.POW_CHALLENGE_TTL + 1)
    r = c.post("/api/auth/register", json={"email": "a@example.com", "password": PASSWORD, "password_confirm": PASSWORD, "antibot": ch, "gender": "male", "age_confirmed": True})
    assert r.json()["error"]["code"] == "antibot_expired"


def test_honeypot_rejected(hx):
    c = hx.client()
    r = hx.register(c, "bot@example.com", website="http://spam")
    assert r.status_code == 400
    with hx.db() as db:
        assert db.scalar(select(User)) is None
        assert db.scalar(select(SecurityEvent.type).where(SecurityEvent.type == "honeypot"))


def test_duplicate_email(hx):
    hx.user("dup@example.com")
    r = hx.register(hx.client(), "DUP@example.com")
    assert r.status_code == 409


def test_login_success_and_failure(hx):
    hx.user("a@example.com")
    c = hx.client()
    assert hx.login(c, "a@example.com", "wrong-password").status_code == 401
    clock.advance(2)
    r = hx.login(c, "a@example.com")
    assert r.status_code == 200 and c.get("/api/me").status_code == 200


def test_unknown_email_same_error_as_wrong_password(hx):
    hx.user("a@example.com")
    r1 = hx.login(hx.client(), "a@example.com", "wrong-password")
    r2 = hx.login(hx.client(), "nobody@example.com", "wrong-password")
    assert r1.status_code == r2.status_code == 401
    assert r1.json() == r2.json()


def _fail(hx, c, email, times):
    for _ in range(times):
        r = hx.login(c, email, "wrong-password")
        assert r.status_code == 401, r.text
        clock.advance(60)  # wait out the progressive delay


def test_three_failures_block_ip_for_that_account_for_4h(hx):
    hx.user("victim@example.com")
    hx.user("neighbour@example.com")
    attacker = hx.client(ip="203.0.113.7")
    _fail(hx, attacker, "victim@example.com", 3)

    r = hx.login(attacker, "victim@example.com")  # even the right password is refused
    assert r.status_code == 429 and r.json()["error"]["retry_after"] > 3 * HOUR
    # Someone else behind the same NAT/IP is not punished in the default scope.
    same_ip = hx.client(ip="203.0.113.7")
    assert hx.login(same_ip, "neighbour@example.com").status_code == 200
    # The real owner from another network can still log in.
    assert hx.login(hx.client(ip="198.51.100.1"), "victim@example.com").status_code == 200

    clock.advance(4 * HOUR + 1)
    assert hx.login(attacker, "victim@example.com").status_code == 200


def test_literal_ip_scope_blocks_whole_ip(make_harness):
    hx = make_harness(LOGIN_BLOCK_SCOPE="ip")
    hx.user("victim@example.com")
    hx.user("other@example.com")
    attacker = hx.client(ip="203.0.113.8")
    _fail(hx, attacker, "victim@example.com", 3)
    assert hx.login(hx.client(ip="203.0.113.8"), "other@example.com").status_code == 429
    assert hx.login(hx.client(ip="203.0.113.9"), "other@example.com").status_code == 200


def test_ip_blocked_after_failures_across_many_accounts(make_harness):
    hx = make_harness(IP_MAX_FAILED_LOGINS=4)
    attacker = hx.client(ip="203.0.113.10")
    for i in range(4):
        r = hx.login(attacker, f"target{i}@example.com", "guess")
        assert r.status_code == 401
    hx.user("innocent@example.com")
    assert hx.login(hx.client(ip="203.0.113.10"), "innocent@example.com").status_code == 429


def test_account_lock_against_distributed_guessing(make_harness):
    hx = make_harness(ACCOUNT_MAX_FAILED_LOGINS=3, ACCOUNT_LOCK_DURATION=900)
    hx.user("target@example.com")
    for i in range(3):
        assert hx.login(hx.client(ip=f"192.0.2.{i + 1}"), "target@example.com", "guess").status_code == 401
    assert hx.login(hx.client(ip="192.0.2.99"), "target@example.com").status_code == 429
    clock.advance(901)
    assert hx.login(hx.client(ip="192.0.2.99"), "target@example.com").status_code == 200


def test_progressive_delay_between_failures(hx):
    hx.user("a@example.com")
    c = hx.client()
    assert hx.login(c, "a@example.com", "bad-1").status_code == 401
    r = hx.login(c, "a@example.com", "bad-2")
    assert r.status_code == 429 and r.json()["error"]["code"] == "rate_limited"


def test_elevated_antibot_difficulty_after_failures(hx):
    hx.user("a@example.com")
    c = hx.client(ip="203.0.113.20")
    easy = hx.challenge(c, "login")
    assert easy["maxnumber"] == hx.settings.POW_MAX_NUMBER
    _fail(hx, c, "a@example.com", 1)
    hard = hx.challenge(c, "login")
    assert hard["maxnumber"] == hx.settings.POW_ELEVATED_MAX_NUMBER
    # An "easy" challenge obtained earlier is no longer accepted from this IP.
    r = c.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD, "antibot": easy})
    assert r.status_code == 400
    r = c.post("/api/auth/login", json={"email": "a@example.com", "password": PASSWORD, "antibot": hard})
    assert r.status_code == 200


def test_max_two_accounts_per_ip_per_4h(hx):
    ip = "203.0.113.30"
    assert hx.register(hx.client(ip=ip), "one@example.com").status_code == 201
    assert hx.register(hx.client(ip=ip), "two@example.com").status_code == 201
    r = hx.register(hx.client(ip=ip), "three@example.com")
    assert r.status_code == 429
    assert hx.register(hx.client(ip="203.0.113.31"), "three@example.com").status_code == 201
    clock.advance(4 * HOUR + 1)
    assert hx.register(hx.client(ip=ip), "four@example.com").status_code == 201


def test_trusted_ip_exempt_from_registration_limit(make_harness):
    hx = make_harness(TRUSTED_IPS="198.51.100.200")
    for i in range(4):
        assert hx.register(hx.client(ip="198.51.100.200"), f"staff{i}@example.com").status_code == 201


def test_raw_ips_are_never_stored(hx):
    ip = "203.0.113.77"
    hx.user("a@example.com", ip=ip)
    hx.login(hx.client(ip=ip), "a@example.com", "wrong")
    with hx.db() as db:
        hashes = db.scalars(select(SecurityEvent.ip_hash)).all()
        assert hashes and all(h and ip not in h for h in hashes)
        assert all(ip not in (u.registration_ip_hash or "") for u in db.scalars(select(User)))


def test_logout_revokes_session(hx):
    c = hx.user()
    token = c.cookies.get("dz_session")
    assert c.post("/api/auth/logout").status_code == 200
    other = hx.client()
    other.cookies.set("dz_session", token)
    assert other.get("/api/me").status_code == 401


def test_session_expires(make_harness):
    hx = make_harness(SESSION_DURATION=3600)
    c = hx.user()
    clock.advance(3601)
    assert c.get("/api/me").status_code == 401


def test_csrf_header_and_origin_required(hx):
    c = hx.user()
    r = c.post("/api/messages", json={"content": "hi"}, headers={"X-DZ-Requested": ""})
    assert r.status_code == 403
    r = c.post("/api/messages", json={"content": "hi"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_banned_user_cannot_login(hx):
    hx.user("bad@example.com")
    with hx.db() as db:
        db.scalar(select(User).where(User.email == "bad@example.com")).status = "banned"
    assert hx.login(hx.client(), "bad@example.com").status_code == 403


# ----------------------------------------------------------------- Google


def _google(hx, c, monkeypatch, claims: dict):
    nonce = c.get("/api/auth/google/nonce").json()["nonce"]
    full = {"iss": "https://accounts.google.com", "aud": hx.settings.GOOGLE_CLIENT_ID, "sub": "g-123",
            "email": "g@example.com", "email_verified": True, "nonce": nonce}
    full.update(claims)
    monkeypatch.setattr(google_auth, "verify_google_id_token", lambda token, cid: full)
    return c.post("/api/auth/google", json={"credential": "header.payload.signature"})


def test_google_sign_in_creates_independent_account(hx, monkeypatch):
    c = hx.client()
    r = _google(hx, c, monkeypatch, {})
    assert r.status_code == 200 and r.json()["display_name"] == "dzplay"
    assert "g@example.com" not in r.text
    assert c.get("/api/me").json()["sign_in_method"] == "google"
    with hx.db() as db:
        user = db.scalar(select(User))
        assert user.google_sub == "g-123" and user.password_hash is None and user.id != "g-123"
    # Same Google account later -> same DZPLAY account
    c2 = hx.client()
    assert _google(hx, c2, monkeypatch, {}).status_code == 200
    with hx.db() as db:
        assert len(db.scalars(select(User)).all()) == 1


def test_google_rejects_bad_tokens(hx, monkeypatch):
    c = hx.client()
    assert _google(hx, c, monkeypatch, {"email_verified": False}).status_code == 401
    assert _google(hx, c, monkeypatch, {"nonce": "replayed"}).status_code == 401
    assert _google(hx, c, monkeypatch, {"aud": "someone-else"}).status_code == 401
    assert _google(hx, c, monkeypatch, {"iss": "https://evil.example"}).status_code == 401

    def boom(token, cid):
        raise ValueError("bad signature")

    c.get("/api/auth/google/nonce")
    monkeypatch.setattr(google_auth, "verify_google_id_token", boom)
    assert c.post("/api/auth/google", json={"credential": "x.y.z"}).status_code == 401


def test_google_does_not_take_over_password_account(hx, monkeypatch):
    hx.user("g@example.com")
    r = _google(hx, hx.client(), monkeypatch, {})
    assert r.status_code == 409 and r.json()["error"]["code"] == "email_registered_with_password"


def test_google_disabled_without_client_id(make_harness):
    hx = make_harness(GOOGLE_CLIENT_ID="")
    c = hx.client()
    assert c.get("/api/config").json()["google_client_id"] is None
    assert c.post("/api/auth/google", json={"credential": "x"}).status_code == 404


def test_new_google_account_must_complete_gender_and_age(hx, monkeypatch):
    c = hx.client()
    assert _google(hx, c, monkeypatch, {}).status_code == 200
    me = c.get("/api/me").json()
    assert me["needs_onboarding"] is True and me["age_confirmed"] is False
    hx.user()  # someone to receive
    r = c.post("/api/messages", json={"content": "مرحبا"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "onboarding_required"
    assert c.post("/api/me/onboarding", json={"gender": "male", "age_confirmed": False}).status_code == 400
    assert c.post("/api/me/onboarding", json={"gender": "robot", "age_confirmed": True}).status_code == 400
    done = c.post("/api/me/onboarding", json={"gender": "male", "age_confirmed": True}).json()
    assert done["needs_onboarding"] is False and done["age_confirmed"] is True and done["gender"] == "male"
    assert c.post("/api/messages", json={"content": "مرحبا"}).status_code == 201
