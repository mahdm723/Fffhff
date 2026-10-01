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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api import admin as admin_api
from app.api import auth as auth_api
from app.api import messages as messages_api
from app.api import posts as posts_api
from app.api import ws as ws_api
from app.config import Settings, get_settings
from app.errors import AppError
from app.services.cleanup import run_cleanup
from app.state import AppState

log = logging.getLogger("dzplay")
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

# Don't depend on the host's /etc/mime.types (ES modules require a JS MIME type).
for _ext, _type in {".js": "text/javascript", ".webmanifest": "application/manifest+json", ".woff2": "font/woff2",
                    ".svg": "image/svg+xml", ".css": "text/css"}.items():
    mimetypes.add_type(_type, _ext)
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
MAX_BODY_BYTES = 64 * 1024  # API bodies are tiny JSON documents


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
        "img-src": "'self' data:",
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
                    run_cleanup(db, state.settings)

            await run_in_threadpool(_run)
        except Exception:  # noqa: BLE001 - keep the loop alive
            log.exception("cleanup failed")
        await asyncio.sleep(state.settings.CLEANUP_INTERVAL)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    state = AppState.build(settings)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        state.database.create_all()
        if settings.secret_key_generated:
            log.warning("SECRET_KEY not set: using a temporary key (sessions survive, IP blocks reset on restart).")
        await state.hub.start()
        cleanup_task = asyncio.create_task(_cleanup_loop(state)) if settings.CLEANUP_INTERVAL > 0 else None
        yield
        if cleanup_task:
            cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cleanup_task
        await state.hub.stop()
        state.push.shutdown()

    app = FastAPI(title="DZPLAY", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.dz = state
    csp = _csp(settings)

    @app.middleware("http")
    async def security_middleware(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/") and int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return _error(413, "too_large", "الطلب كبير جدًا.")
        if path.startswith("/api/") and request.method not in _SAFE_METHODS:
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
        h["Content-Security-Policy"] = csp
        if settings.COOKIE_SECURE:
            h["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith("/api/"):
            h["Cache-Control"] = "no-store"
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
    app.include_router(posts_api.router)
    app.include_router(admin_api.router)
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

        @app.get("/admin", include_in_schema=False)
        def admin_page() -> FileResponse:
            """Owner dashboard (works only with ADMIN_API_TOKEN; never linked from the app)."""
            if not settings.ADMIN_API_TOKEN:
                raise StarletteHTTPException(404)
            return FileResponse(STATIC_DIR / "admin.html", headers={"X-Robots-Tag": "noindex, nofollow"})

        app.mount("/", StaticFiles(directory=STATIC_DIR), name="static")
    return app


app = create_app()
