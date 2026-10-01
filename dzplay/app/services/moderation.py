"""Automatic flagging of harmful private content, for user protection.

Every new message and comment is scanned. A hit never blocks the content and
the sender is not told; a copy of the text is queued (ContentFlag) for the
owner to review in the admin dashboard, so it survives the message TTL. Users
are told about this in the in-app privacy notice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import timedelta
from functools import lru_cache

from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.models import ContentFlag
from app.moderation_words import CATEGORIES

_TASHKEEL = re.compile(r"[ؐ-ًؚ-ٰٟۖ-ۭـ]")
_ARABIC_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه", "گ": "ك",
                             **{chr(0x0660 + d): str(d) for d in range(10)}, **{chr(0x06F0 + d): str(d) for d in range(10)}})
_NON_WORD = re.compile(r"[^\w']+")
_REPEATS = re.compile(r"(\D)\1+")  # letters only: numbers keep their digits
_PREFIXES = ("وال", "بال", "فال", "كال", "لل", "ال", "يا", "و", "ب", "ف", "ل")
_SUFFIXES = ("كم", "ها", "هم", "كي", "ك", "ه", "ي")

_PHONE = re.compile(r"(?:\+|00)?\d(?:[\s.\-]?\d){8,}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]{2,}")
_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z0-9_.]{3,}")
_LINK = re.compile(r"https?://|www\.|\b[\w-]+\.(?:com|net|org|dz|fr|me|io|ly)\b", re.IGNORECASE)

CATEGORY_ORDER = ("threat", "blackmail", "sexual", "insult", "contact", "custom")


def normalize(text: str) -> str:
    """Lowercase, strip diacritics/tatweel, unify letter variants, collapse repeats."""
    t = _TASHKEEL.sub("", text.lower()).translate(_ARABIC_MAP)
    t = _NON_WORD.sub(" ", t).replace("_", " ")
    return " ".join(_REPEATS.sub(r"\1", t).split())


@dataclass
class _Lexicon:
    words: dict[str, str] = field(default_factory=dict)      # whole word -> category
    stems: list[tuple[str, str]] = field(default_factory=list)  # word prefix -> category
    phrases: list[tuple[str, str]] = field(default_factory=list)  # " phrase " -> category

    def add(self, term: str, category: str) -> None:
        star = term.strip().endswith("*")
        norm = normalize(term.strip().rstrip("*"))
        if not norm:
            return
        if star:
            self.stems.append((norm, category))
        elif " " in norm:
            self.phrases.append((f" {norm} ", category))
        else:
            self.words.setdefault(norm, category)


@lru_cache(maxsize=8)
def _lexicon(extra_words: str) -> _Lexicon:
    lex = _Lexicon()
    for category, terms in CATEGORIES.items():
        for term in terms:
            lex.add(term, category)
    for term in extra_words.split(","):
        lex.add(term, "custom")
    return lex


def _variants(token: str) -> set[str]:
    out = {token}
    for p in _PREFIXES:
        if token.startswith(p) and len(token) - len(p) >= 2:
            out.add(token[len(p):])
    for t in list(out):
        for s in _SUFFIXES:
            if t.endswith(s) and len(t) - len(s) >= 2:
                out.add(t[: -len(s)])
    return out


@dataclass
class ScanResult:
    categories: list[str]
    terms: list[str]

    def __bool__(self) -> bool:
        return bool(self.categories)


def scan(text: str, extra_words: str = "") -> ScanResult:
    found: dict[str, set[str]] = {}

    def hit(category: str, term: str) -> None:
        found.setdefault(category, set()).add(term)

    raw = text.translate(_ARABIC_MAP)
    for regex, label in ((_PHONE, "رقم هاتف"), (_EMAIL, "بريد إلكتروني"), (_HANDLE, "حساب @"), (_LINK, "رابط")):
        if regex.search(raw):
            hit("contact", label)

    lex = _lexicon(extra_words)
    norm = normalize(text)
    padded = f" {norm} "
    for phrase, category in lex.phrases:
        if phrase in padded:
            hit(category, phrase.strip())
    for token in norm.split():
        for v in _variants(token):
            if v in lex.words:
                hit(lex.words[v], v)
        for stem, category in lex.stems:
            if token.startswith(stem) or any(v.startswith(stem) for v in _variants(token)):
                hit(category, stem + "…")
    categories = [c for c in CATEGORY_ORDER if c in found]
    terms = sorted({t for c in categories for t in found[c]})
    return ScanResult(categories, terms)


def flag_content(db: Session, settings: Settings, *, target: str, text: str, offender_id: str, victim_id: str | None,
                 message_id: str | None = None, conversation_id: str | None = None,
                 comment_id: str | None = None, post_id: str | None = None) -> ContentFlag | None:
    """Queue a copy of harmful content for review. Never raises into the send path."""
    if not settings.MODERATION_ENABLED:
        return None
    result = scan(text, settings.MODERATION_EXTRA_WORDS)
    if not result:
        return None
    now = clock.utcnow()
    flag = ContentFlag(
        target=target, message_id=message_id, conversation_id=conversation_id, comment_id=comment_id, post_id=post_id,
        offender_id=offender_id, victim_id=victim_id, categories=",".join(result.categories),
        terms=json.dumps(result.terms[:20], ensure_ascii=False), snapshot=text, created_at=now,
        expires_at=now + timedelta(seconds=settings.REPORT_RETENTION),
    )
    db.add(flag)
    return flag
