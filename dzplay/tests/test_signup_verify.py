"""V6 phase 15: e-mail confirmation at signup (the account exists only after the code), and the owner's admin signup
code (12+ characters, hashed, off by default, expiry and uses, strict per-network tries, audit + Telegram)."""

from __future__ import annotations

import re
from datetime import timedelta

import pytest
from sqlalchemy import select

from app import clock
from app.models import AdminAuditLog, AppSetting, SignupPending, User
from app.services import access, auth
from tests import fake_telegram as tg
from tests.conftest import PASSWORD
from tests.smtp_sink import SmtpSink

HOOK = "hook-secret-0123456789"
ADMIN_CODE = "Owner-Signup-Code-2026!"


@pytest.fixture
def smtp():
    with SmtpSink() as sink:
        yield sink


def _hx(make_harness, smtp=None, **extra):
    fake_tg = tg.FakeTelegram()
    settings = dict(TELEGRAM_BOT_TOKEN=tg.TOKEN, TELEGRAM_ADMIN_CHAT_ID=str(tg.ADMIN_ID), TELEGRAM_WEBHOOK_SECRET=HOOK,
                    REGISTRATION_EMAIL_VERIFICATION=True, ADMIN_SESSION_IDLE=30 * 86400, ADMIN_SESSION_TTL=30 * 86400)
    if smtp is not None:
        settings.update(SMTP_HOST="127.0.0.1", SMTP_PORT=smtp.port, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test")
    settings.update(extra)
    hx = make_harness(telegram_transport=fake_tg.transport, **settings)
    hx.default_member = False
    hx.tg, hx.smtp = fake_tg, smtp
    return hx


def _code(smtp) -> str:
    return re.search(r"\b([0-9]{6})\b", smtp.last_text()).group(1)


def _start(hx, c, email, **extra):
    r = hx.register(c, email, **extra)
    assert r.status_code == 202, r.text
    hx.state.mail_queue.wait_idle()  # V6 phase 18: the code leaves from the mail queue
    return r.json()


def _user(hx, email) -> User | None:
    with hx.db() as db:
        return db.scalar(select(User).where(User.email == email))


def _step_up(hx) -> str:
    from app.security import totp

    clock.advance(totp.STEP)
    return totp.code_at(hx.admin_secrets["owner"], totp.current_step(clock.timestamp()))


# ---------------------------------------------------------------------------------------------- the e-mail code


def test_the_account_exists_only_after_the_code(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    assert hx.client().get("/api/config").json()["email_verification"] is True
    c = hx.client()
    d = _start(hx, c, "new.person@example.com")
    assert d["verify"] is True and d["sent_to"] == "n***@example.com" and d["code_length"] == 6
    assert _user(hx, "new.person@example.com") is None  # nothing created yet
    assert c.get("/api/me").status_code == 401
    msg = smtp.messages[-1]["msg"]
    assert "رمز تأكيد حسابك" in msg["Subject"] and msg["To"] == "new.person@example.com"
    code = _code(smtp)
    assert code in smtp.last_html()  # shown large in the HTML part
    with hx.db() as db:
        row = db.scalars(select(SignupPending)).one()
        assert code not in row.code_hash and PASSWORD not in row.password_hash  # hashes only

    wrong = "000000" if code != "000000" else "111111"
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": wrong})
    assert r.status_code == 400 and r.json()["error"]["code"] == "code_invalid"
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": code})
    assert r.status_code == 201, r.text
    assert c.get("/api/me").status_code == 200  # signed in
    u = _user(hx, "new.person@example.com")
    assert u.signup_verified_via == "email" and u.email_verified_at is not None
    assert u.email_canonical == "new.person@example.com"
    with hx.db() as db:
        assert db.scalars(select(SignupPending)).all() == []
    # the same token cannot make a second account
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": code})
    assert r.status_code == 400 and r.json()["error"]["code"] == "signup_expired"
    assert hx.login(hx.client(), "new.person@example.com").status_code == 200


def test_the_code_arrives_in_the_persons_language(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    c = hx.client()
    c.headers["X-DZ-Lang"] = "fr"
    d = _start(hx, c, "francais@example.com")
    assert "code de confirmation de votre compte" in smtp.messages[-1]["msg"]["Subject"]
    assert "Ceci est un message automatique" in smtp.last_text()
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code(smtp)})
    assert r.status_code == 201
    assert _user(hx, "francais@example.com").language == "fr"


def test_wrong_codes_cancel_and_codes_expire(make_harness, smtp):
    hx = _hx(make_harness, smtp, REGISTER_CODE_MAX_ATTEMPTS=3)
    c = hx.client()
    d = _start(hx, c, "tries@example.com")
    code = _code(smtp)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(2):
        assert c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": wrong}).json()["error"]["code"] == "code_invalid"
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": wrong})
    assert r.json()["error"]["code"] == "code_cancelled"
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": code})
    assert r.json()["error"]["code"] == "signup_expired" and _user(hx, "tries@example.com") is None

    d = _start(hx, hx.client(), "late@example.com")
    clock.advance(hx.settings.REGISTER_CODE_TTL + 1)
    r = hx.client().post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code(smtp)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "signup_expired"
    assert _user(hx, "late@example.com") is None


def test_resend_waits_and_replaces_the_code(make_harness, smtp):
    hx = _hx(make_harness, smtp, REGISTER_MAX_SENDS=2)
    c = hx.client()
    d = _start(hx, c, "again@example.com")
    first = _code(smtp)
    r = c.post("/api/auth/register/resend", json={"signup_token": d["signup_token"]})
    assert r.status_code == 429 and r.json()["error"]["retry_after"] > 0  # REGISTER_RESEND_SECONDS
    clock.advance(hx.settings.REGISTER_RESEND_SECONDS + 1)
    r = c.post("/api/auth/register/resend", json={"signup_token": d["signup_token"]})
    hx.state.mail_queue.wait_idle()
    assert r.status_code == 200 and len(smtp.messages) == 2
    second = _code(smtp)
    clock.advance(hx.settings.REGISTER_RESEND_SECONDS + 1)
    r = c.post("/api/auth/register/resend", json={"signup_token": d["signup_token"]})
    assert r.status_code == 429 and r.json()["error"]["code"] == "too_many_sends"
    if second != first:  # (1 in a million they are equal)
        r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": first})
        assert r.json()["error"]["code"] == "code_invalid"
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": second})
    assert r.status_code == 201


def test_one_mailbox_one_account_and_the_limits(make_harness, smtp):
    hx = _hx(make_harness, smtp, REGISTER_PER_EMAIL_PER_DAY=2, REGISTER_STARTS_PER_IP_PER_HOUR=3)
    assert auth.canonical_email("John.Doe+promo@GoogleMail.com") == "johndoe@gmail.com"
    assert auth.canonical_email("john.doe+x@example.com") == "john.doe@example.com"
    c = hx.client()
    d = _start(hx, c, "john.doe@gmail.com")
    assert c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": _code(smtp)}).status_code == 201
    # the same Gmail mailbox: the same answer as a new address (V6 phase 18), «لديك حساب بالفعل» by e-mail, no account
    _start(hx, hx.client(), "johndoe+second@googlemail.com")
    assert "لديك حساب بالفعل" in smtp.messages[-1]["msg"]["Subject"]
    with hx.db() as db:
        assert len(db.scalars(select(User).where(User.email_canonical == "johndoe@gmail.com")).all()) == 1
    # per mailbox per day (any spelling)
    for spelling in ("ma.ry@gmail.com", "mary+1@gmail.com"):
        _start(hx, hx.client(), spelling)
    r = hx.register(hx.client(), "m.a.r.y+2@gmail.com")
    assert r.status_code == 429
    # per network
    net = hx.client()
    for i in range(3):
        _start(hx, net, f"net{i}@example.com")
    assert hx.register(net, "net3@example.com").status_code == 429


def test_the_phone_limit_still_applies(make_harness, smtp):
    hx = _hx(make_harness, smtp, MAX_ACCOUNTS_PER_DEVICE=1)
    with hx.db() as db:
        db.add(User(email="first@example.com", email_canonical="first@example.com", password_hash="x",
                    registered_device_id="phone1"))
        db.commit()
    token, _ = access.issue_token(hx.settings, "phone1", 8)
    c = hx.client()
    c.cookies.set(access.APP_COOKIE, token)
    r = hx.register(c, "second@example.com")
    assert r.status_code == 403 and r.json()["error"]["code"] == "device_limit"
    assert smtp.messages == []


def test_no_mailbox_and_no_bot_means_signup_is_unavailable(make_harness):
    hx = make_harness(REGISTRATION_EMAIL_VERIFICATION=True)  # neither SMTP nor Telegram: no way to deliver a code
    r = hx.register(hx.client(), "nomail@example.com")
    assert r.status_code == 503 and r.json()["error"] == {"code": "registration_unavailable",
                                                          "message": "التسجيل غير متاح مؤقتًا. حاول لاحقًا."}
    with hx.db() as db:
        assert db.scalars(select(SignupPending)).all() == [] and db.scalars(select(User)).all() == []


def test_a_mail_server_that_refuses_hands_the_code_to_the_owner(make_harness):
    """V6 phase 18: the person keeps the code screen; the owner gets the code to send by hand + an alert."""
    hx = _hx(make_harness, None, SMTP_HOST="127.0.0.1", SMTP_PORT=1, SMTP_SECURITY="none", SMTP_FROM="no-reply@dzplay.test",
             SMTP_TIMEOUT=2)
    c = hx.client()
    d = _start(hx, c, "refused@example.com")
    hx.state.bot.wait_idle()
    texts = hx.tg.texts()
    assert sum("تعذّر إرسال رمز التسجيل تلقائيًا" in t for t in texts) == 1
    code = _code_in_text(next(t for t in texts if "refused@example.com" in t))
    assert c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": code}).status_code == 201
    _start(hx, hx.client(), "refused2@example.com")
    hx.state.bot.wait_idle()
    assert sum("تعذّر إرسال رمز التسجيل تلقائيًا" in t for t in hx.tg.texts()) == 1  # the alert: at most every 30 min


def _code_in_text(text: str) -> str:
    return re.search(r"\b([0-9]{6})\b", text).group(1)


def test_with_the_setting_off_accounts_are_made_at_once(make_harness):
    hx = make_harness()  # tests default: REGISTRATION_EMAIL_VERIFICATION off
    c = hx.client()
    r = hx.register(c, "Direct.User+tag@Example.com")
    assert r.status_code == 201
    u = _user(hx, "direct.user+tag@example.com")
    assert u.signup_verified_via is None and u.email_canonical == "direct.user@example.com"
    assert hx.register(hx.client(), "direct.user@example.com").status_code == 409  # the same mailbox


def test_old_accounts_get_their_canonical_address(make_harness):
    from app.migrations import backfill

    hx = make_harness()
    with hx.db() as db:
        db.add(User(email="old.one+x@gmail.com", password_hash="x"))
        db.commit()
    backfill(hx.state.database.engine)
    assert _user(hx, "old.one+x@gmail.com").email_canonical == "oldone@gmail.com"


def test_cleanup_removes_signups_never_confirmed(make_harness, smtp):
    from app.services.cleanup import run_cleanup

    hx = _hx(make_harness, smtp)
    _start(hx, hx.client(), "forgot@example.com")
    clock.advance(hx.settings.REGISTER_CODE_TTL + 5)
    with hx.db() as db:
        assert run_cleanup(db, hx.settings)["signup_pending"] == 1


# ---------------------------------------------------------------------------------------------- the admin signup code


def test_the_admin_signup_code(make_harness, smtp):
    hx = _hx(make_harness, smtp)
    adm = hx.admin()
    status = adm.get("/api/admin/signup-code").json()
    assert status["configured"] is False and status["enabled"] is False  # off by default
    c = hx.client()
    d = _start(hx, c, "guest@example.com")
    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": ADMIN_CODE})
    assert r.json()["error"]["code"] == "code_invalid"  # not set yet

    body = {"signup_code": ADMIN_CODE, "enabled": True, "max_uses": 2}
    assert adm.put("/api/admin/signup-code", json={**body, "code": "000000"}).status_code == 403  # step-up 2FA
    r = adm.put("/api/admin/signup-code", json={**body, "signup_code": "short-code", "code": _step_up(hx)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "code_too_short"
    r = adm.put("/api/admin/signup-code", json={**body, "code": _step_up(hx)})
    assert r.status_code == 200, r.text
    assert r.json()["active"] is True and r.json()["max_uses"] == 2 and "hash" not in r.json()
    with hx.db() as db:
        raw = db.get(AppSetting, "signup.admin_code").value
        assert ADMIN_CODE not in raw and "argon2" in raw  # stored hashed
        log = db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "signup_code_update")).all()
        assert log and all(ADMIN_CODE not in (row.detail or "") for row in log)

    r = c.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": ADMIN_CODE})
    assert r.status_code == 201, r.text
    hx.state.pipeline.wait_idle()
    u = _user(hx, "guest@example.com")
    assert u.signup_verified_via == "admin_code"
    assert any("استُعمل رمز إنشاء الحساب الإداري" in t and "g***@example.com" in t and "1/2" in t for t in hx.tg.texts())
    with hx.db() as db:
        used = db.scalars(select(AdminAuditLog).where(AdminAuditLog.action == "admin_signup_code_used")).all()
        assert len(used) == 1 and used[0].target_id == u.id
    listed = adm.get("/api/admin/access/users", params={"q": "guest@"}).json()["users"]
    assert listed[0]["verified_via"] == "admin_code"
    assert adm.get(f"/api/admin/access/users/{u.id}").json()["user"]["verified_via"] == "admin_code"

    # the second (last) use switches it off
    d2 = _start(hx, hx.client(), "guest2@example.com")
    assert hx.client().post("/api/auth/register/verify", json={"signup_token": d2["signup_token"], "code": ADMIN_CODE}).status_code == 201
    hx.state.pipeline.wait_idle()
    assert "أُوقف تلقائيًا" in hx.tg.texts()[-1]
    st = adm.get("/api/admin/signup-code").json()
    assert st["uses"] == 2 and st["enabled"] is False and st["active"] is False
    d3 = _start(hx, hx.client(), "guest3@example.com")
    r = hx.client().post("/api/auth/register/verify", json={"signup_token": d3["signup_token"], "code": ADMIN_CODE})
    assert r.json()["error"]["code"] == "code_invalid"


def test_the_admin_code_expires_and_its_tries_are_limited(make_harness, smtp):
    hx = _hx(make_harness, smtp, ADMIN_SIGNUP_CODE_TRIES_PER_IP=2)
    adm = hx.admin()
    past = (clock.utcnow() - timedelta(hours=1)).isoformat() + "Z"
    r = adm.put("/api/admin/signup-code", json={"signup_code": ADMIN_CODE, "enabled": True, "expires_at": past,
                                                "code": _step_up(hx)})
    assert r.status_code == 400
    soon = (clock.utcnow() + timedelta(hours=2)).isoformat() + "Z"
    r = adm.put("/api/admin/signup-code", json={"signup_code": ADMIN_CODE, "enabled": True, "expires_at": soon,
                                                "code": _step_up(hx)})
    assert r.status_code == 200 and r.json()["active"] is True
    # strict tries per network: two wrong codes, then even the right one is refused
    net = hx.client()
    d = _start(hx, net, "tryhard@example.com")
    for _ in range(2):
        r = net.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": "not-the-code-at-all"})
        assert r.json()["error"]["code"] == "code_invalid"
    r = net.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": ADMIN_CODE})
    assert r.status_code == 429
    # expired: no longer works (another network)
    clock.advance(3 * 3600)
    other = hx.client()
    d = _start(hx, other, "afterwards@example.com")
    r = other.post("/api/auth/register/verify", json={"signup_token": d["signup_token"], "code": ADMIN_CODE})
    assert r.json()["error"]["code"] == "code_invalid"
    assert adm.get("/api/admin/signup-code").json()["expired"] is True
