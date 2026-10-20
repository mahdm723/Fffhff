"""DZPLAY application factory.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import re
import logging
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

import anyio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app import i18n
from app.i18n import N_
from app.api import account as account_api
from app.api import admin as admin_api
from app.api import admin_money as admin_money_api
from app.api import admin_official as admin_official_api
from app.api import loadtest as loadtest_api
from app.api import admin_v5 as admin_v5_api
from app.api import admin_brand as admin_brand_api
from app.api import admin_cms as admin_cms_api
from app.api import auth as auth_api
from app.api import cms as cms_api
from app.api import download as download_api
from app.api import giveaway as giveaway_api
from app.api import i18n as i18n_api
from app.api import market as market_api
from app.api import media as media_api
from app.api import membership as membership_api
from app.api import messages as messages_api
from app.api import notifications as notifications_api
from app.api import people as people_api
from app.api import policies as policies_api
from app.api import admin_i18n as admin_i18n_api
from app.api import app_access as app_access_api
from app.api import posts as posts_api
from app.api import rewards as rewards_api
from app.api import telegram as telegram_api
from app.api import uploads as uploads_api
from app.api import ws as ws_api
from app.config import Settings, get_settings
from app.errors import AppError
from app.security import passwords
from app.security.net import client_ip
from app.services import access, admin_auth, leader
from app.services.cleanup import run_cleanup
from app.state import AppState

log = logging.getLogger("dzplay")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
_ADMIN_ASSETS = {"admin.css": "text/css", **{f"{m}.js": "text/javascript" for m in (
    "admin", "admin-common", "admin-users", "admin-content", "admin-engage", "admin-system", "admin-v5", "admin-money",
    "admin-texts", "admin-brand", "admin-i18n", "admin-official")}}


def brand(page: str, settings: Settings, base: str = "") -> str:
    """The placeholders of a page shell: {{APP_NAME}} (escaped), {{WORDMARK}} (the name with the logo's red dot:
    DALTA.BIT → DALTA●BIT), {{LOGO}} (the logo inline, for the boot screen), {{HEAD_ICONS}} and {{OG}} (link
    previews, absolute URLs from `base`)."""
    from app.services import brand as brand_svc

    name = settings.APP_NAME
    lang = i18n.current()  # V6 phase 12: the first guess; js/theme-boot.js applies the app's saved choice at once
    page = page.replace("{{HEAD_ICONS}}", brand_svc.HEAD_ICONS)
    if "{{OG}}" in page:
        page = page.replace("{{OG}}", brand_svc.og_tags(settings, base, name, i18n.tr(OG_DESCRIPTION, lang), lang))
    return (page.replace("{{APP_NAME}}", html.escape(name)).replace("{{WORDMARK}}", brand_svc.wordmark_html(name))
            .replace("{{LOGO}}", brand_svc.logo_svg()).replace("{{LANG}}", lang).replace("{{DIR}}", i18n.direction(lang))
            .replace("{{DESCRIPTION}}", html.escape(i18n.tr(OG_DESCRIPTION, lang)))
            .replace("{{NOSCRIPT}}", html.escape(i18n.tr("يحتاج {app} إلى تفعيل JavaScript.", lang, app=name))))


OG_DESCRIPTION = N_("مجتمع للمتداولين ولمشاركة أخبار العملات الرقمية والتحليلات والأفكار، مع حماية بياناتك.")


def request_language(request: Request) -> str:
    """V6 phase 12: the app sends its language (X-DZ-Lang); pages without JS: ?lang=, the dz_lang cookie, then
    Accept-Language; Arabic otherwise."""
    return (i18n.normalize(request.headers.get("x-dz-lang")) or i18n.normalize(request.query_params.get("lang"))
            or i18n.normalize(request.cookies.get("dz_lang")) or i18n.from_accept_language(request.headers.get("accept-language"))
            or i18n.DEFAULT)


_CSRF_EXEMPT = {"/api/telegram/webhook"}  # authenticated by Telegram's secret header instead

# Don't depend on the host's /etc/mime.types (ES modules require a JS MIME type).
for _ext, _type in {".js": "text/javascript", ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
                    ".svg": "image/svg+xml", ".css": "text/css"}.items():
    mimetypes.add_type(_type, _ext)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY_BYTES = 64 * 1024  # API bodies are tiny JSON documents
_UPLOAD_PATH = "/api/uploads"  # V5: file uploads carry their own per-purpose cap (app.services.media_items)
_OFFICIAL_UPLOAD = "/api/admin/official/uploads"  # V6 phase 19: the official account's picture (capped by the route)
# V6 phase 17: pictures in chats are switched off for good — refused here, before the body is read
_CHAT_MEDIA_PATH = re.compile(r"^/api/conversations/[^/]+/media$")


# V6 phase 20: on an old address (LEGACY_HOSTS) only what the installed Android app and the download page need is
# still served (the app itself, its update, the files its pages load); everything else moves to APP_DOMAIN.
_LEGACY_SERVED = ("/download", "/api/app/", "/.well-known/", "/healthz", "/api/telegram/webhook", "/sw.js",
                  "/manifest.webmanifest", "/css/", "/js/", "/icons/", "/fonts/", "/brand/", "/favicon", "/robots.txt")


def _legacy_redirect(request: Request, settings: Settings) -> Response | None:
    if not settings.APP_DOMAIN:
        return None
    legacy = settings.legacy_hosts
    host = (request.headers.get("host") or "").strip().lower()
    host = host[1:].split("]")[0] if host.startswith("[") else host.split(":")[0]
    if not legacy or host not in legacy:
        return None
    path = request.url.path
    if path.startswith(_LEGACY_SERVED) or "DZPLAYApp/" in request.headers.get("user-agent", ""):
        return None
    target = f"https://{settings.APP_DOMAIN}{path}" + (f"?{request.url.query}" if request.url.query else "")
    return RedirectResponse(target, status_code=301 if request.method in ("GET", "HEAD") else 308)


def _chat_picture_upload(request: Request) -> bool:
    path = request.url.path
    if request.method != "POST":
        return False
    if path == _UPLOAD_PATH:
        return request.query_params.get("purpose") == "chat"
    return bool(_CHAT_MEDIA_PATH.match(path))
I18N_IMPORT_BYTES = 3 * 1024 * 1024  # V6 phase 12: a translation file (every text + the policy pages), panel only


def _error(status: int, code: str, message: str, retry_after: int | None = None, params: dict | None = None,
           extra: dict | None = None) -> JSONResponse:
    """The error body; the message (an Arabic msgid) in the request's language (V6 phase 12)."""
    body = {"error": {"code": code, "message": i18n.tr(message, **(params or {})), **(extra or {})}}
    headers = {}
    if retry_after is not None:
        body["error"]["retry_after"] = retry_after
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(body, status_code=status, headers=headers)


def _csp(settings: Settings) -> str:
    gsi = "https://accounts.google.com/gsi/"
    g = settings.google_enabled
    directives = {
        "default-src": "'self'",
        "script-src": "'self'" + (f" {gsi}client" if g else ""),
        "style-src": "'self'" + (f" {gsi}style" if g else ""),
        "frame-src": gsi if g else "'none'",
        "connect-src": "'self'" + (f" {gsi}" if g else ""),
        "img-src": "'self' data: blob:",  # blob: previews of a picture picked on the phone (never uploaded yet)
        "media-src": "'self' blob:",
        "font-src": "'self'",
        "object-src": "'none'",
        "base-uri": "'none'",
        "form-action": "'self'",
        "frame-ancestors": "'none'",
        "worker-src": "'self'",
        "manifest-src": "'self'",
    }
    return "; ".join(f"{k} {v}" for k, v in directives.items())


async def _cleanup_loop(state: AppState) -> None:
    while True:
        try:
            def _run() -> None:
                with state.database.session() as db:
                    if leader.take(db, "cleanup"):  # V6 phase 21: one worker at a time
                        run_cleanup(db, state.settings, state.media)

            await run_in_threadpool(_run)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("cleanup failed")
        await asyncio.sleep(state.settings.CLEANUP_INTERVAL)


async def _engagement_loop(state: AppState) -> None:
    """Advance spread-out team comments (see app.services.engagement)."""
    from app.services import engagement
    from app.services.messaging import Effects

    while True:
        await asyncio.sleep(state.settings.ENGAGEMENT_TICK_SECONDS)
        try:
            effects = Effects()

            def _run() -> None:
                with state.database.session() as db:
                    if leader.take(db, "engagement"):  # never two workers posting the same team comments
                        engagement.tick(db, state.settings, effects)

            await run_in_threadpool(_run)
            state.dispatch(effects)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("engagement tick failed")


async def _media_loop(state: AppState) -> None:
    """V5: chat pictures disappear on time (both sides), unused uploads and old evidence are purged."""
    from app.services import media_items
    from app.services.messaging import Effects

    while True:
        await asyncio.sleep(state.settings.MEDIA_TICK_SECONDS)
        try:
            effects = Effects()

            def _run() -> None:
                with state.database.session() as db:
                    if leader.take(db, "media"):
                        media_items.tick(db, state.settings, effects)

            await run_in_threadpool(_run)
            state.dispatch(effects)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("media tick failed")


async def _market_loop(state: AppState) -> None:
    """V6 phase 2: refresh the market list (top gainers/losers) from Bybit."""
    from app.services import market

    while True:
        try:
            def _run() -> None:
                with state.database.session() as db:
                    if leader.take(db, "market"):  # the others read the shared snapshot
                        market.refresh(db, state.settings)
                    else:
                        market.load_shared(db)

            await run_in_threadpool(_run)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("market refresh failed")
        await asyncio.sleep(max(10, state.settings.MARKET_REFRESH_SECONDS))


async def _metrics_loop(state: AppState) -> None:
    """V6 phase 21: the request timings go to Redis every few seconds (several workers add up)."""
    while True:
        await asyncio.sleep(state.settings.METRICS_FLUSH_SECONDS)
        try:
            await run_in_threadpool(state.metrics.flush)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("metrics flush failed")


async def _chain_loop(state: AppState) -> None:
    """V6 phase 13: check again the membership payments not found / not confirmed yet (claimed atomically per row,
    so several instances never check one twice). It only records results: it never accepts or rejects."""
    from app.services import payment_check

    while True:
        try:
            if state.settings.CHAIN_CHECK_ENABLED:
                for request_id in await run_in_threadpool(payment_check.due, state):
                    await run_in_threadpool(payment_check.run, state, request_id)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("payment check failed")
        await asyncio.sleep(max(10, state.settings.CHAIN_TICK_SECONDS))


def _raise_open_files() -> None:
    """V6 phase 21: one open file per WebSocket. Containers often start with a soft limit of 1024 (the hard limit is far
    higher) and Python never raises it: up to the hard limit, which any process may do."""
    import resource

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    want = min(hard if hard != resource.RLIM_INFINITY else 1 << 20, 1 << 20)
    if soft < want:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (want, hard))
        except (ValueError, OSError):
            log.warning("open files: could not raise the limit above %d", soft)


class _QuietSockets(logging.Filter):
    """V6 phase 21: uvicorn logs two INFO lines for every WebSocket ('… "WebSocket /api/ws" [accepted]', "connection
    open"), even with --no-access-log. With thousands of phones that was most of the log (and its CPU under load): the
    size-capped container log would rotate real warnings and errors away within minutes. Those two lines only."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno != logging.INFO or not isinstance(record.msg, str):
            return True
        return not (record.msg.endswith('" [accepted]') or record.msg in ("connection open", "connection closed"))


def _check_capacity(state: AppState) -> None:
    """V6 phase 21: every worker's pool must fit in PostgreSQL's max_connections, or requests fail under load."""
    s = state.settings
    if state.database.engine.dialect.name != "postgresql":
        return
    try:
        with state.database.engine.connect() as conn:
            limit = int(conn.execute(text("SHOW max_connections")).scalar())
    except Exception:  # noqa: BLE001 - a warning only
        return
    need = s.WEB_CONCURRENCY * (s.DB_POOL_SIZE + s.DB_MAX_OVERFLOW) + 10
    if need > limit:
        log.warning("database: %d workers × (%d + %d) connections + 10 = %d > max_connections %d — lower DB_POOL_SIZE / "
                    "DB_MAX_OVERFLOW or raise max_connections", s.WEB_CONCURRENCY, s.DB_POOL_SIZE, s.DB_MAX_OVERFLOW, need, limit)


def create_app(settings: Settings | None = None, telegram_transport=None, chain_transport=None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO — Telegram URLs contain the bot token. Never log them.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn.error", "websockets.server"):
        if not any(isinstance(f, _QuietSockets) for f in logging.getLogger(name).filters):
            logging.getLogger(name).addFilter(_QuietSockets())
    from app.services import names

    names.configure(settings)
    passwords.configure(settings.PASSWORD_HASH_CONCURRENCY, settings.PASSWORD_HASH_QUEUE,  # V6 phase 21: a login wave
                        settings.PASSWORD_HASH_WAIT_SECONDS, settings.WEB_CONCURRENCY)   # never exhausts the memory
    _raise_open_files()
    state = AppState.build(settings, telegram_transport, chain_transport)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        with state.database.startup_lock():  # V6 phase 21: several workers start together; one at a time here
            state.database.create_all()
            with state.database.session() as db:  # V6 phase 19: exactly one official account, named after the app
                from app.services import admin_content

                admin_content.ensure_official(db, settings)
        state.load_runtime_config()  # bot settings saved from the admin panel
        anyio.to_thread.current_default_thread_limiter().total_tokens = max(4, settings.THREADPOOL_SIZE)
        _check_capacity(state)
        if settings.secret_key_generated:
            log.warning("SECRET_KEY not set: using a temporary key (sessions survive, IP blocks reset on restart).")
        await state.hub.start()
        cleanup_task = asyncio.create_task(_cleanup_loop(state)) if settings.CLEANUP_INTERVAL > 0 else None
        engagement_task = asyncio.create_task(_engagement_loop(state)) if settings.ENGAGEMENT_TICK_SECONDS > 0 else None
        media_task = asyncio.create_task(_media_loop(state)) if settings.MEDIA_TICK_SECONDS > 0 else None
        market_task = asyncio.create_task(_market_loop(state)) if settings.MARKET_REFRESH_SECONDS > 0 else None
        chain_task = asyncio.create_task(_chain_loop(state)) if settings.CHAIN_TICK_SECONDS > 0 else None
        metrics_task = asyncio.create_task(_metrics_loop(state)) if settings.METRICS_FLUSH_SECONDS > 0 else None
        yield
        if metrics_task:
            metrics_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await metrics_task
            with contextlib.suppress(Exception):
                await run_in_threadpool(state.metrics.flush)
        if chain_task:
            chain_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await chain_task
        if market_task:
            market_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await market_task
        if media_task:
            media_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await media_task
        if engagement_task:
            engagement_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await engagement_task
        if cleanup_task:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await state.hub.stop()
        state.push.shutdown()
        if state.fcm is not None:
            state.fcm.shutdown()
        if state.bot is not None:
            state.bot.shutdown()
        state.pipeline.shutdown()
        state.mail_queue.shutdown()

    app = FastAPI(title=settings.APP_NAME, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dz = state
    csp = _csp(settings)
    admin_prefix = settings.ADMIN_PATH
    from app.services.metrics import Metrics, MetricsMiddleware

    state.metrics = Metrics(settings.REDIS_URL, admin_prefix)  # V6 phase 21: «صحة النظام», alerts, load tests

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        moved = _legacy_redirect(request, settings)  # V6 phase 20: the old address → the domain
        if moved is not None:
            return moved
        in_admin = bool(admin_prefix) and (path == admin_prefix or path.startswith(admin_prefix + "/"))
        # the panel (its pages and its API) stays in Arabic whatever language the app on the same browser uses
        i18n.set_current(i18n.DEFAULT if in_admin or path.startswith("/api/admin") else request_language(request))
        if in_admin and not admin_auth.ip_allowed(settings, client_ip(request, settings)):
            return _error(404, "not_found", "غير موجود.")  # outside the allowlist the panel does not exist
        is_api = path.startswith("/api/") or (in_admin and path.startswith(admin_prefix + "/api/"))
        if access.gated(path, admin_prefix):  # V6 phase 14: who may open the app (services/access.py)
            verdict = access.decision(settings, access.classify(settings, request.headers.get("user-agent", ""),
                                                                request.cookies))
            if verdict is not None:
                if is_api:
                    return _error(403, "app_required", N_("افتح التطبيق للمتابعة."), extra={"gate": verdict})
                return download_api.gate_response(request, verdict)
        if _chat_picture_upload(request):
            return _error(410, "chat_images_disabled", N_("إرسال الصور في المحادثات متوقف نهائيًا."))
        cap = I18N_IMPORT_BYTES if in_admin and path.startswith(admin_prefix + "/api/admin/i18n/import") else MAX_BODY_BYTES
        upload = path == _UPLOAD_PATH or (in_admin and path == admin_prefix + _OFFICIAL_UPLOAD)
        if is_api and not upload and int(request.headers.get("content-length") or 0) > cap:
            return _error(413, "too_large", "الطلب كبير جدًا.")
        if is_api and request.method not in _SAFE_METHODS and path not in _CSRF_EXEMPT:
            # CSRF defence in depth (cookies are SameSite=Strict as well): a custom
            # header cannot be sent cross-site without a CORS preflight we never allow.
            if request.headers.get("x-dz-requested") != "1":
                return _error(403, "csrf", "طلب غير مسموح.")
            origin = request.headers.get("origin")
            if origin and urlsplit(origin).netloc != request.headers.get("host") and origin.rstrip("/") not in settings.allowed_origins:
                return _error(403, "csrf", "طلب غير مسموح.")
        response = await call_next(request)
        h = response.headers
        h["X-Content-Type-Options"] = "nosniff"
        h["Referrer-Policy"] = "no-referrer"
        h["X-Frame-Options"] = "DENY"
        h["Permissions-Policy"] = "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        h["Cross-Origin-Opener-Policy"] = "same-origin-allow-popups"
        h["Cross-Origin-Resource-Policy"] = "same-origin"  # no other site may embed our responses
        h["X-Permitted-Cross-Domain-Policies"] = "none"
        h["Content-Security-Policy"] = csp
        if settings.COOKIE_SECURE:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if is_api or in_admin:
            h["Cache-Control"] = "no-store"
        if in_admin:
            h["X-Robots-Tag"] = "noindex, nofollow"
        elif "Cache-Control" not in h:
            h["Cache-Control"] = "no-cache"
        return response

    @app.exception_handler(AppError)
    async def app_error(_request: Request, exc: AppError):
        return _error(exc.status, exc.code, exc.message, exc.retry_after, exc.params)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, _exc: RequestValidationError):
        return _error(400, "invalid_input", "طلب غير صالح.")

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_request: Request, exc: StarletteHTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _error(exc.status_code, code, "غير موجود." if exc.status_code == 404 else "طلب غير صالح.")

    @app.exception_handler(Exception)
    async def unexpected(_request: Request, exc: Exception):
        log.exception("unhandled error", exc_info=exc)
        return _error(500, "server_error", "حدث خطأ غير متوقع. حاول مرة أخرى.")

    @app.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/healthz/ip", include_in_schema=False)
    def healthz_ip(request: Request) -> JSONResponse:
        """V6 phase 20: the address the server sees for the caller — its own, never anyone else's, never stored. The
        move to Cloudflare checks with it that the real client arrives (CF-Connecting-IP from Cloudflare only)."""
        return JSONResponse({"ip": client_ip(request, settings)}, headers={"Cache-Control": "no-store"})

    app.include_router(auth_api.router)
    app.include_router(loadtest_api.router)  # V6 phase 21: staging only (404 everywhere else)
    app.include_router(messages_api.router)
    app.include_router(people_api.router)
    app.include_router(download_api.router)
    app.include_router(media_api.router)
    app.include_router(market_api.router)
    app.include_router(notifications_api.router)
    app.include_router(membership_api.router)
    app.include_router(rewards_api.router)
    app.include_router(giveaway_api.router)
    app.include_router(policies_api.router)
    app.include_router(cms_api.router)
    app.include_router(posts_api.router)
    app.include_router(telegram_api.router)
    app.include_router(uploads_api.router)
    app.include_router(account_api.router)
    app.include_router(i18n_api.router)
    app.include_router(app_access_api.router)
    if settings.admin_enabled:
        app.include_router(admin_api.router, prefix=admin_prefix)
        app.include_router(admin_v5_api.router, prefix=admin_prefix)
        app.include_router(admin_money_api.router, prefix=admin_prefix)
        app.include_router(admin_official_api.router, prefix=admin_prefix)  # V6 phase 19
        app.include_router(admin_cms_api.router, prefix=admin_prefix)
        app.include_router(admin_brand_api.router, prefix=admin_prefix)
        app.include_router(admin_i18n_api.router, prefix=admin_prefix)
    app.include_router(ws_api.router)

    @app.get("/.well-known/assetlinks.json", include_in_schema=False)
    def assetlinks() -> JSONResponse:
        """Digital Asset Links for the Android app (full-screen Trusted Web Activity)."""
        prints = settings.android_cert_fingerprints
        if not prints:
            return _error(404, "not_found", "غير موجود.")
        return JSONResponse([{
            "relation": ["delegate_permission/common.handle_all_urls"],
            "target": {"namespace": "android_app", "package_name": settings.ANDROID_APP_PACKAGE,
                       "sha256_cert_fingerprints": prints},
        }])

    @app.get("/download/dzplay.apk", include_in_schema=False)
    def android_apk() -> FileResponse:
        apk = STATIC_DIR / "download" / "dzplay.apk"
        if not apk.is_file():
            raise StarletteHTTPException(404)
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename=(re.sub(r"[^A-Za-z0-9._-]", "", settings.APP_NAME) or "app") + ".apk")

    if STATIC_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        @app.get("/index.html", include_in_schema=False)
        def index(request: Request) -> HTMLResponse:
            """The app shell, with the name from APP_NAME (V6 phase 8: one setting renames the whole app)."""
            base = settings.PUBLIC_URL or str(request.base_url)
            return HTMLResponse(brand((STATIC_DIR / "index.html").read_text(encoding="utf-8"), settings, base))

        @app.get("/manifest.webmanifest", include_in_schema=False)
        def manifest() -> Response:
            data = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
            data["name"] = data["short_name"] = settings.APP_NAME
            lang = i18n.current()  # V6 phase 12: the description in the visitor's language
            data.update(lang=lang, dir=i18n.direction(lang), description=i18n.tr(data.get("description", ""), lang))
            return Response(json.dumps(data, ensure_ascii=False), media_type="application/manifest+json",
                            headers={"Cache-Control": "no-cache", "Vary": "Cookie, Accept-Language"})

        if settings.admin_enabled:
            @app.get(admin_prefix, include_in_schema=False)
            def admin_page() -> HTMLResponse:
                """The admin panel (secret path; never linked from the app)."""
                page = (ADMIN_STATIC_DIR / "admin.html").read_text(encoding="utf-8")
                return HTMLResponse(brand(page.replace("{{ADMIN_PATH}}", admin_prefix), settings))

            @app.get(admin_prefix + "/assets/{name}", include_in_schema=False)
            def admin_asset(name: str) -> FileResponse:
                if name not in _ADMIN_ASSETS:  # fixed allowlist: no path input reaches the filesystem
                    raise StarletteHTTPException(404)
                return FileResponse(ADMIN_STATIC_DIR / name, media_type=_ADMIN_ASSETS[name])

        app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(MetricsMiddleware, metrics=state.metrics)  # outermost: the whole time of every request
    return app


app = create_app()
