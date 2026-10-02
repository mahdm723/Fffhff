"""Minimal Telegram Bot API client.

Security rules:
* The bot token is only ever part of the request URL to TELEGRAM_API_BASE; it
  is redacted from every error message and httpx's own request logging is
  silenced (app.main), so it never reaches the logs or the client.
* Files are downloaded only from TELEGRAM_API_BASE/file/bot<token>/<file_path>
  where file_path comes from getFile and must match a strict pattern
  (no "..", no scheme, no host) — the server never fetches a URL taken from a
  message (no SSRF).
* Downloads are streamed with a hard size cap.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import httpx

from app.config import Settings

log = logging.getLogger("dzplay.telegram")
_FILE_PATH = re.compile(r"^[A-Za-z0-9_\-]+(?:/[A-Za-z0-9_\-.]+){0,3}$")


class TelegramError(Exception):
    pass


class FileTooLarge(TelegramError):
    pass


class TelegramClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self._token = settings.TELEGRAM_BOT_TOKEN
        self._base = settings.TELEGRAM_API_BASE.rstrip("/")
        self.max_file_bytes = settings.TELEGRAM_MAX_FILE_MB * 1024 * 1024
        self._http = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0), transport=transport,
                                  follow_redirects=False, trust_env=True)

    # -------------------------------------------------------------- helpers
    def redact(self, text: object) -> str:
        out = str(text)
        return out.replace(self._token, "<token>") if self._token else out

    def call(self, method: str, **params) -> dict | list | bool:
        payload = {k: v for k, v in params.items() if v is not None}
        try:
            res = self._http.post(f"{self._base}/bot{self._token}/{method}", json=payload)
            data = res.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise TelegramError(self.redact(f"{method}: {type(exc).__name__}: {exc}")) from None
        if not data.get("ok"):
            raise TelegramError(self.redact(f"{method}: {data.get('error_code')} {data.get('description')}"))
        return data.get("result")

    # -------------------------------------------------------------- messages
    def send_message(self, chat_id: str | int, text: str, *, reply_markup: dict | None = None,
                     reply_to: int | None = None) -> dict:
        params = {"chat_id": chat_id, "text": text[:4000], "reply_markup": reply_markup,
                  "disable_web_page_preview": True}
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
        return self.call("sendMessage", **params)

    def answer_callback(self, callback_id: str, text: str = "") -> None:
        self.call("answerCallbackQuery", callback_query_id=callback_id, text=text[:190])

    def edit_reply_markup(self, chat_id: str | int, message_id: int, reply_markup: dict | None = None) -> None:
        self.call("editMessageReplyMarkup", chat_id=chat_id, message_id=message_id,
                  reply_markup=reply_markup or {"inline_keyboard": []})

    def set_webhook(self, url: str, secret: str) -> None:
        self.call("setWebhook", url=url, secret_token=secret, drop_pending_updates=False,
                  allowed_updates=["message", "callback_query"], max_connections=10)

    def webhook_info(self) -> dict:
        return self.call("getWebhookInfo")

    # -------------------------------------------------------------- files
    def download(self, file_id: str, dest: Path) -> int:
        """Download a file by file_id to `dest`; returns its size. Raises FileTooLarge over the cap."""
        info = self.call("getFile", file_id=file_id)
        size = int(info.get("file_size") or 0)
        if size > self.max_file_bytes:
            raise FileTooLarge(f"{size} bytes")
        file_path = str(info.get("file_path") or "")
        if not _FILE_PATH.match(file_path) or ".." in file_path:
            raise TelegramError("unexpected file_path from getFile")
        written = 0
        try:
            with self._http.stream("GET", f"{self._base}/file/bot{self._token}/{file_path}") as res:
                if res.status_code != 200:
                    raise TelegramError(f"file download: HTTP {res.status_code}")
                with open(dest, "wb") as fh:
                    for chunk in res.iter_bytes(64 * 1024):
                        written += len(chunk)
                        if written > self.max_file_bytes:
                            raise FileTooLarge(f">{self.max_file_bytes} bytes")
                        fh.write(chunk)
        except httpx.HTTPError as exc:
            raise TelegramError(self.redact(f"file download: {type(exc).__name__}")) from None
        return written

    def close(self) -> None:
        self._http.close()
