"""Internal admin API. Disabled unless ADMIN_API_TOKEN is set.

Usage: curl -H "Authorization: Bearer $ADMIN_API_TOKEN" https://host/api/admin/stats
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import client_context, get_state
from app.api.schemas import ResolveReportBody, UserStatusBody
from app.errors import AppError
from app.security.crypto import constant_time_equals
from app.services import admin as admin_service
from app.services.auth import log_event
from app.services.cleanup import run_cleanup

# Brute-force protection for the admin token: after this many wrong tokens from
# one network within the window, that network is refused before the token is
# even compared (so further guesses reveal nothing). Stored as security events,
# so it works the same with or without Redis and survives restarts.
ADMIN_MAX_FAILURES = 10
ADMIN_FAILURE_WINDOW = 10 * 60


def require_admin(request: Request) -> None:
    st = get_state(request)
    token = st.settings.ADMIN_API_TOKEN
    if not token:
        raise AppError(404, "not_found", "غير موجود.")
    ctx = client_context(request)
    with st.database.session() as db:
        retry = admin_service.admin_lockout(db, ctx.ip_hash, ADMIN_MAX_FAILURES, ADMIN_FAILURE_WINDOW)
        if retry:
            raise AppError(429, "rate_limited", "محاولات كثيرة. حاول لاحقًا.", retry)
        header = request.headers.get("authorization", "")
        if not header.startswith("Bearer ") or not constant_time_equals(header[7:], token):
            log_event(db, "admin_auth_failed", ctx)
            db.commit()
            raise AppError(401, "unauthenticated", "unauthorized")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/stats")
def stats(request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.stats(db)


@router.get("/activity")
def activity(request: Request, days: int = Query(default=14, ge=1, le=30), tz: int = Query(default=0, ge=-840, le=840)) -> dict:
    """Daily counts for the dashboard chart. `tz` = browser getTimezoneOffset() in minutes."""
    with get_state(request).database.session() as db:
        return admin_service.activity(db, days, tz)


@router.get("/reports")
def reports(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200)) -> dict:
    with get_state(request).database.session() as db:
        return {"reports": admin_service.list_reports(db, status, limit)}


@router.post("/reports/{report_id}/resolve")
def resolve(report_id: str, body: ResolveReportBody, request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.resolve_report(db, report_id, body.action)


@router.get("/flags")
def flags(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200)) -> dict:
    with get_state(request).database.session() as db:
        return {"flags": admin_service.list_flags(db, status, limit)}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: str, body: ResolveReportBody, request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.resolve_flag(db, flag_id, body.action)


@router.get("/users/{user_ref}/conversations")
def user_conversations(user_ref: str, request: Request) -> dict:
    """Stored conversations of a reported/flagged user. Every call is logged."""
    with get_state(request).database.session() as db:
        return admin_service.user_conversations(db, user_ref)


@router.post("/users/{user_ref}/status")
def user_status(user_ref: str, body: UserStatusBody, request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.set_user_status(db, user_ref, body.status)


@router.get("/security-events")
def events(request: Request, type: str | None = Query(default=None, max_length=48), limit: int = Query(default=100, le=500)) -> dict:
    with get_state(request).database.session() as db:
        return {"events": admin_service.security_events(db, type, limit)}


@router.post("/cleanup")
def cleanup(request: Request) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"deleted": run_cleanup(db, st.settings)}
