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

from app import i18n
from app.api.deps import get_state
from app.config import Settings
from app.services import brand, cms

router = APIRouter(tags=["policies"])

MAIN = ("privacy", "terms", "guidelines", "verification")


def principle(settings: Settings, lang: str | None = None) -> str:
    return cms.principle(settings, lang)


def _pages(lang: str | None = None) -> dict[str, str]:
    return cms.page_titles(lang)


QUOTES = {"ar": ("«", "»"), "fr": ("« ", " »"), "en": ("“", "”")}


def lang_switch(path: str, lang: str) -> str:
    """Links to the same page in the other languages (pages without JavaScript)."""
    return " · ".join(f'<a href="{path}?lang={code}" hreflang="{code}" lang="{code}">{html.escape(name)}</a>'
                      for code, name in i18n.NAMES.items() if code != lang)


def _page(settings: Settings, title: str, inner: str, updated: str | None, *, lang: str, path: str,
          body_lang: str | None = None) -> str:
    tr = i18n.tr
    pages = _pages(lang)
    nav = (" · ".join(f'<a href="/policies/{k}">{html.escape(pages[k])}</a>' for k in MAIN)
           + f' · <a href="/policies">{html.escape(tr("المزيد", lang))}</a>')
    app = html.escape(settings.APP_NAME)
    meta = f'<p class="dl-meta">{html.escape(tr("آخر تحديث: {date}", lang, date=updated))}</p>' if updated else ""
    q1, q2 = QUOTES[lang]
    body_lang = body_lang or lang
    body_attrs = "" if body_lang == lang else f' lang="{body_lang}" dir="{i18n.direction(body_lang)}"'
    return f"""<!doctype html>
<html lang="{lang}" dir="{i18n.direction(lang)}">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>{html.escape(title)} — {app}</title>
  <meta name="referrer" content="no-referrer">
  {brand.HEAD_ICONS}
  <link rel="stylesheet" href="/css/download.css">
  <link rel="stylesheet" href="/css/policies.css">
</head>
<body>
  <main class="dl policy">
    <header class="dl-head"><a href="/"><img class="dl-icon" src="/icons/icon-192.png" width="64" height="64" alt="{app}"></a>
      <h1>{html.escape(title)}</h1></header>
    <blockquote class="policy-principle">{q1}{html.escape(principle(settings, lang))}{q2}</blockquote>
    <article class="dl-card policy-body"{body_attrs}>{inner}</article>
    {meta}
    <nav class="policy-nav">{nav}</nav>
    <p class="dl-foot"><a href="/">{html.escape(tr("العودة إلى {app}", lang, app=settings.APP_NAME))}</a> · <a href="/download">{html.escape(tr("تحميل التطبيق", lang))}</a></p>
    <p class="dl-foot dl-langs">{lang_switch(path, lang)}</p>
  </main>
</body>
</html>"""


def respond(request: Request, page: str) -> HTMLResponse:
    """The page; a language picked with ?lang= is remembered (dz_lang cookie) for the next pages and the app."""
    from app.api.i18n import set_language_cookie

    out = HTMLResponse(page, headers={"Cache-Control": "no-cache", "Vary": "Cookie, Accept-Language"})
    picked = i18n.normalize(request.query_params.get("lang"))
    if picked and picked != i18n.normalize(request.cookies.get("dz_lang")):
        set_language_cookie(out, request, picked)
    return out


@router.get("/policies", include_in_schema=False)
def policies_index(request: Request) -> HTMLResponse:
    st = get_state(request)
    lang = i18n.current()
    items = "".join(f'<li><a href="/policies/{k}">{html.escape(v)}</a></li>' for k, v in _pages(lang).items())
    return respond(request, _page(st.settings, i18n.tr("السياسات والشروط", lang), f"<ul>{items}</ul>", None, lang=lang,
                                  path="/policies"))


@router.get("/policies/{slug}", include_in_schema=False)
def policy(slug: str, request: Request) -> HTMLResponse:
    if slug not in _pages():
        raise HTTPException(404)
    st = get_state(request)
    lang = i18n.current()
    with st.database.session() as db:
        cur = cms.current(db, st.settings, slug, lang)
        inner = cms.page_html(db, st.settings, slug, cur=cur)
    body_lang = i18n.DEFAULT if cur["missing"] else lang
    return respond(request, _page(st.settings, cur["title"], inner, cms.updated_label(cur), lang=lang,
                                  path=f"/policies/{slug}", body_lang=body_lang))
