"""V6 phase 8: a small Markdown subset for the editable texts (policies, terms, help pages, notices).

Everything is HTML-escaped FIRST; only these are turned into markup afterwards:
  # / ## / ###  headings          - item / * item  bullet list        1. item  numbered list
  **bold**  *italic*              [text](https://…) links open in a new tab; [text](/policies/terms) site links
  a blank line separates paragraphs; a single line break stays a line break.
Nothing else is recognised: no raw HTML, no images, no other link schemes (javascript:, data:, http:, //host…).
No external library: the output is predictable and the same converter renders the panel preview.
"""

from __future__ import annotations

import html
import re
import unicodedata

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")
_HEADING = re.compile(r"^(#{1,3})\s+(.+)$")
_BULLET = re.compile(r"^[-*]\s+(.+)$")
_NUMBER = re.compile(r"^\d{1,3}[.)]\s+(.+)$")
_LINK = re.compile(r"\[([^\[\]\n]{1,300})\]\((https://[^\s()<>\"']{1,1000}|/(?![/\\])[A-Za-z0-9/_.#?=%-]{0,300})\)")
_BOLD = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_ITALIC = re.compile(r"(?<!\*)\*(?=\S)([^*\n]+?)(?<=\S)\*(?!\*)")  # also inside a word (و*مائل*)

MAX_LENGTH = 50_000


def clean(text: object) -> str:
    """Normalised source text: NFC, \\n line endings, no control or direction-override characters."""
    if not isinstance(text, str):
        return ""
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)[:MAX_LENGTH]


def _inline(escaped: str) -> str:
    """Links, bold, italic — applied to text that is already HTML-escaped."""
    links: list[str] = []

    def keep_link(m: re.Match) -> str:
        url = m.group(2)  # escaped already (&amp; …); no quotes or spaces can be in it (regex)
        if url.startswith("/"):  # a page of this site
            links.append(f'<a href="{url}">{m.group(1)}</a>')
        else:
            links.append(f'<a href="{url}" target="_blank" rel="noopener noreferrer nofollow">{m.group(1)}</a>')
        return f"\x00{len(links) - 1}\x00"

    out = _LINK.sub(keep_link, escaped)
    out = _BOLD.sub(r"<strong>\1</strong>", out)
    out = _ITALIC.sub(r"<em>\1</em>", out)
    return re.sub(r"\x00(\d+)\x00", lambda m: links[int(m.group(1))], out)


def render(text: object) -> str:
    """Safe HTML for the given Markdown subset."""
    src = clean(text)
    blocks: list[str] = []
    para: list[str] = []
    items: list[str] = []
    kind = ""  # current list: ul | ol

    def flush_para() -> None:
        if para:
            blocks.append("<p>" + "<br>".join(_inline(html.escape(line)) for line in para) + "</p>")
            para.clear()

    def flush_list() -> None:
        nonlocal kind
        if items:
            blocks.append(f"<{kind}>" + "".join(f"<li>{i}</li>" for i in items) + f"</{kind}>")
            items.clear()
        kind = ""

    for raw in src.split("\n"):
        line = raw.strip()
        if not line:
            flush_para()
            flush_list()
            continue
        m = _HEADING.match(line)
        if m:
            flush_para()
            flush_list()
            level = len(m.group(1)) + 1  # # → h2 (the page title is the h1)
            blocks.append(f"<h{level}>{_inline(html.escape(m.group(2).strip()))}</h{level}>")
            continue
        bullet, number = _BULLET.match(line), _NUMBER.match(line)
        if bullet or number:
            flush_para()
            want = "ul" if bullet else "ol"
            if kind and kind != want:
                flush_list()
            kind = want
            items.append(_inline(html.escape((bullet or number).group(1).strip())))
            continue
        flush_list()
        para.append(line)
    flush_para()
    flush_list()
    return "\n".join(blocks)


def plain(text: object) -> str:
    """The text without Markdown marks (for e-mails, Telegram, previews)."""
    src = clean(text)
    src = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})", src)
    src = re.sub(r"^#{1,3}\s+", "", src, flags=re.M)
    src = src.replace("**", "")
    return _ITALIC.sub(r"\1", src)
