"""Policy and help pages: privacy, terms, guidelines, membership / rewards / withdrawal / red envelope terms,
blue star, about, FAQ, contact.

V6 phase 8: the texts come from the content system (services/cms.py) — editable from the panel, with
{{variables}} filled from the running settings so they always match what the app really does. Server-rendered
(no JavaScript, CSP-safe like /download); the Markdown converter escapes all HTML. No personal information about
the owner appears anywhere.
"""

from __future__ import annotations

import html

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException

from app.api.deps import get_state
from app.config import Settings
from app.services import cms

router = APIRouter(tags=["policies"])

MAIN = ("privacy", "terms", "guidelines", "verification")


def principle(settings: Settings) -> str:
    return cms.principle(settings)


def _pages() -> dict[str, str]:
    return {k: s.title for k, s in cms.registry().items() if s.kind == "page"}


def _page(settings: Settings, title: str, inner: str, updated: str | None) -> str:
    pages = _pages()
    nav = " · ".join(f'<a href="/policies/{k}">{html.escape(pages[k])}</a>' for k in MAIN) + ' · <a href="/policies">المزيد</a>'
    app = html.escape(settings.APP_NAME)
    meta = f'<p class="dl-meta">آخر تحديث: {html.escape(updated)}</p>' if updated else ""
    return f"""<!doctype html>
<html lang="ar" dir="rtl">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{html.escape(title)} — {app}</title>
  <meta name="referrer" content="no-referrer">
  <link rel="icon" href="/icons/icon-192.png">
  <link rel="stylesheet" href="/css/download.css">
  <link rel="stylesheet" href="/css/policies.css">
</head>
<body>
  <main class="dl policy">
    <header class="dl-head"><a href="/"><img class="dl-icon" src="/icons/icon-192.png" width="64" height="64" alt="{app}"></a>
      <h1>{html.escape(title)}</h1></header>
    <blockquote class="policy-principle">«{html.escape(principle(settings))}»</blockquote>
    <article class="dl-card policy-body">{inner}</article>
    {meta}
    <nav class="policy-nav">{nav}</nav>
    <p class="dl-foot"><a href="/">العودة إلى {app}</a> · <a href="/download">تحميل التطبيق</a></p>
  </main>
</body>
</html>"""


@router.get("/policies", include_in_schema=False)
def policies_index(request: Request) -> HTMLResponse:
    st = get_state(request)
    items = "".join(f'<li><a href="/policies/{k}">{html.escape(v)}</a></li>' for k, v in _pages().items())
    return HTMLResponse(_page(st.settings, "السياسات والشروط", f"<ul>{items}</ul>", None), headers={"Cache-Control": "no-cache"})


@router.get("/policies/{slug}", include_in_schema=False)
def policy(slug: str, request: Request) -> HTMLResponse:
    if slug not in _pages():
        raise HTTPException(404)
    st = get_state(request)
    with st.database.session() as db:
        cur = cms.current(db, st.settings, slug)
        inner = cms.page_html(db, st.settings, slug)
    return HTMLResponse(_page(st.settings, cur["title"], inner, cms.updated_label(cur)), headers={"Cache-Control": "no-cache"})
