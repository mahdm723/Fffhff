"""Message content validation and normalisation (text only).

Messages are stored as plain text and always rendered with `textContent` on the
client, so nothing can execute. On top of that the server:

* normalises Unicode (NFC) and line endings,
* strips control characters and invisible direction-override characters used
  for spoofing (keeps ZWJ/ZWNJ needed by emoji and some scripts),
* rejects HTML markup and (by default) links,
* enforces length limits.
"""

from __future__ import annotations

import re
import unicodedata

from app.errors import AppError

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_INVISIBLE = re.compile("[​⁠﻿\u202a-\u202e\u2066-\u2069]")
_MANY_NEWLINES = re.compile(r"\n{3,}")
_HTML_TAG = re.compile(r"<\s*/?\s*[a-zA-Z!][^>]*>")
_URL = re.compile(
    r"(?ix)("
    r"\b(?:https?|ftp|file|javascript|data|vbscript|mailto|tel|intent)\s*:"
    r"|\bwww\d{0,3}\."
    r"|\b[a-z0-9][a-z0-9-]{0,62}(?:\.[a-z0-9-]{1,63})*\."
    r"(?:com|net|org|io|me|co|ly|gg|xyz|info|app|dev|link|site|online|top|ru|tk|ml|ga|cf|gq|"
    r"dz|sa|eg|ma|tn|ae|fr|uk|de|us|tv|biz|cc|shop|store|live|click|club)\b"
    r"|\bt\.me/"
    r")"
)


def clean_message(raw: object, max_length: int, link_policy: str) -> str:
    if not isinstance(raw, str):
        raise AppError(400, "invalid_content", "الرسالة يجب أن تكون نصًا.")
    text = unicodedata.normalize("NFC", raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", " ")
    text = _CONTROL.sub("", text)
    text = _INVISIBLE.sub("", text)
    text = _MANY_NEWLINES.sub("\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()

    if not text:
        raise AppError(400, "empty_message", "لا يمكن إرسال رسالة فارغة.")
    if len(text) > max_length:
        raise AppError(400, "message_too_long", f"الرسالة أطول من الحد المسموح ({max_length} حرف).")
    if _HTML_TAG.search(text):
        raise AppError(400, "html_not_allowed", "لا يُسمح بإرسال أكواد HTML. أرسل نصًا عاديًا فقط.")
    if link_policy == "reject" and _URL.search(text):
        raise AppError(400, "links_not_allowed", "لا يُسمح بإرسال الروابط في الرسائل.")
    return text


def preview(text: str, length: int = 120) -> str:
    one_line = " ".join(text.split())
    return one_line if len(one_line) <= length else one_line[: length - 1] + "…"
