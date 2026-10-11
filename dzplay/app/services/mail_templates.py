"""V6 phase 7: the texts of every e-mail, by key (email.*). Phase 8 lets the admin edit them in the content
system (same keys); until a text is edited, these defaults are used.

Variables are written {name}. Only the names listed for a template are replaced (a plain regex, never
str.format), so an edited text cannot reach anything else: "{0.__class__}" or "{settings}" stay as written.
"""

from __future__ import annotations

import re

from sqlalchemy.orm import Session

from app.config import Settings

_VAR = re.compile(r"\{([a-z_]{1,32})\}")

# key: (title shown in the panel, subject, body, variables). {app} is always available.
TEMPLATES: dict[str, tuple[str, str, str, tuple[str, ...]]] = {
    "email.code": (
        "رمز التأكيد (الاسترجاع، السحب، الظرف الأحمر)",
        "{app}: رمز التأكيد {code}",
        "مرحبًا،\n\nرمز التأكيد ({purpose}): {code}\n\nصالح {minutes} دقيقة. لا تشاركه مع أي أحد: فريق {app} لا يطلبه منك أبدًا.\n"
        "إن لم تطلبه أنت فتجاهل هذه الرسالة وغيّر كلمة المرور.\n\nفريق {app}",
        ("purpose", "code", "minutes"),
    ),
    "email.reset": (
        "رمز استعادة كلمة المرور",
        "رمز استعادة حساب {app}",
        "رمز استعادة حسابك في {app}:\n\n    {code}\n\nاكتب هذا الرمز في التطبيق لاختيار كلمة مرور جديدة. الرمز صالح {hours} ساعة ولمرة واحدة.\n"
        "إذا لم تطلب استعادة حسابك فتجاهل هذه الرسالة؛ كلمة مرورك الحالية لم تتغير.\n\nفريق {app}",
        ("code", "hours"),
    ),
    "email.membership_accepted": (
        "تفعيل العضوية",
        "{app}: العضوية",
        "مرحبًا،\n\nتم تفعيل عضويتك. أصبحت ميزات الأعضاء متاحة لك (النجمة الزرقاء، الأفكار مع صورة، صور المحادثة).\n\nفريق {app}",
        (),
    ),
    "email.membership_rejected": (
        "رفض طلب العضوية",
        "{app}: العضوية",
        "مرحبًا،\n\nلم نتمكن من قبول طلب العضوية. السبب: {note}\n\nفريق {app}",
        ("note",),
    ),
    "email.notice": (
        "إشعار عام (الاسترجاع، السحب، التوثيق)",
        "{app}: {topic}",
        "مرحبًا،\n\n{message}\n\nفريق {app}",
        ("topic", "message"),
    ),
    "email.giveaway": (
        "رمز جائزة الظرف الأحمر",
        "{app}: جائزتك في الظرف الأحمر",
        "مبروك! 🧧\n\nربحت في «{title}». رمز جائزتك:\n\n{code}\n\nلا تشاركه مع أحد. فريق {app} لن يطلبه منك أبدًا.\n\nفريق {app}",
        ("title", "code"),
    ),
    "email.ticket_new": (
        "تذكرة دعم جديدة (إلى صندوق الدعم)",
        "[{app} #{ticket}] {subject}",
        "{kind} #{ticket}\nالنوع: {category}\nالمعرّف العام: {public_id}\n\n{message}\n\n"
        "— الرد على هذا البريد يصل مباشرة إلى بريد المستخدم. للرد داخل التطبيق استعمل لوحة القيادة ← الدعم.",
        ("ticket", "subject", "kind", "category", "public_id", "message"),
    ),
    "email.ticket_reply": (
        "رد الدعم على تذكرة (إلى المستخدم)",
        "[{app} #{ticket}] تم الرد على تذكرتك",
        "مرحبًا،\n\nردّ فريق الدعم على تذكرتك #{ticket} ({subject}).\nادخل إلى {app} ← حسابي ← الدعم ← تذاكري لقراءة الرد.\n\nفريق {app}",
        ("ticket", "subject"),
    ),
    "email.test": (
        "بريد التجربة من لوحة القيادة",
        "{app}: بريد تجربة ({mailbox})",
        "إعدادات {mailbox} في {app} تعمل.",
        ("mailbox",),
    ),
    "email.footer": (
        "تذييل رسائل النظام",
        "",
        "هذه رسالة آلية، لا ترد عليها. للتواصل استخدم «الدعم» في التطبيق{support_line}.",
        ("support_line",),
    ),
}


def fill(text: str, values: dict[str, object], allowed: tuple[str, ...]) -> str:
    names = set(allowed) | {"app"}
    return _VAR.sub(lambda m: str(values[m.group(1)]) if m.group(1) in names and m.group(1) in values else m.group(0), text)


def source(db: Session | None, key: str, lang: str = "ar") -> tuple[str, str]:
    """(subject, body) of a template in lang: the text edited in the content system if any, else the default
    (V6 phase 12: the French/English defaults ship in app/i18n/content_<lang>.py; the Arabic is the last resort)."""
    from app import i18n
    from app.i18n import content as content_i18n

    lang = i18n.normalize(lang) or i18n.DEFAULT
    _title, subject, body, _vars = TEMPLATES[key]
    if db is not None:
        from app.services import cms

        edited = cms.email_template(db, key, lang)
        if edited is not None:
            return edited
    if lang != i18n.DEFAULT:
        shipped = content_i18n.default(key, lang)
        if shipped is not None:
            return (shipped[0] or subject), shipped[1]
        return source(db, key, i18n.DEFAULT)
    return subject, body


def render(db: Session | None, settings: Settings, key: str, values: dict[str, object] | None = None,
           lang: str = "ar") -> tuple[str, str]:
    allowed = TEMPLATES[key][3]
    vals = {"app": settings.APP_NAME, **(values or {})}
    subject, body = source(db, key, lang)
    return fill(subject, vals, allowed).replace("\n", " ")[:200], fill(body, vals, allowed)
