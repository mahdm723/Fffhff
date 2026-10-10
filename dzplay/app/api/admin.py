"""Admin panel API — mounted under the secret ADMIN_PATH (see app.main).

Access: an admin account session (password + TOTP), cookie scoped to
ADMIN_PATH, optional IP allowlist. Every action, and every view of private
content (report evidence, flags, conversations), is written to the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import client_context, get_state
from app.api.schemas import ResolveReportBody, UserStatusBody, _Body
from app.errors import AppError, rate_limited
from app.models import AdminUser
from app.services import admin as admin_service
from app.services import admin_access, admin_auth, admin_content, audit, engagement, runtime_config
from app.services.media import ASSET_ID, CONTENT_TYPES, VARIANTS, MediaError
from app.services.messaging import Effects
from app.services.rate_limit import Limit
from app.services.telegram import TelegramError
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


def _throttle(st, key: str, per_minute: int) -> None:
    decision = st.limiter.check_and_hit([Limit(key, per_minute, 60)])
    if not decision.allowed:
        raise rate_limited(decision.retry_after, "طلبات كثيرة. انتظر قليلًا.")


def require_admin(request: Request) -> AdminContext:
    st = get_state(request)
    ctx = client_context(request)
    token = request.cookies.get(admin_auth.COOKIE_NAME)
    with st.database.session() as db:
        admin = admin_auth.resolve(db, st.settings, token)
        if admin is None:
            _throttle(st, f"admin_anon:{ctx.ip_hash}", st.settings.ADMIN_API_ANON_PER_MINUTE)
            raise AppError(401, "unauthenticated", "انتهت الجلسة. سجّل الدخول من جديد.")
        db.expunge(admin)
    _throttle(st, f"admin_api:{admin.id}", st.settings.ADMIN_API_PER_MINUTE)
    return AdminContext(admin, ctx)


def require_role(*roles: str):
    """Route dependency: an admin session whose role is one of `roles`."""

    def dependency(request: Request) -> AdminContext:
        ac = require_admin(request)
        if admin_auth.role_of(ac.admin) not in roles:
            raise AppError(403, "forbidden", "ليست لديك صلاحية لهذا الإجراء.")
        return ac

    return dependency


SUPER_ADMIN = require_role("super_admin")


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
        name, role = admin.username, admin_auth.role_of(admin)
    response.set_cookie(admin_auth.COOKIE_NAME, token, max_age=st.settings.ADMIN_SESSION_TTL, httponly=True,
                        secure=bool(st.settings.COOKIE_SECURE), samesite="strict", path=_cookie_path(request))
    return {"username": name, "role": role}


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
    return {"username": ac.admin.username, "role": admin_auth.role_of(ac.admin)}


# ----------------------------------------------------------------- overview


@router.get("/stats")
def stats(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return admin_service.stats(db, st.settings, st.media)


@router.get("/activity")
def activity(request: Request, days: int = Query(default=14, ge=1, le=30), tz: int = Query(default=0, ge=-840, le=840),
             ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """Daily counts for the dashboard chart. `tz` = browser getTimezoneOffset() in minutes."""
    with get_state(request).database.session() as db:
        return admin_service.activity(db, days, tz)


# ----------------------------------------------------------------- reports + flags (private content → audited)


@router.get("/reports")
def reports(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200),
            ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        items = admin_service.list_reports(db, status, limit)
        _record(db, ac, "view_reports", detail=f"status={status} count={len(items)}")
        return {"reports": items}


@router.post("/reports/{report_id}/resolve")
def resolve(report_id: str, body: ResolveReportBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = admin_service.resolve_report(db, report_id, body.action, effects, st.settings)
        _record(db, ac, f"report_{body.action}", target_type="report", target_id=report_id)
    st.dispatch(effects)
    return result


@router.get("/flags")
def flags(request: Request, status: str = Query(default="open", max_length=16), limit: int = Query(default=50, le=200),
          ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        items = admin_service.list_flags(db, status, limit)
        _record(db, ac, "view_flags", detail=f"status={status} count={len(items)}")
        return {"flags": items}


@router.post("/flags/{flag_id}/resolve")
def resolve_flag(flag_id: str, body: ResolveReportBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_service.resolve_flag(db, flag_id, body.action)
        _record(db, ac, f"flag_{body.action}", target_type="flag", target_id=flag_id)
        return result


@router.get("/users/{user_ref}/conversations")
def user_conversations(user_ref: str, request: Request, reason: str = Query(default="", max_length=255),
                       ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    """Stored conversations of a user (full access, privacy policy v3). Every view is audited; reason optional."""
    with get_state(request).database.session() as db:
        result = admin_service.user_conversations(db, user_ref)
        _record(db, ac, "view_conversations", target_type="user", target_id=user_ref, reason=reason.strip() or None,
                detail=f"{len(result['conversations'])} conversations")
        return result


# ----------------------------------------------------------------- users, security, maintenance


@router.post("/users/{user_ref}/status")
def user_status(user_ref: str, body: UserStatusBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = admin_service.set_user_status(db, user_ref, body.status, effects, st.settings)
        _record(db, ac, f"user_{body.status}", target_type="user", target_id=user_ref)
    st.dispatch(effects)
    return result


@router.get("/security-events")
def events(request: Request, type: str | None = Query(default=None, max_length=48), limit: int = Query(default=100, le=500),
           ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"events": admin_service.security_events(db, type, limit)}


@router.post("/cleanup")
def cleanup(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        deleted = run_cleanup(db, st.settings, st.media)
        _record(db, ac, "cleanup", detail=str(sum(deleted.values())))
        return {"deleted": deleted}


@router.get("/audit")
def audit_log(request: Request, limit: int = Query(default=100, le=500), before: int | None = Query(default=None),
              action: str | None = Query(default=None, max_length=64), ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"entries": audit.list_entries(db, limit, before, action), "chain": audit.verify_chain(db)}


# ----------------------------------------------------------------- content: Ideas, official comments, library


class OfficialCommentBody(_Body):
    target: str = Field(max_length=8)  # idea
    target_id: str = Field(max_length=32)
    text: str | None = Field(default=None, max_length=4000)
    library_id: str | None = Field(default=None, max_length=32)


class LibraryBody(_Body):
    category: str = Field(max_length=32)
    text: str = Field(max_length=4000)


@router.get("/media/{asset_id}/{variant}")
async def admin_media(asset_id: str, variant: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)):
    """Previews for the panel (admin session instead of a user-bound signature)."""
    st = get_state(request)
    if not ASSET_ID.match(asset_id) or variant not in CONTENT_TYPES:
        raise AppError(404, "not_found", "غير موجود.")

    def locate():
        with st.database.session() as db:
            asset = admin_content.asset_for_admin(db, asset_id)
            if variant not in VARIANTS.get(asset.kind, ()):
                raise AppError(404, "not_found", "غير موجود.")
            try:
                return st.media.ensure(db, asset, variant)
            except (MediaError, TelegramError):
                raise AppError(503, "media_unavailable", "تعذّر تحميل الملف الآن.") from None

    path = await run_in_threadpool(locate)
    return FileResponse(path, media_type=CONTENT_TYPES[variant], headers={"Cache-Control": "no-store"})


@router.get("/ideas")
def ideas_list(request: Request, limit: int = Query(default=30, le=100), before: str | None = Query(default=None, max_length=40),
               ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return admin_content.ideas_list(db, limit, before)


@router.post("/official-comment", status_code=201)
def official_comment(body: OfficialCommentBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = admin_content.official_comment(db, st.settings, target=body.target, target_id=body.target_id,
                                                text=body.text, library_id=body.library_id, effects=effects)
        _record(db, ac, "official_comment", target_type=body.target, target_id=body.target_id,
                detail=f"library={body.library_id}" if body.library_id else None)
    st.dispatch(effects)
    return result


@router.get("/library")
def library(request: Request, q: str = Query(default="", max_length=200), category: str = Query(default="", max_length=32),
            ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return engagement.library(db, q, category)


@router.post("/library", status_code=201)
def library_add(body: LibraryBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        item = engagement.item_save(db, st.settings, None, body.category, body.text)
        _record(db, ac, "library_add", target_type="library", target_id=item["id"])
        return item


@router.put("/library/{item_id}")
def library_edit(item_id: str, body: LibraryBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        item = engagement.item_save(db, st.settings, item_id, body.category, body.text)
        _record(db, ac, "library_edit", target_type="library", target_id=item_id)
        return item


@router.delete("/library/{item_id}")
def library_delete(item_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        engagement.item_delete(db, item_id)
        _record(db, ac, "library_delete", target_type="library", target_id=item_id)
    return {"ok": True}


class ImportBody(_Body):
    category: str = Field(max_length=32)
    text: str = Field(max_length=60_000)  # API bodies are capped at 64 KB (≈ 1,000 comments per import)


class CategoryBody(_Body):
    name: str = Field(max_length=64)


@router.post("/library/import")
def library_import(body: ImportBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        result = engagement.bulk_import(db, st.settings, body.category, body.text)
        _record(db, ac, "library_import", target_type="category", target_id=body.category, detail=str(result))
        return result


@router.get("/library/categories")
def categories(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"categories": engagement.categories(db)}


@router.post("/library/categories", status_code=201)
def category_add(body: CategoryBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        cat = engagement.category_save(db, None, body.name)
        _record(db, ac, "category_add", target_type="category", target_id=cat["id"], detail=cat["name"])
        return cat


@router.put("/library/categories/{cat_id}")
def category_edit(cat_id: str, body: CategoryBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        cat = engagement.category_save(db, cat_id, body.name)
        _record(db, ac, "category_edit", target_type="category", target_id=cat_id, detail=cat["name"])
        return cat


@router.delete("/library/categories/{cat_id}")
def category_delete(cat_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        engagement.category_delete(db, cat_id)
        _record(db, ac, "category_delete", target_type="category", target_id=cat_id)
    return {"ok": True}


# ----------------------------------------------------------------- engagement control


class CommentSource(_Body):
    kind: str = Field(max_length=8)  # library | random | text
    library_ids: list[str] = Field(default_factory=list, max_length=500)
    category: str | None = Field(default=None, max_length=32)
    count: int | None = None
    text: str | None = Field(default=None, max_length=4000)


class TeamCommentBody(_Body):
    target_type: str = Field(max_length=8)
    ids: list[str] = Field(max_length=500)
    source: CommentSource
    appearance: str = Field(default="dzplay", max_length=8)  # dzplay | official
    duration_minutes: int | None = Field(default=None, ge=0)


@router.post("/engagement/comments")
def engagement_comments(body: TeamCommentBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        result = engagement.post_comments(db, st.settings, target_type=body.target_type, ids=body.ids,
                                          source=body.source.model_dump(), appearance=body.appearance,
                                          duration_minutes=body.duration_minutes, actor=ac.actor, effects=effects)
        _record(db, ac, "engagement_comments", target_type=body.target_type, target_id=result["batch_id"],
                detail=f"source={body.source.kind} appearance={body.appearance} minutes={body.duration_minutes or 0} "
                       f"targets={len(result['targets'])}")
    st.dispatch(effects)
    return result


@router.get("/engagement/jobs")
def engagement_jobs(request: Request, status: str = Query(default="", max_length=12), ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"jobs": engagement.jobs(db, status)}


@router.post("/engagement/jobs/{job_id}/cancel")
def engagement_cancel(job_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        n = engagement.cancel(db, job_id)
        _record(db, ac, "engagement_cancel", target_type="job", target_id=job_id, detail=str(n))
        return {"cancelled": n}


# ----------------------------------------------------------------- users & network blocks


@router.get("/users")
def users(request: Request, filter: str = Query(default="reported", max_length=16), ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        return {"users": admin_content.users_list(db, filter)}


@router.get("/ip-blocks")
def ip_blocks(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        return {"blocks": admin_content.ip_blocks(db, st.settings)}


@router.delete("/ip-blocks/{block_id:path}")
def lift_ip_block(block_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        admin_content.lift_ip_block(db, block_id[:200])
        _record(db, ac, "ip_unblock", target_type="network", target_id=block_id.split(":")[-1][:12])
    return {"ok": True}


# ----------------------------------------------------------------- full access (privacy policy v3; every read audited)


@router.get("/access/users")
def access_users(request: Request, q: str = Query(default="", max_length=200), status: str = Query(default="", max_length=16),
                 method: str = Query(default="", max_length=16), flagged: bool = False, has_posts: bool = False,
                 created_from: str | None = Query(default=None, max_length=40), created_to: str | None = Query(default=None, max_length=40),
                 include_team: bool = False, page: int = Query(default=0, ge=0, le=10_000), size: int = Query(default=50, ge=1, le=100),
                 ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.users_search(db, q=q, status=status, method=method, flagged=flagged, has_posts=has_posts,
                                           created_from=created_from, created_to=created_to, include_team=include_team,
                                           page=page, size=size)
        _record(db, ac, "view_users", detail=f"q={q[:60]!r} count={len(result['users'])}")
        return result


@router.get("/access/users/{user_id}")
def access_user(user_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.user_detail(db, user_id)
        _record(db, ac, "view_user", target_type="user", target_id=user_id)
        return result


@router.post("/access/users/{user_id}/revoke-sessions")
def access_revoke(user_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        n = admin_access.revoke_sessions(db, user_id)
        _record(db, ac, "user_revoke_sessions", target_type="user", target_id=user_id, detail=str(n))
        return {"revoked": n}


@router.delete("/access/users/{user_id}")
def access_delete_user(user_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    effects = Effects()
    with st.database.session() as db:
        admin_access.delete_account(db, user_id)
        _record(db, ac, "user_delete", target_type="user", target_id=user_id)
    st.dispatch(effects)
    return {"ok": True}


@router.get("/access/ideas")
def access_ideas(request: Request, q: str = Query(default="", max_length=200), status: str = Query(default="", max_length=16),
                 page: int = Query(default=0, ge=0, le=10_000), size: int = Query(default=30, ge=1, le=100),
                 ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.ideas(db, q, status, page, size)
        _record(db, ac, "view_ideas", detail=f"q={q[:60]!r}")
        return result


@router.get("/access/ideas/{post_id}")
def access_idea(post_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.idea_detail(db, post_id)
        _record(db, ac, "view_idea_comments", target_type="idea", target_id=post_id)
        return result


@router.get("/access/conversations")
def access_conversations(request: Request, q: str = Query(default="", max_length=200), user: str = Query(default="", max_length=32),
                         page: int = Query(default=0, ge=0, le=10_000), size: int = Query(default=30, ge=1, le=100),
                         ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.conversations(db, q, user, page, size)
        _record(db, ac, "view_conversation_list", target_type="user" if user else None, target_id=user or None,
                detail=f"q={q[:60]!r}")
        return result


@router.get("/access/conversations/{conversation_id}")
def access_conversation(conversation_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.conversation_detail(db, conversation_id)
        _record(db, ac, "view_conversation", target_type="conversation", target_id=conversation_id)
        return result


@router.get("/access/search")
def access_search(request: Request, q: str = Query(max_length=200), ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        result = admin_access.search(db, q)
        _record(db, ac, "search_content", detail=f"q={q[:60]!r}")
        return result


@router.delete("/access/content/{kind}/{item_id}")
def access_delete(kind: str, item_id: str, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    with get_state(request).database.session() as db:
        admin_access.delete_content(db, kind[:16], item_id)
        _record(db, ac, f"delete_{kind[:16]}", target_type=kind[:16], target_id=item_id)
    return {"ok": True}


@router.get("/system")
def system_status(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    bot = {"configured": st.bot is not None, "webhook": None, "error": None}
    if st.telegram is not None:
        try:
            info = st.telegram.webhook_info()
            bot["webhook"] = {"url_set": bool(info.get("url")), "pending_updates": info.get("pending_update_count"),
                              "last_error": info.get("last_error_message")}
        except TelegramError as exc:
            bot["error"] = str(exc)
    with st.database.session() as db:
        media_bytes = st.media.total_size(db)
        return {
            "bot": bot,
            "smtp_configured": runtime_config.smtp_config(db, st.settings)["source"] != "none",
            "media_cache": {"bytes": media_bytes, "limit_bytes": int(st.settings.MEDIA_CACHE_MAX_GB * 1024 ** 3)},
            "ip_blocks": admin_content.ip_blocks(db, st.settings),
            "failed_logins": admin_access.failed_logins(db, 50),
            "reset_requests": admin_access.reset_requests(db, 50),
        }


# ----------------------------------------------------------------- Telegram bot settings (token is write-only)


class TelegramBody(_Body):
    token: str = Field(max_length=100)
    chat_id: str = Field(max_length=24)
    code: str = Field(max_length=12)


class StepUpBody(_Body):
    code: str = Field(max_length=12)


def _public_url(request: Request, settings) -> str:
    return (settings.PUBLIC_URL or f"https://{request.headers.get('host', '')}").rstrip("/")


def _telegram_status(st, request: Request) -> dict:
    with st.database.session() as db:
        cfg = runtime_config.telegram_config(db, st.settings)
    out = {"configured": st.bot is not None, "source": st.telegram_source, "chat_id": cfg["chat_id"] or None,
           "token_hint": ("…" + cfg["token"][-4:]) if cfg["token"] else None, "bot_username": None,
           "webhook": None, "error": None, "public_url": _public_url(request, st.settings)}
    if st.telegram is not None:
        try:
            out["bot_username"] = st.telegram.get_me().get("username")
            info = st.telegram.webhook_info()
            out["webhook"] = {"url_set": bool(info.get("url")), "pending_updates": info.get("pending_update_count"),
                              "last_error": info.get("last_error_message")}
        except TelegramError as exc:
            out["error"] = str(exc)
    return out


def _hello(st) -> str | None:
    """Send the test message to the admin chat; returns an Arabic hint on failure."""
    from app.services.telegram_bot import HELP

    try:
        st.telegram.send_message(st.bot.admin_id, f"✅ {st.settings.APP_NAME} متصل. أرسل فيديو أو صورة مع وصف لنشرها.\n\n" + HELP)
        return None
    except TelegramError as exc:
        if "chat not found" in str(exc).lower() or "403" in str(exc):
            return "تعذّر إرسال رسالة التجربة: افتح البوت في Telegram واضغط Start، وتأكد من رقم المحادثة."
        return "تعذّر إرسال رسالة التجربة: " + str(exc)


@router.get("/telegram")
def telegram_get(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _telegram_status(get_state(request), request)


@router.put("/telegram")
def telegram_put(body: TelegramBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    from app.services.telegram import TelegramClient

    st = get_state(request)
    token, chat_id = runtime_config.validate(body.token, body.chat_id)
    public_url = _public_url(request, st.settings)
    if not public_url.startswith("https://") or public_url == "https://":
        raise AppError(400, "https_required", "يحتاج Telegram عنوان https للموقع (PUBLIC_URL).")
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
    probe = TelegramClient(st.settings.model_copy(update={"TELEGRAM_BOT_TOKEN": token}), transport=st.telegram_transport)
    try:
        me = probe.get_me()
    except TelegramError:
        raise AppError(400, "invalid_token", "Telegram رفض هذا الرمز. انسخه من جديد من @BotFather.") from None
    finally:
        probe.close()
    with st.database.session() as db:
        cfg = runtime_config.save_telegram(db, st.settings, token, chat_id, ac.actor)
        _record(db, ac, "telegram_update", target_type="bot", target_id=str(me.get("username") or "")[:32],
                detail=f"chat_id={chat_id}")
    st.configure_telegram(cfg["token"], cfg["chat_id"], cfg["secret"], "panel")
    try:
        st.telegram.set_webhook(public_url + "/api/telegram/webhook", cfg["secret"])
    except TelegramError as exc:
        raise AppError(502, "webhook_failed", "حُفظ الرمز، لكن تعذّر ربط Telegram بالموقع: " + str(exc)) from None
    out = _telegram_status(st, request)
    out["test_error"] = _hello(st)
    return out


@router.post("/telegram/test")
def telegram_test(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    if st.bot is None:
        raise AppError(409, "not_configured", "البوت غير مربوط بعد.")
    problem = _hello(st)
    if problem:
        raise AppError(502, "test_failed", problem)
    return {"ok": True}


@router.post("/telegram/remove")
def telegram_remove(body: StepUpBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
        runtime_config.clear_telegram(db)
        _record(db, ac, "telegram_remove", target_type="bot")
    if st.telegram is not None and st.telegram_source == "panel":
        try:
            st.telegram.delete_webhook()
        except TelegramError:
            pass
    if st.settings.telegram_enabled:  # back to the .env bot
        st.configure_telegram(st.settings.TELEGRAM_BOT_TOKEN, st.settings.TELEGRAM_ADMIN_CHAT_ID,
                              st.settings.TELEGRAM_WEBHOOK_SECRET, "env")
        try:
            st.telegram.set_webhook(_public_url(request, st.settings) + "/api/telegram/webhook", st.telegram_secret)
        except TelegramError:
            pass
    else:
        st.configure_telegram(None)
    return _telegram_status(st, request)


# ----------------------------------------------------------------- e-mail (SMTP) settings (password is write-only)


class SmtpBody(_Body):
    host: str = Field(max_length=253)
    port: int = Field(ge=1, le=65535)
    security: str = Field(max_length=10)
    username: str = Field(default="", max_length=200)
    password: str = Field(default="", max_length=200)  # empty = keep the stored one
    sender: str = Field(max_length=200)
    test_to: str = Field(default="", max_length=254)
    code: str = Field(max_length=12)


class SmtpTestBody(_Body):
    to: str = Field(max_length=254)


def _smtp_status(st, profile: str = "system") -> dict:
    from app.services import mail

    with st.database.session() as db:
        cfg = runtime_config.smtp_config(db, st.settings, profile)
        support = mail.support_address(db, st.settings)
    return {"profile": profile, "configured": cfg["source"] != "none", "source": cfg["source"],
            "inherited": bool(cfg.get("inherited")), "host": cfg["host"] or None, "port": cfg["port"],
            "security": cfg["security"], "username": cfg["username"] or None, "sender": cfg["from"] or None,
            "password_set": bool(cfg["password"]), "support_address": support or None}


def _send_test_mail(st, to: str, profile: str = "system") -> str | None:
    from app.services import mail
    from app.services.mailer import MailError

    to = (to or "").strip()
    if not runtime_config._EMAIL_RE.match(to):
        return "عنوان بريد التجربة غير صالح."
    with st.database.session() as db:
        out = mail.prepare(db, st.settings, profile, to, "email.test", {"mailbox": mail.LABELS[profile]})
    if out is None:
        return "البريد غير مُعدّ بعد."
    try:
        mail.deliver(out)
        return None
    except MailError as exc:
        return f"تعذّر إرسال بريد التجربة ({exc}). تحقق من الخادم والمنفذ واسم المستخدم وكلمة المرور."


def _smtp_save(body: SmtpBody, request: Request, ac: AdminContext, profile: str) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
    with st.database.session() as db:
        runtime_config.save_smtp(db, st.settings, host=body.host, port=body.port, security=body.security,
                                 username=body.username, password=body.password, sender=body.sender, actor=ac.actor,
                                 profile=profile)
        _record(db, ac, "smtp_update", target_type="smtp", target_id=f"{profile}:{body.host[:24]}")
    out = _smtp_status(st, profile)
    out["test_error"] = _send_test_mail(st, body.test_to, profile) if body.test_to else None
    return out


def _smtp_test(body: SmtpTestBody, request: Request, profile: str) -> dict:
    problem = _send_test_mail(get_state(request), body.to, profile)
    if problem:
        raise AppError(502, "test_failed", problem)
    return {"ok": True}


def _smtp_remove(body: StepUpBody, request: Request, ac: AdminContext, profile: str) -> dict:
    st = get_state(request)
    with st.database.session() as db:
        admin_auth.verify_step_up(db, st.settings, ac.client, ac.admin, body.code)
        runtime_config.clear_smtp(db, profile)
        _record(db, ac, "smtp_remove", target_type="smtp", target_id=profile)
    return _smtp_status(st, profile)


# system mailbox (V5 routes, unchanged)
@router.get("/smtp")
def smtp_get(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_status(get_state(request))


@router.put("/smtp")
def smtp_put(body: SmtpBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_save(body, request, ac, "system")


@router.post("/smtp/test")
def smtp_test(body: SmtpTestBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_test(body, request, "system")


@router.post("/smtp/remove")
def smtp_remove(body: StepUpBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_remove(body, request, ac, "system")


# V6 phase 7: support mailbox (tickets and replies)
@router.get("/smtp/support")
def smtp_support_get(request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_status(get_state(request), "support")


@router.put("/smtp/support")
def smtp_support_put(body: SmtpBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_save(body, request, ac, "support")


@router.post("/smtp/support/test")
def smtp_support_test(body: SmtpTestBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_test(body, request, "support")


@router.post("/smtp/support/remove")
def smtp_support_remove(body: StepUpBody, request: Request, ac: AdminContext = Depends(SUPER_ADMIN)) -> dict:
    return _smtp_remove(body, request, ac, "support")


