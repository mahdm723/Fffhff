"""V6 phase 21: the load-test mode exists in staging only. It cannot be switched on in production (or development):
the settings refuse it, so the app cannot even start; and every use checks again at request time."""

from __future__ import annotations

import pytest

from app.config import Settings

TOKEN = "lt-" + "x" * 40
ANDROID_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8; wv) AppleWebKit/537.36 DZPLAYApp/3.2.0"


@pytest.mark.parametrize("env", ["production", "development", "test", "Staging", ""])
def test_impossible_outside_staging(env):
    with pytest.raises(ValueError, match="LOADTEST_MODE"):
        Settings(_env_file=None, ENV=env, SECRET_KEY="k" * 40, LOADTEST_MODE=True, LOADTEST_TOKEN=TOKEN)


def test_staging_needs_a_long_token_and_behaves_like_production():
    with pytest.raises(ValueError, match="LOADTEST_TOKEN"):
        Settings(_env_file=None, ENV="staging", SECRET_KEY="k" * 40, LOADTEST_MODE=True, LOADTEST_TOKEN="short")
    with pytest.raises(ValueError, match="SECRET_KEY"):
        Settings(_env_file=None, ENV="staging")
    s = Settings(_env_file=None, ENV="staging", SECRET_KEY="k" * 40, LOADTEST_MODE=True, LOADTEST_TOKEN=TOKEN)
    assert s.COOKIE_SECURE is True  # as in production


def test_not_a_panel_setting_and_not_in_env_example():
    from pathlib import Path

    from app.services import tunables

    assert not [t for t in tunables.REGISTRY if t.key.startswith("LOADTEST")]
    assert "LOADTEST" not in (Path(__file__).resolve().parent.parent / ".env.example").read_text()


@pytest.fixture
def staging(make_harness):
    return make_harness(ENV="staging", LOADTEST_MODE=True, LOADTEST_TOKEN=TOKEN, COOKIE_SECURE=False)


def test_synthetic_address_per_virtual_user(staging):
    c = staging.client()
    lt = {"X-Loadtest-Token": TOKEN}
    assert c.get("/healthz/ip", headers={**lt, "X-Loadtest-IP": "100.64.3.7"}).json()["ip"] == "100.64.3.7"
    assert c.get("/healthz/ip", headers={**lt, "X-Loadtest-IP": "2001:db8::9"}).json()["ip"] == "2001:db8::9"
    # wrong token, no token, or not an address: the real address
    for headers in ({"X-Loadtest-Token": "wrong", "X-Loadtest-IP": "100.64.3.7"}, {"X-Loadtest-IP": "100.64.3.7"},
                    {**lt, "X-Loadtest-IP": "not-an-ip"}):
        assert c.get("/healthz/ip", headers=headers).json()["ip"] == c.ip
    assert c.get("/healthz/env").json() == {"env": "staging", "loadtest": True}


def test_per_network_limits_apply_per_virtual_user(staging):
    """One generator, many users: each virtual user's address has its own limits (and the limits still work)."""
    staging.settings.MAX_ACCOUNTS_PER_IP = 1
    lt = {"X-Loadtest-Token": TOKEN}
    for n in range(3):
        c = staging.client()
        c.headers.update({**lt, "X-Loadtest-IP": f"100.64.0.{n + 1}"})
        assert staging.register(c, f"vu{n}@example.com").status_code == 201
    again = staging.client()
    again.headers.update({**lt, "X-Loadtest-IP": "100.64.0.1"})
    assert staging.register(again, "vu-again@example.com").status_code == 429


def test_synthetic_phone_passes_the_android_gate(staging):
    c = staging.client()
    c.headers["user-agent"] = ANDROID_UA
    assert c.get("/api/me").json()["error"]["code"] == "app_required"  # an Android app without proof
    assert c.post("/api/app/loadtest-attest", json={"device": "vu-1", "version_code": 30200}).status_code == 404
    r = c.post("/api/app/loadtest-attest", json={"device": "vu-1", "version_code": 30200},
               headers={"X-Loadtest-Token": TOKEN})
    assert r.status_code == 200 and r.json()["cookie"] == "dz_app"
    assert c.get("/api/me").status_code == 401  # through the gate: only the session is missing
    # the same synthetic phone twice = one device row (locked, verified boot: not «suspicious»)
    c.post("/api/app/loadtest-attest", json={"device": "vu-1", "version_code": 30200}, headers={"X-Loadtest-Token": TOKEN})
    from sqlalchemy import func, select

    from app.models import AppDevice

    with staging.db() as db:
        assert db.scalar(select(func.count()).select_from(AppDevice)) == 1


def test_everything_is_off_outside_staging(make_harness):
    hx = make_harness()  # ENV=test, no load-test mode
    c = hx.client()
    lt = {"X-Loadtest-Token": TOKEN, "X-Loadtest-IP": "100.64.3.7"}
    assert c.get("/healthz/ip", headers=lt).json()["ip"] == c.ip
    assert c.get("/healthz/env").status_code == 404
    assert c.post("/api/app/loadtest-attest", json={"device": "x", "version_code": 1}, headers=lt).status_code == 404


def test_checked_again_at_request_time(staging):
    """Even a running app whose settings were changed afterwards stops honouring it at once."""
    c = staging.client()
    staging.settings.ENV = "production"
    lt = {"X-Loadtest-Token": TOKEN, "X-Loadtest-IP": "100.64.3.7"}
    assert c.get("/healthz/ip", headers=lt).json()["ip"] == c.ip
    assert c.get("/healthz/env").status_code == 404
    assert c.post("/api/app/loadtest-attest", json={"device": "x", "version_code": 1}, headers=lt).status_code == 404
