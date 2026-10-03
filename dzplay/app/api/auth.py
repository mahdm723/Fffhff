from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Request, Response

from app.api.deps import (
    clear_session_cookie,
    client_context,
    current_user,
    get_state,
    session_token,
    set_session_cookie,
)
from app.api.schemas import (
    ChallengeBody,
    GoogleBody,
    LoginBody,
    RegisterBody,
    ResetCompleteBody,
    ResetRequestBody,
    ResetVerifyBody,
)
from app.config import PRIVACY_VERSION
from app.errors import AppError, rate_limited
from app.security.sessions import create_session, revoke_session
from app.services import auth as auth_service
from app.services import password_reset
from app.services.ideas import own_profile
from app.services.messaging import profile
from app.services.rate_limit import Limit

router = APIRouter(prefix="/api", tags=["auth"])

_GOOGLE_NONCE_COOKIE = "dz_gnonce"
_APK = Path(__file__).resolve().parent.parent.parent / "static" / "download" / "dzplay.apk"


@router.get("/config")
def public_config(request: Request) -> dict:
    s = get_state(request).settings
    return {
        "app_name": s.APP_NAME,
        "antibot_enabled": s.ANTIBOT_ENABLED,
        "google_client_id": s.GOOGLE_CLIENT_ID or None,
        "push_public_key": s.VAPID_PUBLIC_KEY if s.push_enabled else None,
        "max_message_length": s.MAX_MESSAGE_LENGTH,
        "max_post_length": s.MAX_POST_LENGTH,
        "max_comment_length": s.MAX_COMMENT_LENGTH,
        "password_min_length": s.PASSWORD_MIN_LENGTH,
        "links_allowed": s.LINK_POLICY != "reject",
        "android_apk_url": "/download/dzplay.apk" if _APK.is_file() else None,
        "max_reel_comment_length": s.MAX_REEL_COMMENT_LENGTH,
        "reels_prefetch": {"count": s.PREFETCH_COUNT, "ahead": s.PREFETCH_AHEAD, "device_cache_mb": s.DEVICE_MEDIA_CACHE_MB},
        # the bot is enough: without SMTP the admin receives the code in Telegram and sends it by hand
        "password_reset_enabled": get_state(request).bot is not None,
        "reset_code_hours": max(1, s.RESET_CODE_TTL // 3600),
        "reset_max_attempts": s.RESET_MAX_CODE_ATTEMPTS,
        "footer_text": s.FOOTER_TEXT,
    }


@router.post("/auth/challenge")
def challenge(body: ChallengeBody, request: Request) -> dict:
    st = get_state(request)
    ctx = client_context(request)
    if not ctx.trusted:
        decision = st.limiter.check_and_hit([Limit(f"challenge:{ctx.ip_hash}", 30, 60)])
        if not decision.allowed:
            raise rate_limited(decision.retry_after)
    with st.database.session() as db:
        return auth_service.issue_challenge(db, st.settings, ctx, body.purpose)


@router.post("/auth/register", status_code=201)
def register(body: RegisterBody, request: Request, response: Response) -> dict:
    st = get_state(request)
    ctx = client_context(request)
    with st.database.session() as db:
        user = auth_service.register(
            db, st.settings, ctx, email=body.email, password=body.password, password_confirm=body.password_confirm,
            antibot_payload=body.antibot, honeypot=body.website, gender=body.gender, age_confirmed=body.age_confirmed,
        )
        token = create_session(db, st.settings, user)
        result = profile(user)
    set_session_cookie(response, request, token)
    return result


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict:
    st = get_state(request)
    ctx = client_context(request)
    with st.database.session() as db:
        user = auth_service.login(db, st.settings, ctx, email=body.email, password=body.password, antibot_payload=body.antibot)
        token = create_session(db, st.settings, user)
        result = profile(user)
    set_session_cookie(response, request, token)
    return result


# ----------------------------------------------------------------- password recovery


def _reset_enabled(request: Request) -> None:
    st = get_state(request)
    if st.bot is None:
        raise AppError(503, "reset_unavailable", "استعادة الحساب غير مفعّلة حاليًا. تواصل مع الإدارة.")


@router.post("/auth/reset/request")
def reset_request(body: ResetRequestBody, request: Request) -> dict:
    """Same answer whether or not the e-mail exists (no account enumeration)."""
    _reset_enabled(request)
    st = get_state(request)
    ctx = client_context(request)
    with st.database.session() as db:
        result = password_reset.request_reset(db, st.settings, ctx, email=body.email, antibot_payload=body.antibot)
    if result.get("notify"):
        st.bot.run_later(password_reset.notify_admin, st.bot, result["notify"])  # after commit, off the request path
    return {"ok": True, "message": result["message"]}


@router.post("/auth/reset/verify")
def reset_verify(body: ResetVerifyBody, request: Request) -> dict:
    _reset_enabled(request)
    st = get_state(request)
    with st.database.session() as db:
        return password_reset.verify_code(db, st.settings, st.limiter, client_context(request), email=body.email, code=body.code)


@router.post("/auth/reset/complete")
def reset_complete(body: ResetCompleteBody, request: Request, response: Response) -> dict:
    _reset_enabled(request)
    st = get_state(request)
    with st.database.session() as db:
        user = password_reset.complete(db, st.settings, client_context(request), reset_token=body.reset_token,
                                       password=body.password, password_confirm=body.password_confirm,
                                       antibot_payload=body.antibot)
        token = create_session(db, st.settings, user)  # a fresh session; all older ones were revoked
        result = profile(user)
    set_session_cookie(response, request, token)
    return result


@router.get("/auth/google/nonce")
def google_nonce(request: Request, response: Response) -> dict:
    s = get_state(request).settings
    nonce = secrets.token_urlsafe(24)
    response.set_cookie(_GOOGLE_NONCE_COOKIE, nonce, max_age=600, httponly=True, secure=bool(s.COOKIE_SECURE),
                        samesite="strict", path="/api/auth/google")
    return {"nonce": nonce}


@router.post("/auth/google")
def google(body: GoogleBody, request: Request, response: Response) -> dict:
    st = get_state(request)
    ctx = client_context(request)
    nonce = request.cookies.get(_GOOGLE_NONCE_COOKIE)
    with st.database.session() as db:
        user = auth_service.google_login(db, st.settings, ctx, credential=body.credential, expected_nonce=nonce)
        token = create_session(db, st.settings, user)
        result = profile(user)
    response.delete_cookie(_GOOGLE_NONCE_COOKIE, path="/api/auth/google")
    set_session_cookie(response, request, token)
    return result


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        revoke_session(db, session_token(request))
    clear_session_cookie(response, request)
    return {"ok": True}


@router.get("/me")
def me(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return own_profile(db, current_user(request, db), st.settings)


@router.post("/me/privacy-ack")
def privacy_ack(request: Request) -> dict:
    """The user has read the current privacy notice."""
    st = get_state(request)
    with st.database.session() as db:
        user = current_user(request, db)
        user.privacy_ack_version = PRIVACY_VERSION
        return {"ok": True, "version": PRIVACY_VERSION}
