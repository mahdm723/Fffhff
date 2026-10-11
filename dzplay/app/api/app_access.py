"""V6 phase 14: the Android app proves itself (Key Attestation) and receives its access cookie (dz_app).

    GET  /api/app/challenge          → {id, challenge}                       (one use, 5 minutes)
    POST /api/app/attest  {challenge_id, chain[], version_code}  → {token, max_age}   (first launch / new key)
    POST /api/app/proof   {challenge_id, key_id, signature, version_code} → {token, max_age}  (later launches)

Open to everyone (the gate lets /api/app/* through); rate limited per IP. The token is also set as an HttpOnly
cookie; the app copies it into its WebView (its own HTTP client does not share the WebView's cookies).
"""

from __future__ import annotations

import base64
import binascii
import logging

from fastapi import APIRouter, Request, Response
from pydantic import Field

from app import clock
from app.api.deps import client_context, get_state
from app.api.schemas import _Body
from app.errors import AppError, rate_limited
from app.i18n import N_
from app.security import attestation
from app.services import access
from app.services.rate_limit import Limit

log = logging.getLogger("dzplay.app_access")
router = APIRouter(prefix="/api/app", tags=["app-access"])


class AttestBody(_Body):
    challenge_id: str = Field(max_length=200)
    chain: list[str] = Field(min_length=2, max_length=10)
    version_code: int = Field(ge=0, le=10_000_000)


class ProofBody(_Body):
    challenge_id: str = Field(max_length=200)
    key_id: str = Field(max_length=64)
    signature: str = Field(max_length=400)
    version_code: int = Field(ge=0, le=10_000_000)


def _limit(request: Request) -> None:
    st = get_state(request)
    ctx = client_context(request)
    decision = st.limiter.check_and_hit([Limit(f"app_attest:{ctx.ip_hash}", 60, 3600)])
    if not decision.allowed:
        raise rate_limited(decision.retry_after)


def _b64(text: str, limit: int) -> bytes:
    try:
        raw = base64.b64decode(text, validate=True) if "-" not in text and "_" not in text else base64.urlsafe_b64decode(text + "==")
    except (binascii.Error, ValueError):
        raise AppError(400, "invalid_input", N_("طلب غير صالح.")) from None
    if not raw or len(raw) > limit:
        raise AppError(400, "invalid_input", N_("طلب غير صالح."))
    return raw


def _respond(request: Request, response: Response, device_id: str, version_code: int) -> dict:
    st = get_state(request)
    token, max_age = access.issue_token(st.settings, device_id, version_code)
    response.set_cookie(access.APP_COOKIE, token, max_age=max_age, path="/", secure=st.settings.COOKIE_SECURE,
                        httponly=True, samesite="lax")
    return {"token": token, "max_age": max_age, "cookie": access.APP_COOKIE}


@router.get("/challenge")
def challenge(request: Request) -> dict:
    _limit(request)
    return access.new_challenge(get_state(request).settings)


@router.post("/attest")
def attest(body: AttestBody, request: Request, response: Response) -> dict:
    _limit(request)
    st = get_state(request)
    nonce = access.use_challenge(st.settings, st.limiter, body.challenge_id)
    chain = [_b64(c, 16 * 1024) for c in body.chain]
    try:
        result = attestation.verify_chain(chain, challenge=nonce, package=st.settings.ANDROID_APP_PACKAGE,
                                          cert_sha256=[bytes.fromhex(f.replace(":", "")) for f in st.settings.android_cert_fingerprints],
                                          roots=st.trusted_roots(), revoked=st.revoked_serials())
    except attestation.AttestationError as exc:
        log.info("attestation refused: %s", exc)
        raise AppError(403, "attestation_failed", N_("تعذّر التحقق من التطبيق على هذا الجهاز.")) from None
    version = result.package_version or body.version_code  # the installed version, as the phone's hardware says
    with st.database.session() as db:
        dev = access.register_device(db, result, version)
        device_id = dev.id
    log.info("app attested: device %s, %s, v%s", device_id, result.description.security_level, version)
    return _respond(request, response, device_id, version)


@router.post("/proof")
def proof(body: ProofBody, request: Request, response: Response) -> dict:
    _limit(request)
    st = get_state(request)
    nonce = access.use_challenge(st.settings, st.limiter, body.challenge_id)
    signature = _b64(body.signature, 200)
    with st.database.session() as db:
        dev = access.device_for_proof(db, body.key_id)
        if dev is None:  # unknown key (the app makes a new one and attests again)
            raise AppError(404, "unknown_key", N_("تعذّر التحقق من التطبيق على هذا الجهاز."))
        if not attestation.verify_proof(base64.b64decode(dev.public_key), nonce, signature):
            raise AppError(403, "attestation_failed", N_("تعذّر التحقق من التطبيق على هذا الجهاز."))
        dev.version_code = body.version_code
        dev.last_seen_at = clock.utcnow()
        device_id = dev.id
    return _respond(request, response, device_id, body.version_code)
