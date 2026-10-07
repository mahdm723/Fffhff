"""DZPLAY application factory.

Run:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import account as account_api
from app.api import admin as admin_api
from app.api import admin_money as admin_money_api
from app.api import admin_v5 as admin_v5_api
from app.api import auth as auth_api
from app.api import download as download_api
from app.api import giveaway as giveaway_api
from app.api import market as market_api
from app.api import media as media_api
from app.api import membership as membership_api
from app.api import messages as messages_api
from app.api import notifications as notifications_api
from app.api import people as people_api
from app.api import policies as policies_api
from app.api import posts as posts_api
from app.api import rewards as rewards_api
from app.api import telegram as telegram_api
from app.api import uploads as uploads_api
from app.api import ws as ws_api
from app.config import Settings, get_settings
from app.errors import AppError
from app.security.net import client_ip
from app.services import admin_auth
from app.services.cleanup import run_cleanup
from app.state import AppState

log = logging.getLogger("dzplay")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
ADMIN_STATIC_DIR = Path(__file__).resolve().parent / "admin_static"
_ADMIN_ASSETS = {"admin.css": "text/css", **{f"{m}.js": "text/javascript" for m in (
    "admin", "admin-common", "admin-users", "admin-content", "admin-engage", "admin-system", "admin-v5", "admin-money")}}
_CSRF_EXEMPT = {"/api/telegram/webhook"}  # authenticated by Telegram's secret header instead

# Don't depend on the host's /etc/mime.types (ES modules require a JS MIME type).
for _ext, _type in {".js": "text/javascript", ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
                    ".svg": "image/svg+xml", ".css": "text/css"}.items():
    mimetypes.add_type(_type, _ext)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY_BYTES = 64 * 1024  # API bodies are tiny JSON documents
_UPLOAD_PATH = "/api/uploads"  # V5: file uploads carry their own per-purpose cap (app.services.media_items)


def _error(status: int, code: str, message: str, retry_after: int | None = None) -> JSONResponse:
    body = {"error": {"code": code, "message": message}}
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
                    market.refresh(db, state.settings)

            await run_in_threadpool(_run)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("market refresh failed")
        await asyncio.sleep(max(10, state.settings.MARKET_REFRESH_SECONDS))


def create_app(settings: Settings | None = None, telegram_transport=None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # httpx logs full request URLs at INFO — Telegram URLs contain the bot token. Never log them.
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    from app.services import names

    names.configure(settings)
    state = AppState.build(settings, telegram_transport)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.database.create_all()
        state.load_runtime_config()  # bot settings saved from the admin panel
        if settings.secret_key_generated:
            log.warning("SECRET_KEY not set: using a temporary key (sessions survive, IP blocks reset on restart).")
        await state.hub.start()
        cleanup_task = asyncio.create_task(_cleanup_loop(state)) if settings.CLEANUP_INTERVAL > 0 else None
        engagement_task = asyncio.create_task(_engagement_loop(state)) if settings.ENGAGEMENT_TICK_SECONDS > 0 else None
        media_task = asyncio.create_task(_media_loop(state)) if settings.MEDIA_TICK_SECONDS > 0 else None
        market_task = asyncio.create_task(_market_loop(state)) if settings.MARKET_REFRESH_SECONDS > 0 else None
        yield
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

    app = FastAPI(title="DZPLAY", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dz = state
    csp = _csp(settings)
    admin_prefix = settings.ADMIN_PATH

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        in_admin = bool(admin_prefix) and (path == admin_prefix or path.startswith(admin_prefix + "/"))
        if in_admin and not admin_auth.ip_allowed(settings, client_ip(request, settings)):
            return _error(404, "not_found", "غير موجود.")  # outside the allowlist the panel does not exist
        is_api = path.startswith("/api/") or (in_admin and path.startswith(admin_prefix + "/api/"))
        if is_api and path != _UPLOAD_PATH and int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
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
        return _error(exc.status, exc.code, exc.message, exc.retry_after)

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

    app.include_router(auth_api.router)
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
    app.include_router(posts_api.router)
    app.include_router(telegram_api.router)
    app.include_router(uploads_api.router)
    app.include_router(account_api.router)
    if settings.admin_enabled:
        app.include_router(admin_api.router, prefix=admin_prefix)
        app.include_router(admin_v5_api.router, prefix=admin_prefix)
        app.include_router(admin_money_api.router, prefix=admin_prefix)
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
        return FileResponse(apk, media_type="application/vnd.android.package-archive", filename="DZPLAY.apk")

    if STATIC_DIR.is_dir():
        @app.get("/", include_in_schema=False)
        def index() -> FileResponse:
            return FileResponse(STATIC_DIR / "index.html")

        if settings.admin_enabled:
            @app.get(admin_prefix, include_in_schema=False)
            def admin_page() -> HTMLResponse:
                """The admin panel (secret path; never linked from the app)."""
                html = (ADMIN_STATIC_DIR / "admin.html").read_text(encoding="utf-8")
                return HTMLResponse(html.replace("{{ADMIN_PATH}}", admin_prefix))

            @app.get(admin_prefix + "/assets/{name}", include_in_schema=False)
            def admin_asset(name: str) -> FileResponse:
                if name not in _ADMIN_ASSETS:  # fixed allowlist: no path input reaches the filesystem
                    raise StarletteHTTPException(404)
                return FileResponse(ADMIN_STATIC_DIR / name, media_type=_ADMIN_ASSETS[name])

        app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
