"""Limits and conditions the admin can change from the panel, live (no restart).

Every entry names a field of `Settings` (its default comes from `.env` / config.py). A value saved
from the panel is stored in AppSetting ("tun.<KEY>"), validated against the entry's type and bounds,
applied to the running Settings object, and announced to every app instance through the hub.
Removing it falls back to the `.env` value. Secrets are never tunables (they stay in `.env`).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app import clock
from app.config import Settings
from app.errors import AppError
from app.models import AppSetting

log = logging.getLogger("dzplay.tunables")
_PREFIX = "tun."


@dataclass(frozen=True)
class Tunable:
    key: str
    kind: str  # int | float | bool | text | chat (Telegram chat id or empty)
    group: str
    label: str
    min: float | None = None
    max: float | None = None
    max_len: int = 500


GROUPS = {
    "media": "الوسائط والفحص",
    "ideas": "الأفكار",
    "chat": "الرسائل وصور المحادثة",
    "moderation": "الإشراف والبلاغات",
    "telegram": "قنوات Telegram",
    "support": "الدعم",
    "verify": "التوثيق (النجمة الزرقاء)",
}
_CHAT = re.compile(r"^-?[0-9]{3,20}$")

REGISTRY: list[Tunable] = [
    # Telegram chats (IDs only; the bot token stays a secret in .env / sealed in the panel)
    Tunable("TELEGRAM_STORAGE_CHANNEL_ID", "chat", "telegram", "معرّف قناة التخزين الخاصة (يبدأ بـ -100)"),
    Tunable("TELEGRAM_MODERATION_CHAT_ID", "chat", "telegram", "معرّف مجموعة الإشراف الخاصة (فارغ = محادثة المدير)"),
    # media / scanning
    Tunable("UPLOADS_PER_HOUR", "int", "media", "عدد الرفع في الساعة لكل مستخدم", 1, 500),
    Tunable("UPLOAD_IMAGE_MAX_MB", "float", "media", "أقصى حجم للصورة المرفوعة (MB)", 0.5, 50),
    Tunable("UPLOAD_IMAGE_MIN_SIDE", "int", "media", "أصغر بُعد مقبول للصورة (px)", 16, 2000),
    Tunable("UPLOAD_IMAGE_MAX_SIDE", "int", "media", "أكبر بُعد مقبول للصورة (px)", 500, 20000),
    Tunable("DEVICE_IMAGE_MAX_SIDE", "int", "media", "الهاتف يصغّر الصورة إلى (px)", 480, 4096),
    Tunable("DEVICE_IMAGE_QUALITY", "float", "media", "جودة ضغط الصورة على الهاتف (0–1)", 0.3, 1),
    Tunable("DEVICE_NSFW_CHECK", "bool", "media", "فحص المحتوى الإباحي على الهاتف"),
    Tunable("SERVER_NSFW_CHECK", "bool", "media", "فحص المحتوى الإباحي على الخادم"),
    Tunable("NSFW_BLOCK_THRESHOLD", "float", "media", "حد رفض المحتوى الإباحي (0–1)", 0.05, 1),
    Tunable("NSFW_SEXY_THRESHOLD", "float", "media", "حد رفض المحتوى المثير (0–1، 1 = لا يُرفض)", 0.05, 1),
    Tunable("NSFW_VIDEO_FRAMES", "int", "media", "عدد لقطات الفيديو المفحوصة", 1, 12),
    Tunable("CAPTION_BLOCK_CATEGORIES", "text", "media", "فئات الكلمات التي ترفض الوصف (sexual,threat,blackmail,insult,contact)",
            max_len=100),
    # ideas
    Tunable("IDEA_IMAGES_ENABLED", "bool", "ideas", "السماح بصورة مع الفكرة"),
    Tunable("IDEA_IMAGE_LIMIT_PER_24H", "int", "ideas", "صور الأفكار لكل مستخدم في 24 ساعة", 0, 50),
    Tunable("IDEA_IMAGE_REQUIRE_APPROVAL", "bool", "ideas", "الفكرة ذات الصورة تظهر بعد الموافقة فقط"),
    Tunable("MAX_POSTS_PER_HOUR", "int", "ideas", "أفكار في الساعة", 1, 200),
    Tunable("MAX_POSTS_PER_DAY", "int", "ideas", "أفكار في اليوم", 1, 1000),
    Tunable("MAX_POST_LENGTH", "int", "ideas", "أقصى طول للفكرة (حرف)", 50, 10000),
    # chat
    Tunable("CHAT_IMAGES_ENABLED", "bool", "chat", "السماح بالصور في المحادثات"),
    Tunable("CHAT_IMAGE_TTL_AFTER_VIEW", "int", "chat", "مدة عرض الصورة بعد فتحها (ثانية)", 5, 3600),
    # max 46 h: Telegram lets bots delete a message only during its first 48 hours
    Tunable("CHAT_IMAGE_UNOPENED_TTL", "int", "chat", "حذف الصورة غير المفتوحة بعد (ثانية)", 300, 46 * 3600),
    Tunable("CHAT_IMAGE_REPORT_GRACE", "int", "chat", "مهلة الإبلاغ بعد انتهاء الصورة (ثانية، لا تُعرض خلالها)", 0, 3600),
    Tunable("CHAT_IMAGE_PER_HOUR", "int", "chat", "صور المحادثة في الساعة لكل مستخدم", 1, 200),
    Tunable("CHAT_IMAGE_ARCHIVE", "bool", "chat", "أرشفة صور المحادثة بعد انتهائها (يُذكر في السياسة)"),
    Tunable("CHAT_IMAGE_FLAG_SECURE", "bool", "chat", "منع لقطة الشاشة أثناء عرض الصورة (تطبيق أندرويد)"),
    Tunable("MAX_MESSAGES_PER_MINUTE", "int", "chat", "رسائل في الدقيقة", 1, 120),
    Tunable("MAX_MESSAGES_PER_HOUR", "int", "chat", "رسائل في الساعة", 1, 2000),
    Tunable("MAX_NEW_CONVERSATIONS_PER_DAY", "int", "chat", "محادثات مجهولة جديدة في اليوم", 1, 500),
    # moderation
    Tunable("REPORT_AUTO_HIDE_THRESHOLD", "int", "moderation", "عدد المبلّغين لإخفاء المحتوى تلقائيًا (0 = معطل)", 0, 100),
    Tunable("REPORT_AUTO_SUSPEND_THRESHOLD", "int", "moderation", "عدد المبلّغين لإيقاف الحساب تلقائيًا (0 = معطل)", 0, 100),
    Tunable("MAX_REPORTS_PER_HOUR", "int", "moderation", "بلاغات في الساعة لكل مستخدم", 1, 200),
    # support + verification
    Tunable("SUPPORT_TICKETS_PER_DAY", "int", "support", "تذاكر جديدة في اليوم لكل مستخدم", 1, 50),
    Tunable("SUPPORT_MESSAGES_PER_HOUR", "int", "support", "رسائل الدعم في الساعة لكل مستخدم", 1, 200),
    Tunable("SUPPORT_MAX_LENGTH", "int", "support", "أقصى طول لرسالة الدعم (حرف)", 200, 10000),
    Tunable("VERIFY_ENABLED", "bool", "verify", "استقبال طلبات التوثيق"),
    Tunable("VERIFY_MIN_POSTS", "int", "verify", "أقل عدد من الأفكار المنشورة", 0, 10000),
    Tunable("VERIFY_MIN_LIKES", "int", "verify", "أقل عدد من الإعجابات", 0, 1000000),
    Tunable("VERIFY_MIN_ACCOUNT_AGE_DAYS", "int", "verify", "أقل عمر للحساب (يوم)", 0, 3650),
    Tunable("PAYMENT_MIN_AMOUNT", "float", "verify", "أقل مبلغ للدفع (0 = أي مبلغ)", 0, 1000000),
    Tunable("MEDIA_EVIDENCE_RETENTION_DAYS", "int", "moderation", "مدة الاحتفاظ بالوسائط المبلّغ عنها (يوم)", 7, 3650),
]
_BY_KEY = {t.key: t for t in REGISTRY}


def register(*items: Tunable, group: tuple[str, str] | None = None) -> None:
    """Later features add their own entries (verification, earnings…)."""
    if group:
        GROUPS.setdefault(*group)
    for t in items:
        if t.key not in _BY_KEY:
            REGISTRY.append(t)
            _BY_KEY[t.key] = t


def coerce(t: Tunable, value: object):
    """Validate a panel value; raises AppError with an Arabic message."""
    bad = AppError(400, "invalid_value", f"قيمة غير صالحة لـ «{t.label}».")
    if t.kind == "bool":
        if not isinstance(value, bool):
            raise bad
        return value
    if t.kind == "chat":
        if not isinstance(value, str) or (value.strip() and not _CHAT.match(value.strip())):
            raise bad
        return value.strip()
    if t.kind == "text":
        if not isinstance(value, str) or len(value) > t.max_len or any(ord(c) < 32 for c in value):
            raise bad
        return value.strip()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise bad
    if t.kind == "int":
        if isinstance(value, float) and not value.is_integer():
            raise bad
        value = int(value)
    else:
        value = float(value)
    if (t.min is not None and value < t.min) or (t.max is not None and value > t.max):
        raise AppError(400, "out_of_range", f"«{t.label}» يجب أن تكون بين {t.min:g} و{t.max:g}.")
    return value


def _env_default(settings: Settings, key: str):
    defaults = settings._tunable_env
    if defaults is None:
        defaults = settings._tunable_env = {}
    if key not in defaults:
        defaults[key] = getattr(settings, key)
    return defaults[key]


def apply(db: Session, settings: Settings, *targets: Settings) -> int:
    """Apply the stored panel values to `settings` (and copies such as the bot's). Returns how many."""
    stored = {row.key[len(_PREFIX):]: row.value for row in db.execute(
        select(AppSetting).where(AppSetting.key.like(_PREFIX + "%"))).scalars()}
    applied = 0
    for t in REGISTRY:
        default = _env_default(settings, t.key)
        value = default
        if t.key in stored:
            try:
                value = coerce(t, json.loads(stored[t.key]))
                applied += 1
            except (AppError, ValueError):
                log.warning("ignoring invalid stored value for %s", t.key)
        for target in (settings, *targets):
            if target is not None:
                setattr(target, t.key, value)
    return applied


def listing(db: Session, settings: Settings) -> list[dict]:
    stored = {row.key[len(_PREFIX):]: row for row in db.execute(
        select(AppSetting).where(AppSetting.key.like(_PREFIX + "%"))).scalars()}
    out = []
    for t in REGISTRY:
        row = stored.get(t.key)
        out.append({"key": t.key, "type": t.kind, "group": t.group, "group_label": GROUPS.get(t.group, t.group),
                    "label": t.label, "min": t.min, "max": t.max, "value": getattr(settings, t.key),
                    "default": _env_default(settings, t.key), "overridden": row is not None,
                    "updated_by": row.updated_by if row else None})
    return out


def save(db: Session, settings: Settings, changes: dict, actor: str) -> list[str]:
    """Validate every change first, then store them all. A value of None resets the key to .env."""
    if not isinstance(changes, dict) or not changes or len(changes) > len(REGISTRY):
        raise AppError(400, "invalid_input", "طلب غير صالح.")
    clean: dict[str, object] = {}
    for key, value in changes.items():
        t = _BY_KEY.get(key)
        if t is None:
            raise AppError(400, "unknown_setting", "إعداد غير معروف.")
        clean[key] = None if value is None else coerce(t, value)
    now = clock.utcnow()
    for key, value in clean.items():
        row = db.get(AppSetting, _PREFIX + key)
        if value is None:
            if row is not None:
                db.delete(row)
            continue
        if row is None:
            db.add(AppSetting(key=_PREFIX + key, value=json.dumps(value), updated_at=now, updated_by=actor))
        else:
            row.value, row.updated_at, row.updated_by = json.dumps(value), now, actor
    db.flush()
    return sorted(clean)


def clear_all(db: Session) -> None:
    db.execute(delete(AppSetting).where(AppSetting.key.like(_PREFIX + "%")))
