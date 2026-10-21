"""V6 phase 21: load tests, in staging only.

Settings refuse LOADTEST_MODE unless ENV=staging (and a 32+ character LOADTEST_TOKEN), so none of this can run in
production; every function here checks it again at the time of the request. With the token in X-Loadtest-Token:

* X-Loadtest-IP — the address of one virtual user. All the load comes from one generator, so without it every
  per-network limit would fire at once; with it each virtual user has its own address and the limits (and their
  cost) stay as in real life.
* POST /api/app/loadtest-attest — the normal signed dz_app cookie for a synthetic phone, without Key Attestation
  (the Android path of the app, gate and device limits included, without a real phone).
* GET /healthz/env — "this is staging": the load-test scripts refuse any target that does not say so.
"""

from __future__ import annotations

import hmac
import ipaddress

from app.config import Settings

HEADER_TOKEN = "x-loadtest-token"
HEADER_IP = "x-loadtest-ip"


def enabled(settings: Settings) -> bool:
    return bool(settings.LOADTEST_MODE and settings.ENV == "staging" and len(settings.LOADTEST_TOKEN) >= 32)


def authorized(settings: Settings, headers) -> bool:
    if not enabled(settings):
        return False
    given = headers.get(HEADER_TOKEN) or ""
    return hmac.compare_digest(given.encode(), settings.LOADTEST_TOKEN.encode())


def synthetic_ip(headers, settings: Settings) -> str | None:
    """The virtual user's address (only with the token, in staging)."""
    if not authorized(settings, headers):
        return None
    try:
        return str(ipaddress.ip_address((headers.get(HEADER_IP) or "").strip()))
    except ValueError:
        return None
