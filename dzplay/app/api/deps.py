from __future__ import annotations

from fastapi import Request, Response
from sqlalchemy.orm import Session

from app.errors import AppError
from app.models import User
from app.security.net import client_ip
from app.security.sessions import resolve_session
from app.services.auth import ClientContext, make_context
from app.state import AppState


def get_state(request: Request) -> AppState:
    return request.app.state.dz


def client_context(request: Request) -> ClientContext:
    st = get_state(request)
    return make_context(st.settings, client_ip(request, st.settings))


def session_token(request: Request) -> str | None:
    return request.cookies.get(get_state(request).settings.SESSION_COOKIE_NAME)


def current_user(request: Request, db: Session) -> User:
    st = get_state(request)
    token = session_token(request)
    user = resolve_session(db, st.settings, token)
    if user is None:
        raise AppError(401, "unauthenticated", "يرجى تسجيل الدخول.")
    from app.services import media_items, media_urls

    media_items.bind_request(st.settings, media_urls.session_key(token))  # media URLs in this response: this session only
    return user


def set_session_cookie(response: Response, request: Request, token: str) -> None:
    s = get_state(request).settings
    response.set_cookie(
        s.SESSION_COOKIE_NAME, token, max_age=s.SESSION_DURATION, httponly=True,
        secure=bool(s.COOKIE_SECURE), samesite="strict", path="/",
    )


def clear_session_cookie(response: Response, request: Request) -> None:
    s = get_state(request).settings
    response.delete_cookie(s.SESSION_COOKIE_NAME, path="/", secure=bool(s.COOKIE_SECURE), httponly=True, samesite="strict")
