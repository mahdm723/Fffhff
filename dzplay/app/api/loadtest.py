"""V6 phase 21: the load-test endpoints — staging only (app/security/loadtest.py). Everywhere else they do not exist
(404), and the settings refuse LOADTEST_MODE outside staging."""

from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Query, Request, Response
from pydantic import Field
from sqlalchemy import select

from app import clock
from app.api.deps import get_state
from app.api.schemas import _Body
from app.errors import not_found
from app.models import AppDevice
from app.security import loadtest

router = APIRouter(tags=["loadtest"], include_in_schema=False)


class AttestBody(_Body):
    device: str = Field(min_length=1, max_length=40)
    version_code: int = Field(ge=0, le=10_000_000)


def _guard(request: Request) -> None:
    if not loadtest.authorized(get_state(request).settings, request.headers):
        raise not_found()


@router.get("/healthz/env")
def env(request: Request) -> dict:
    st = get_state(request)
    if not loadtest.enabled(st.settings):
        raise not_found()
    return {"env": st.settings.ENV, "loadtest": True}


@router.get("/healthz/metrics")
def internal_metrics(request: Request, minutes: int = Query(default=1, ge=1, le=180)) -> dict:
    """The load-test collector's view (the same as the panel's «صحة النظام»), every few seconds."""
    from app.services import health

    _guard(request)
    return health.report(get_state(request), minutes)


@router.post("/api/app/loadtest-attest")
def attest(body: AttestBody, request: Request, response: Response) -> dict:
    """A synthetic phone (same cookie, same device rows as a real attested one; its details say locked/verified)."""
    from app.api.app_access import _respond

    _guard(request)
    st = get_state(request)
    key_id = hashlib.sha256(f"loadtest:{body.device}".encode()).hexdigest()
    now = clock.utcnow()
    with st.database.session() as db:
        dev = db.scalar(select(AppDevice).where(AppDevice.key_id == key_id))
        if dev is None:
            dev = AppDevice(key_id=key_id, public_key="", security_level="tee", created_at=now,
                            details=json.dumps({"level": "tee", "boot": "verified", "locked": True, "loadtest": True}))
            db.add(dev)
        dev.version_code, dev.last_seen_at = body.version_code, now
        db.flush()
        device_id = dev.id
    return _respond(request, response, device_id, body.version_code)
