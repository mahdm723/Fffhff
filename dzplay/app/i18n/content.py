"""V6 phase 12: the French and English versions of the content system's default texts (services/cms.py) — policy
pages, short texts and e-mails — shipped with the code in content_fr.py / content_en.py:

    CONTENT = {key: (title, body)}   # title: the page title or the e-mail subject (None for short texts)
    PRINCIPLE = "…"                   # the one-line principle shown on the policy pages

The same {{variables}} (pages, texts) and {variables} (e-mails) as the Arabic texts. An edit made in the panel
(content_translations) always wins over these.
"""

from __future__ import annotations

from functools import lru_cache
from importlib import import_module


@lru_cache(maxsize=None)
def _module(lang: str):
    if lang not in ("fr", "en"):
        return None
    try:
        return import_module(f"app.i18n.content_{lang}")
    except ModuleNotFoundError:
        return None


def default(key: str, lang: str) -> tuple[str | None, str] | None:
    mod = _module(lang)
    return getattr(mod, "CONTENT", {}).get(key) if mod is not None else None


def principle(lang: str) -> str | None:
    mod = _module(lang)
    return getattr(mod, "PRINCIPLE", None) if mod is not None else None
