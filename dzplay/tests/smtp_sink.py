"""A real local SMTP server for tests (aiosmtpd): messages are parsed and kept in memory."""

from __future__ import annotations

import socket
from email import message_from_bytes, policy

from aiosmtpd.controller import Controller


class _Handler:
    def __init__(self):
        self.messages = []

    async def handle_DATA(self, server, session, envelope):  # noqa: N802 - aiosmtpd hook name
        msg = message_from_bytes(envelope.content, policy=policy.default)
        self.messages.append({"to": envelope.rcpt_tos, "from": envelope.mail_from, "msg": msg})
        return "250 OK"


class SmtpSink:
    def __init__(self):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.handler = _Handler()
        self.controller = Controller(self.handler, hostname="127.0.0.1", port=self.port)

    def __enter__(self):
        self.controller.start()
        return self

    def __exit__(self, *exc):
        self.controller.stop()

    @property
    def messages(self):
        return self.handler.messages

    def last_text(self) -> str:
        return self.messages[-1]["msg"].get_body(preferencelist=("plain",)).get_content()

    def last_html(self) -> str:
        return self.messages[-1]["msg"].get_body(preferencelist=("html",)).get_content()
