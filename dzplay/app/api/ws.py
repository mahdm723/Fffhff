from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from app import clock
from app.models import Conversation
from app.security.sessions import resolve_session

router = APIRouter()
log = logging.getLogger("dzplay.ws")

PING_INTERVAL = 25
_PEER_CACHE_SECONDS = 30


def _origin_allowed(ws: WebSocket) -> bool:
    origin = ws.headers.get("origin")
    if not origin:
        return False
    st = ws.app.state.dz
    host = ws.headers.get("host", "")
    if urlsplit(origin).netloc == host:
        return True
    return origin.rstrip("/") in st.settings.allowed_origins


class Connection:
    """One authenticated socket. Client → server messages are small JSON objects
    ({"type": ...}); everything else closes the socket. Every handler re-checks
    authorization server-side: the client is never trusted."""

    def __init__(self, ws: WebSocket, user_id: str, token: str | None = None) -> None:
        self.ws = ws
        self.token = token
        self.st = ws.app.state.dz
        self.user_id = user_id
        self._window = (0.0, 0)  # (start, count) for WS_MSGS_PER_10S
        self._typing: list[float] = []
        self._peers: dict[str, tuple[float, str | None]] = {}

    def allow(self) -> bool:
        now = time.monotonic()
        start, count = self._window
        if now - start >= 10:
            start, count = now, 0
        self._window = (start, count + 1)
        return count + 1 <= self.st.settings.WS_MSGS_PER_10S

    async def chat_peer(self, conversation_id: str) -> str | None:
        """The other member of an active conversation of mine that may receive live
        signals from me (None otherwise). Cached briefly per connection."""
        now = time.monotonic()
        hit = self._peers.get(conversation_id)
        if hit and now - hit[0] < _PEER_CACHE_SECONDS:
            return hit[1]

        def _lookup() -> str | None:
            with self.st.database.session() as db:
                c = db.get(Conversation, conversation_id)
                if (c is None or not c.is_participant(self.user_id) or c.status != "active"
                        or c.expires_at <= clock.utcnow()):
                    return None
                peer = c.peer_of(self.user_id)
                if c.hidden_for(self.user_id) or c.hidden_for(peer):
                    return None
                if c.is_direct and (c.request_state or "accepted") != "accepted" and self.user_id != c.recipient_id:
                    return None  # an unanswered request: the recipient sees nothing live
                return peer

        peer = await run_in_threadpool(_lookup)
        if len(self._peers) > 200:
            self._peers.clear()
        self._peers[conversation_id] = (now, peer)
        return peer


async def _typing(conn: Connection, data: dict) -> None:
    cid, on = data.get("conversation_id"), data.get("on")
    if not isinstance(cid, str) or not 0 < len(cid) <= 32 or not isinstance(on, bool):
        return
    now = time.monotonic()
    conn._typing = [t for t in conn._typing if now - t < 60]
    if len(conn._typing) >= conn.st.settings.TYPING_EVENTS_PER_MINUTE:
        return
    conn._typing.append(now)
    peer = await conn.chat_peer(cid)
    if peer:
        conn.st.hub.notify([peer], {"type": "typing", "conversation_id": cid, "on": on})


async def _noop(conn: Connection, data: dict) -> None:
    return None


async def _call(conn: Connection, data: dict) -> None:
    """Call signaling (accept / decline / hangup / SDP / ICE / audio↔video / quality).
    The session is re-checked on every message: a revoked session or a banned account stops at once."""
    from app.services import calls
    from app.services.messaging import Effects

    effects = Effects()

    def _run() -> None:
        with conn.st.database.session() as db:
            user = resolve_session(db, conn.st.settings, conn.token)
            if user is None or user.id != conn.user_id:
                return
            calls.handle_signal(db, conn.st.settings, user, data, effects)

    await run_in_threadpool(_run)
    conn.st.dispatch(effects)


# type -> handler. Unknown types are ignored (forward compatible clients).
HANDLERS: dict[str, Callable[[Connection, dict], Awaitable[None]]] = {
    "ping": _noop,
    "typing": _typing,
    **{t: _call for t in ("call.ringing", "call.accept", "call.decline", "call.hangup", "call.sdp", "call.ice",
                          "call.media", "call.quality")},
}


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
    conn = Connection(ws, user_id, token)

    async def reader() -> None:
        # Reading also detects disconnects. Any abuse closes the socket.
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                return
            text = msg.get("text")
            if text is None:
                await ws.close(code=1003)  # binary frames are not accepted
                return
            if len(text.encode()) > st.settings.WS_MAX_MESSAGE_BYTES:
                await ws.close(code=1009)
                return
            if not conn.allow():
                await ws.close(code=1008)
                return
            try:
                data = json.loads(text)
            except ValueError:
                continue
            if not isinstance(data, dict) or not isinstance(data.get("type"), str):
                continue
            handler = HANDLERS.get(data["type"])
            if handler is not None:
                try:
                    await handler(conn, data)
                except Exception:  # noqa: BLE001 - one bad message never kills the socket
                    log.exception("ws handler %s failed", data["type"][:20])

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
