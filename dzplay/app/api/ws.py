from __future__ import annotations

import asyncio
import contextlib
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app.security.sessions import resolve_session

router = APIRouter()

PING_INTERVAL = 25


def _origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return False
    st = ws.app.state.dz
    host = ws.headers.get("host", "")
    if urlsplit(origin).netloc == host:
        return True
    return origin.rstrip("/") in st.settings.allowed_origins


@router.websocket("/api/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    st = ws.app.state.dz
    if not _origin_allowed(ws):  # blocks cross-site WebSocket hijacking
        await ws.close(code=4403)
        return
    token = ws.cookies.get(st.settings.SESSION_COOKIE_NAME)

    def _resolve() -> str | None:
        with st.database.session() as db:
            user = resolve_session(db, st.settings, token)
            return user.id if user else None

    user_id = await run_in_threadpool(_resolve)
    if user_id is None:
        await ws.close(code=4401)
        return

    await ws.accept()
    queue = st.hub.connect(user_id)

    async def reader() -> None:
        # Client messages are only keep-alives; reading also detects disconnects.
        while True:
            await ws.receive_text()

    reader_task = asyncio.create_task(reader())
    try:
        await ws.send_json({"type": "hello"})
        while True:
            # Wake up on a new event, on disconnect, or every PING_INTERVAL for a keep-alive.
            get_task = asyncio.ensure_future(queue.get())
            done, _ = await asyncio.wait({get_task, reader_task}, timeout=PING_INTERVAL,
                                         return_when=asyncio.FIRST_COMPLETED)
            if reader_task in done:
                get_task.cancel()
                break
            if get_task in done:
                event = get_task.result()
            else:
                get_task.cancel()
                event = {"type": "ping"}
            await ws.send_json(event)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        reader_task.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect, Exception):
            await reader_task
        st.hub.disconnect(user_id, queue)
