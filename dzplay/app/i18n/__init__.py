"""V6 phase 12: three languages — Arabic (the original, and the fallback), French and English.

The Arabic text written in the code is the key (msgid, gettext style): `t('حساب جديد')` in the interface,
`tr('غير موجود.')` on the server. A missing translation shows the Arabic text, never a technical key.

* Defaults shipped with the code: app/i18n/fr.json, en.json ({msgid: text}); ar.json only holds the Arabic plural
  forms. A text may be a plural: {"one": …, "other": …} (CLDR categories), chosen with the `n` parameter.
* Edits from the panel («اللغات والنصوص»): table i18n_texts, cached here (app.i18n.store), applied at once.
* Variables are written {name} and filled with a regex (never str.format: a translator cannot reach attributes).
* The current language of a request: X-DZ-Lang (sent by the app) → the dz_lang cookie → Accept-Language → Arabic
  (main.security_middleware). Mails and notifications for someone else pass the recipient's language explicitly.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

LANGS = ("ar", "fr", "en")
DEFAULT = "ar"
DIRS = {"ar": "rtl", "fr": "ltr", "en": "ltr"}
NAMES = {"ar": "العربية", "fr": "Français", "en": "English"}
LOCALES = {"ar": "ar-DZ", "fr": "fr-DZ", "en": "en-GB"}  # numbers and dates (ar-DZ keeps the Latin digits)
HERE = Path(__file__).resolve().parent
_VAR = re.compile(r"\{(\w+)\}")

_current: ContextVar[str] = ContextVar("dz_lang", default=DEFAULT)
# panel edits: {lang: {msgid: text}} (filled by app.i18n.store at start-up and after every change)
OVERRIDES: dict[str, dict[str, object]] = {lang: {} for lang in LANGS}


def normalize(value: str | None) -> str | None:
    """'fr-FR' → 'fr'; anything not offered → None."""
    if not value:
        return None
    code = value.strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGS else None


def from_accept_language(header: str | None) -> str | None:
    """The best offered language of an Accept-Language header (by its q weights)."""
    best: tuple[float, str] | None = None
    for i, part in enumerate((header or "").split(",")):
        bits = part.strip().split(";")
        lang = normalize(bits[0])
        if not lang:
            continue
        q = 1.0
        for b in bits[1:]:
            if b.strip().startswith("q="):
                try:
                    q = float(b.strip()[2:])
                except ValueError:
                    q = 0.0
        key = (q - i * 1e-6, lang)
        if best is None or key > best:
            best = key
    return best[1] if best else None


def set_current(lang: str | None) -> None:
    _current.set(normalize(lang) or DEFAULT)


def current() -> str:
    return _current.get()


def direction(lang: str | None = None) -> str:
    return DIRS.get(normalize(lang) or current(), "rtl")


@lru_cache(maxsize=None)
def defaults(lang: str) -> dict[str, object]:
    path = HERE / f"{lang}.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def N_(msgid: str) -> str:
    """Marks a text for extraction without translating it now (labels kept in dicts, translated when sent)."""
    return msgid


def plural_category(lang: str, n: float) -> str:
    """CLDR plural categories for our three languages."""
    try:
        n = abs(float(n))
    except (TypeError, ValueError):
        return "other"
    whole = n == int(n)
    i = int(n)
    if lang == "ar":
        if not whole:
            return "other"
        if i == 0:
            return "zero"
        if i == 1:
            return "one"
        if i == 2:
            return "two"
        if 3 <= i % 100 <= 10:
            return "few"
        if 11 <= i % 100 <= 99:
            return "many"
        return "other"
    if lang == "fr":
        return "one" if i in (0, 1) else "other"
    return "one" if n == 1 else "other"


def fill(text: str, params: dict) -> str:
    if not params:
        return text
    return _VAR.sub(lambda m: str(params[m.group(1)]) if m.group(1) in params else m.group(0), text)


def lookup(msgid: str, lang: str) -> object | None:
    """The stored text for msgid in lang (panel edit, then shipped default), or None."""
    if msgid in OVERRIDES.get(lang, {}):
        return OVERRIDES[lang][msgid]
    return defaults(lang).get(msgid)


def resolve(msgid: str, lang: str, n: float | None = None) -> str:
    value = lookup(msgid, lang)
    if value is None and lang != DEFAULT:
        value = lookup(msgid, DEFAULT)  # a missing translation shows the Arabic
    if value is None:
        value = msgid
    if isinstance(value, dict):
        cat = plural_category(lang if lookup(msgid, lang) is not None else DEFAULT, n if n is not None else 1)
        value = value.get(cat) or value.get("other") or next(iter(value.values()), msgid)
    return str(value)


def tr(msgid: str, lang: str | None = None, **params) -> str:
    """The text of msgid in lang (default: the current request's language), with {variables} filled."""
    if not msgid:
        return msgid
    lang = normalize(lang) or current()
    n = params.get("n", params.get("count"))
    return fill(resolve(msgid, lang, n), params)
