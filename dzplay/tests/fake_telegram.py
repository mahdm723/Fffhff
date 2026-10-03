"""A local fake of the Telegram Bot API (api.telegram.org is unreachable from CI).

It speaks the same HTTP API the real one does for the methods we use
(sendMessage, getFile, file download, answerCallbackQuery, editMessageReplyMarkup,
setWebhook), records what the bot sends, and serves file bytes registered by
the tests. Plugged in through httpx.MockTransport.
"""

from __future__ import annotations

import itertools
import json

import httpx

TOKEN = "123456:TEST-token-never-logged"
ADMIN_ID = 1001
STRANGER_ID = 2002


class FakeTelegram:
    def __init__(self, token: str = TOKEN):
        self.token = token
        self.files: dict[str, tuple[bytes, int | None]] = {}  # file_id -> (content, reported size)
        self.sent: list[dict] = []
        self.calls: list[str] = []
        self.downloads: list[str] = []
        self.fail_methods: set[str] = set()
        self._ids = itertools.count(1)
        self.transport = httpx.MockTransport(self._handle)

    # ------------------------------------------------------------- test helpers
    def add_file(self, content: bytes, *, reported_size: int | None = None, name: str = "file") -> str:
        file_id = f"{name}-{next(self._ids)}-AgAD"
        self.files[file_id] = (content, reported_size)
        return file_id

    def texts(self) -> list[str]:
        return [m.get("text", "") for m in self.sent]

    def last_text(self) -> str:
        return self.sent[-1]["text"] if self.sent else ""

    # ------------------------------------------------------------- API
    def _ok(self, result) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": result})

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith(f"/file/bot{self.token}/"):
            file_path = path[len(f"/file/bot{self.token}/"):]
            file_id = file_path.split("/", 1)[1].rsplit(".", 1)[0]
            if file_id not in self.files:
                return httpx.Response(404)
            self.downloads.append(file_id)
            return httpx.Response(200, content=self.files[file_id][0])
        prefix = f"/bot{self.token}/"
        if not path.startswith(prefix):
            return httpx.Response(401, json={"ok": False, "error_code": 401, "description": "Unauthorized"})
        method = path[len(prefix):]
        self.calls.append(method)
        body = json.loads(request.content or b"{}")
        if method in self.fail_methods:
            return httpx.Response(400, json={"ok": False, "error_code": 400, "description": f"Bad Request: {method} failed"})
        if method == "sendMessage":
            self.sent.append(body)
            return self._ok({"message_id": next(self._ids), "chat": {"id": body.get("chat_id")}, "text": body.get("text")})
        if method == "getFile":
            file_id = body.get("file_id")
            if file_id not in self.files:
                return httpx.Response(400, json={"ok": False, "error_code": 400, "description": "Bad Request: invalid file_id"})
            content, reported = self.files[file_id]
            size = reported if reported is not None else len(content)
            return self._ok({"file_id": file_id, "file_unique_id": file_id[:8], "file_size": size,
                             "file_path": f"documents/{file_id}.bin"})
        if method == "setWebhook":
            self.webhook = body
            return self._ok(True)
        if method in ("answerCallbackQuery", "editMessageReplyMarkup"):
            return self._ok(True)
        if method == "deleteWebhook":
            self.webhook = None
            return self._ok(True)
        if method == "getMe":
            return self._ok({"id": int(self.token.split(":")[0]), "is_bot": True, "username": "dzplay_test_bot"})
        if method == "getWebhookInfo":
            hook = getattr(self, "webhook", None) or {}
            return self._ok({"url": hook.get("url", ""), "pending_update_count": 0})
        return httpx.Response(404, json={"ok": False, "error_code": 404, "description": "Not Found"})


# ----------------------------------------------------------------- update builders

_update_ids = itertools.count(1000)
_message_ids = itertools.count(1)


def _base(sender: int = ADMIN_ID, chat: int | None = None) -> dict:
    return {"message_id": next(_message_ids), "date": 1700000000,
            "chat": {"id": chat if chat is not None else sender, "type": "private"},
            "from": {"id": sender, "is_bot": False, "first_name": "Admin"}}


def update(message: dict) -> dict:
    return {"update_id": next(_update_ids), "message": message}


def text(cmd: str, sender: int = ADMIN_ID, chat: int | None = None) -> dict:
    return update({**_base(sender, chat), "text": cmd})


def video(file_id: str, size: int, caption: str | None = None, sender: int = ADMIN_ID, width=360, height=640) -> dict:
    msg = {**_base(sender), "video": {"file_id": file_id, "file_unique_id": file_id[:8], "width": width,
                                      "height": height, "duration": 2, "file_size": size, "mime_type": "video/mp4"}}
    if caption:
        msg["caption"] = caption
    return update(msg)


def photo(file_id: str, size: int, caption: str | None = None, group: str | None = None, sender: int = ADMIN_ID,
          message_id: int | None = None) -> dict:
    msg = {**_base(sender), "photo": [
        {"file_id": "thumb-" + file_id, "file_unique_id": "t" + file_id[:6], "width": 90, "height": 160, "file_size": 1000},
        {"file_id": file_id, "file_unique_id": file_id[:8], "width": 720, "height": 1280, "file_size": size},
    ]}
    if message_id is not None:
        msg["message_id"] = message_id
    if caption:
        msg["caption"] = caption
    if group:
        msg["media_group_id"] = group
    return update(msg)


def callback(data: str, sender: int = ADMIN_ID, message_id: int = 1) -> dict:
    return {"update_id": next(_update_ids), "callback_query": {
        "id": f"cb{next(_update_ids)}", "from": {"id": sender, "is_bot": False, "first_name": "Admin"},
        "message": {"message_id": message_id, "chat": {"id": sender, "type": "private"}}, "data": data}}
