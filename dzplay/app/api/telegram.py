"""Telegram webhook. Authenticated by the secret token Telegram sends in
X-Telegram-Bot-Api-Secret-Token (set with setWebhook); exempt from the CSRF
header check for that reason only. Updates are queued and handled in the
background; the response is immediate so Telegram never retries needlessly.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.api.deps import get_state
from app.security.crypto import constant_time_equals

router = APIRouter(tags=["telegram"])


@router.post("/api/telegram/webhook", include_in_schema=False)
async def webhook(request: Request) -> JSONResponse:
    st = get_state(request)
    secret = st.telegram_secret  # .env or admin panel (AppState.configure_telegram)
    if st.bot is None or not secret:
        return JSONResponse({"error": {"code": "not_found"}}, status_code=404)
    if not constant_time_equals(request.headers.get("x-telegram-bot-api-secret-token", ""), secret):
        return JSONResponse({"error": {"code": "forbidden"}}, status_code=403)
    try:
        update = await request.json()
    except ValueError:
        return JSONResponse({"ok": True})
    if isinstance(update, dict):
        st.bot.submit(update)
    return JSONResponse({"ok": True})
