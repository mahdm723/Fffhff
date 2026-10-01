"""Internal admin API. Disabled unless ADMIN_API_TOKEN is set.

Usage: curl -H "Authorization: Bearer $ADMIN_API_TOKEN" https://host/api/admin/stats
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request

from app.api.deps import get_state
from app.api.schemas import ResolveReportBody, UserStatusBody
from app.errors import AppError
from app.security.crypto import constant_time_equals
from app.services import admin as admin_service
from app.services.cleanup import run_cleanup


def require_admin(request: Request) -> None:
    token = get_state(request).settings.ADMIN_API_TOKEN
    if not token:
        raise AppError(404, "not_found", "غير موجود.")
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer ") or not constant_time_equals(header[7:], token):
        raise AppError(401, "unauthenticated", "unauthorized")


router = APIRouter(prefix="/api/admin", tags=["admin"], dependencies=[Depends(require_admin)])


@router.get("/stats")
def stats(request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.stats(db)


@router.get("/reports")
def reports(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200)) -> dict:
    with get_state(request).database.session() as db:
        return {"reports": admin_service.list_reports(db, status, limit)}


@router.post("/reports/{report_id}/resolve")
def resolve(report_id: str, body: ResolveReportBody, request: Request) -> dict:
    with get_state(request).database.session() as db:
        return admin_service.resolve_report(db, report_id, body.action)


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
