"""Admin panel API — mounted under the secret ADMIN_PATH (see app.main).

Access: an admin account session (password + TOTP), cookie scoped to
ADMIN_PATH, optional IP allowlist. Every action, and every view of private
content (report evidence, flags, conversations), is written to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request, Response
from pydantic import Field

from app.api.deps import client_context, get_state
from app.api.schemas import ResolveReportBody, UserStatusBody, _Body
from app.errors import AppError
from app.models import AdminUser
from app.services import admin as admin_service
from app.services import admin_auth, audit
from app.services.auth import ClientContext
from app.services.cleanup import run_cleanup

router = APIRouter(prefix="/api/admin", tags=["admin"])


class AdminLoginBody(_Body):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    code: str = Field(max_length=10)


@dataclass
class AdminContext:
    admin: AdminUser
    client: ClientContext

    @property
    def actor(self) -> str:
        return self.admin.username

    @property
    def ip_ref(self) -> str:
        return self.client.ip_hash[:12]


def _cookie_path(request: Request) -> str:
    return get_state(request).settings.ADMIN_PATH


def require_admin(request: Request) -> AdminContext:
    st = get_state(request)
    token = request.cookies.get(admin_auth.COOKIE_NAME)
    with st.database.session() as db:
        admin = admin_auth.resolve(db, st.settings, token)
        if admin is None:
            raise AppError(401, "unauthenticated", "انتهت الجلسة. سجّل الدخول من جديد.")
        db.expunge(admin)
    return AdminContext(admin, client_context(request))


def _record(db, ac: AdminContext, action: str, **kw) -> None:
    audit.record(db, ac.actor, action, ip_ref=ac.ip_ref, **kw)


# ----------------------------------------------------------------- session


@router.post("/login")
def login(body: AdminLoginBody, request: Request, response: Response) -> dict:
    st = get_state(request)
    ctx = client_context(request)
    with st.database.session() as db:
        admin, token = admin_auth.login(db, st.settings, ctx, body.username, body.password, body.code)
        audit.record(db, admin.username, "login", ip_ref=ctx.ip_hash[:12])
        name = admin.username
    response.set_cookie(admin_auth.COOKIE_NAME, token, max_age=st.settings.ADMIN_SESSION_TTL, httponly=True,
                        secure=bool(st.settings.COOKIE_SECURE), samesite="strict", path=_cookie_path(request))
    return {"username": name}


@router.post("/logout")
def logout(request: Request, response: Response) -> dict:
    st = get_state(request)
    token = request.cookies.get(admin_auth.COOKIE_NAME)
    with st.database.session() as db:
        admin = admin_auth.resolve(db, st.settings, token)
        if admin is not None:
            audit.record(db, admin.username, "logout", ip_ref=client_context(request).ip_hash[:12])
        admin_auth.logout(db, token)
    response.delete_cookie(admin_auth.COOKIE_NAME, path=_cookie_path(request), secure=bool(st.settings.COOKIE_SECURE),
                           httponly=True, samesite="strict")
    return {"ok": True}


@router.get("/session")
def session(ac: AdminContext = Depends(require_admin)) -> dict:
    return {"username": ac.admin.username}


# ----------------------------------------------------------------- overview


@router.get("/stats")
def stats(request: Request, ac: AdminContext = Depends(require_admin)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return admin_service.stats(db, st.settings)


@router.get("/activity")
def activity(request: Request, days: int = Query(default=14, ge=1, le=30), tz: int = Query(default=0, ge=-840, le=840),
             ac: AdminContext = Depends(require_admin)) -> dict:
    """Daily counts for the dashboard chart. `tz` = browser getTimezoneOffset() in minutes."""
    with get_state(request).database.session() as db:
        return admin_service.activity(db, days, tz)


# ----------------------------------------------------------------- reports + flags (private content → audited)


@router.get("/reports")
def reports(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200),
            ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        items = admin_service.list_reports(db, status, limit)
        _record(db, ac, "view_reports", detail=f"status={status} count={len(items)}")
        return {"reports": items}


@router.post("/reports/{report_id}/resolve")
def resolve(report_id: str, body: ResolveReportBody, request: Request, ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_service.resolve_report(db, report_id, body.action)
        _record(db, ac, f"report_{body.action}", target_type="report", target_id=report_id)
        return result


@router.get("/flags")
def flags(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200),
          ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        items = admin_service.list_flags(db, status, limit)
        _record(db, ac, "view_flags", detail=f"status={status} count={len(items)}")
        return {"flags": items}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: str, body: ResolveReportBody, request: Request, ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_service.resolve_flag(db, flag_id, body.action)
        _record(db, ac, f"flag_{body.action}", target_type="flag", target_id=flag_id)
        return result


@router.get("/users/{user_ref}/conversations")
def user_conversations(user_ref: str, request: Request, reason: str = Query(default="", max_length=255),
                       ac: AdminContext = Depends(require_admin)) -> dict:
    """Stored conversations of a reported/flagged user. A reason is required; every view is audited."""
    if len(reason.strip()) < 3:
        raise AppError(400, "reason_required", "اكتب سبب الاطلاع على المحادثات.")
    with get_state(request).database.session() as db:
        result = admin_service.user_conversations(db, user_ref)
        _record(db, ac, "view_conversations", target_type="user", target_id=user_ref, reason=reason.strip(),
                detail=f"{len(result['conversations'])} conversations")
        return result


# ----------------------------------------------------------------- users, security, maintenance


@router.post("/users/{user_ref}/status")
def user_status(user_ref: str, body: UserStatusBody, request: Request, ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_service.set_user_status(db, user_ref, body.status)
        _record(db, ac, f"user_{body.status}", target_type="user", target_id=user_ref)
        return result


@router.get("/security-events")
def events(request: Request, type: str | None = Query(default=None, max_length=48), limit: int = Query(default=100, le=500),
           ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        return {"events": admin_service.security_events(db, type, limit)}


@router.post("/cleanup")
def cleanup(request: Request, ac: AdminContext = Depends(require_admin)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        deleted = run_cleanup(db, st.settings, st.media)
        _record(db, ac, "cleanup", detail=str(sum(deleted.values())))
        return {"deleted": deleted}


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(default=100, le=500), before: int | None = Query(default=None),
              action: str | None = Query(default=None, max_length=64), ac: AdminContext = Depends(require_admin)) -> dict:
    with get_state(request).database.session() as db:
        return {"entries": audit.list_entries(db, limit, before, action), "chain": audit.verify_chain(db)}
