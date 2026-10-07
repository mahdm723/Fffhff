"""Display names (V4).

Every account shows the default name ("dzplay") until its owner picks another one. Names
are not unique: people are told apart by their public ID (DZ-XXXXXX). Rules:

* length NAME_MIN_LENGTH..NAME_MAX_LENGTH after cleaning (NFKC, tashkeel removed,
  spaces collapsed); only Arabic letters/digits, Latin letters, digits, space and "_";
* invisible characters and bidi controls (RTL override…) are refused outright;
* reserved names (NAME_RESERVED) and anything containing "dzplay" are refused after a
  lookalike "skeleton" (0→o, 1/i→l, rn→m, Arabic letter variants, spaces removed…);
* offensive words / phone numbers are refused (the moderation lexicon + NAME_BLOCKED_WORDS);
* a change every NAME_CHANGE_COOLDOWN_DAYS; going back to the default is always allowed;
* previous names are kept in NameHistory (admin only). Names are always rendered as text.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import timedelta

from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.models import NameHistory, User
from app.services import moderation

# Zero-width, joiners, bidi embeddings/overrides/isolates, Arabic letter mark, fillers, BOM, variation selectors.
_INVISIBLE = re.compile("[­͏؜ᅟᅠ឴឵᠋-᠏​-‏‪-‮"
                        "⁠-⁯ㅤ︀-️﻿ﾠ\U000e0000-\U000e007f]")
_TASHKEEL = re.compile("[ؐ-ًؚ-ٰٟۖ-ۭـ]")  # marks + tatweel
_ALLOWED = re.compile(r"^[ء-غف-ي٠-٩A-Za-z0-9 _]+$")
_SPACES = re.compile(r"[ _]{2,}")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")
_ARABIC_VARIANTS = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا", "ى": "ي", "ئ": "ي", "ؤ": "و", "ة": "ه"})
_LOOKALIKE = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l", "3": "e", "4": "a", "5": "s", "7": "t",
                            "8": "b", "9": "g", "$": "s", "@": "a"})
GENDERS = ("male", "female", "unspecified")


_default = {"name": "dzplay"}


def configure(settings: Settings) -> None:
    """Called once by the app factory (DEFAULT_DISPLAY_NAME)."""
    _default["name"] = settings.DEFAULT_DISPLAY_NAME


def default_name() -> str:
    return _default["name"]


def shown_name(user: User | None, settings: Settings | None = None) -> str:
    fallback = settings.DEFAULT_DISPLAY_NAME if settings is not None else _default["name"]
    return (user.display_name if user is not None and user.display_name else None) or fallback


def skeleton(text: str) -> str:
    """Lookalike-insensitive key used to compare against reserved names."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = _TASHKEEL.sub("", t).translate(_ARABIC_DIGITS).translate(_ARABIC_VARIANTS)
    t = t.replace("rn", "m").replace("vv", "w").replace("cl", "d")
    t = t.translate(_LOOKALIKE)
    return re.sub(r"[\s_\-.]+", "", t)


def search_key(text: str) -> str:
    """Normalized form for people search: أ/إ/آ→ا, ة→ه, ى→ي, no tashkeel, case-insensitive."""
    return moderation.normalize(unicodedata.normalize("NFKC", text or ""))


def _reserved(settings: Settings) -> set[str]:
    return {skeleton(w) for w in settings.NAME_RESERVED.split(",") if w.strip()}


def clean(raw: object, settings: Settings) -> str:
    """Validate a requested display name and return its cleaned form (raises AppError)."""
    if not isinstance(raw, str):
        raise AppError(400, "invalid_name", "اسم غير صالح.")
    if _INVISIBLE.search(raw):
        raise AppError(400, "invalid_name", "الاسم يحتوي حروفًا مخفية غير مسموح بها.")
    name = unicodedata.normalize("NFKC", raw)
    if _INVISIBLE.search(name):
        raise AppError(400, "invalid_name", "الاسم يحتوي حروفًا مخفية غير مسموح بها.")
    name = _TASHKEEL.sub("", name).strip()
    name = _SPACES.sub(lambda m: m.group(0)[0], re.sub(r"\s+", " ", name))
    if not (settings.NAME_MIN_LENGTH <= len(name) <= settings.NAME_MAX_LENGTH):
        raise AppError(400, "invalid_name_length",
                       f"الاسم بين {settings.NAME_MIN_LENGTH} و{settings.NAME_MAX_LENGTH} حرفًا.")
    if not _ALLOWED.match(name):
        raise AppError(400, "invalid_name_chars", "الاسم: حروف عربية أو لاتينية وأرقام ومسافة و _ فقط.")
    if not re.search(r"[ء-يA-Za-z]", name):
        raise AppError(400, "invalid_name", "الاسم يجب أن يحتوي حروفًا.")
    sk = skeleton(name)
    if sk in _reserved(settings) or "dzplay" in sk or "دزبلاي" in sk or "ديزدبلاي" in sk:
        raise AppError(400, "reserved_name", "هذا الاسم محجوز. اختر اسمًا آخر.")
    hit = moderation.scan(name, settings.NAME_BLOCKED_WORDS)
    if hit.categories:
        raise AppError(400, "blocked_name", "هذا الاسم غير مسموح به.")
    return name


def require_name(raw: object, settings: Settings) -> str:
    """V6 phase 3: every account shows a chosen name (no more "dzplay" by default)."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise AppError(400, "name_required", "اختر اسمًا يظهر للآخرين.")
    return clean(raw, settings)


def name_required(user: User) -> bool:
    """An account from before V6 phase 3 that never chose a name (team accounts excepted)."""
    return user.display_name is None and not user.is_system and not user.is_official


def change_name(db: Session, settings: Settings, user: User, raw: object) -> dict:
    """Set a new name (V6: going back to no name / "dzplay" is no longer possible)."""
    now = clock.utcnow()
    old = user.display_name
    name = require_name(raw, settings)
    if name == old:
        return {"display_name": name, "next_change_at": _next_change(user, settings)}
    nxt = _next_change(user, settings)
    if old is not None and nxt is not None and nxt > now:
        days = max(1, -(-int((nxt - now).total_seconds()) // 86400))  # whole days, rounded up
        raise AppError(429, "name_cooldown", f"يمكنك تغيير الاسم مرة كل {settings.NAME_CHANGE_COOLDOWN_DAYS} يومًا. "
                                             f"حاول بعد {days} يوم.", retry_after=int((nxt - now).total_seconds()))
    user.display_name = name
    user.name_norm = search_key(name)
    user.name_changed_at = now
    db.add(NameHistory(user_id=user.id, old_name=old, new_name=name, changed_at=now))
    return {"display_name": name, "next_change_at": _next_change(user, settings)}


def _next_change(user: User, settings: Settings):
    if user.name_changed_at is None:
        return None
    return user.name_changed_at + timedelta(days=settings.NAME_CHANGE_COOLDOWN_DAYS)


def set_gender(user: User, value: object) -> None:
    if value not in GENDERS:
        raise AppError(400, "invalid_gender", "اختر: رجل، أنثى، أو أفضّل عدم الذكر.")
    user.gender = value
    user.gender_asked_at = user.gender_asked_at or clock.utcnow()


def public_gender(user: User) -> str | None:
    """Gender shown next to the name (None when hidden / not given)."""
    return user.gender if user.gender in ("male", "female") else None
