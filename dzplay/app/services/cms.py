"""V6 phase 8: the content system — every non-essential text (policies, terms, help pages, notices, e-mails)
editable from the panel, with a revision history and «major change» re-consent.

* pages  — /policies/<key> (Markdown, rendered by services/markdown.py), with «آخر تحديث».
* texts  — short texts used inside the app (market disclaimer, membership intro, welcome, announcement, footer).
* emails — the e-mail texts (email.*, services/mail_templates.py): subject = title, body = plain text.

Defaults live in the code (below); an edited text is a ContentBlock row, each save a ContentRevision. Texts can
use {{variables}} filled from the running settings (TTLs, prices, limits…), so they keep matching what the app
really does; only the names in values() are replaced. No personal information about the owner, anywhere.
Reads go straight to the database (a few small rows by primary key): always current on every instance.

V6 phase 12 — three languages. The Arabic text (ContentBlock, else the default above) is the original. French and
English: an edit from the panel (ContentTranslation), else the translation shipped in app/i18n/content_<lang>.py,
else the Arabic text (shown as «missing» in the panel). Re-consent («major change») follows the Arabic version.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock, i18n
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, not_found
from app.i18n import N_, content as content_i18n
from app.models import AppSetting, ContentBlock, ContentRevision, ContentTranslation
from app.services import markdown

DEFAULT_UPDATED = "2026-10-10"
ACK_KEY = "content.ack_version"
_VAR = re.compile(r"\{\{\s*([a-z_]{1,40})\s*\}\}")
MAX_TITLE = 200


@dataclass(frozen=True)
class Spec:
    key: str
    title: str
    kind: str  # page | text | email
    default: object  # str, or callable(settings) -> str
    ack: bool = False  # a «major change» asks every user to accept it again
    note: str = ""
    setting: str = ""  # a default read from this setting: translated only while the setting keeps its default value


def _lang(lang: str | None) -> str:
    return i18n.normalize(lang) or i18n.current()


def _days(seconds: int, lang: str | None = None) -> str:
    d = seconds / 86400
    if d >= 1:
        return i18n.tr_params(N_("{n} يوم"), lang, {"n": d})
    return i18n.tr_params(N_("{n} ساعة"), lang, {"n": max(1, seconds // 3600)})


def _join(items: list[str], lang: str) -> str:
    """«A وB» / «A et B» / «A and B»."""
    if len(items) < 2:
        return items[0] if items else "—"
    sep = "، " if lang == "ar" else ", "
    return i18n.tr(N_("{items} و{last}"), lang, items=sep.join(items[:-1]), last=items[-1])


def values(db: Session | None, settings: Settings, lang: str | None = None) -> dict[str, str]:
    """The {{variables}} available in pages and texts (all plain text), in the language of the text."""
    s = settings
    lang = _lang(lang)
    tr = i18n.tr
    support = ""
    if db is not None:
        from app.services import mail

        support = mail.support_address(db, s)
    nets = [n.strip() for n in str(s.WITHDRAW_NETWORKS).split(",") if n.strip()]
    return {
        "app": s.APP_NAME,
        "message_ttl": _days(s.MESSAGE_TTL, lang),
        "message_ttl_read": _days(s.MESSAGE_TTL_AFTER_READ, lang),
        "conversation_idle": _days(s.CONVERSATION_IDLE_TTL, lang),
        "legacy_anon_days": str(s.LEGACY_ANON_RETENTION_DAYS),
        "chat_ttl": str(s.CHAT_IMAGE_TTL_AFTER_VIEW),
        "chat_unopened": _days(s.CHAT_IMAGE_UNOPENED_TTL, lang),
        "chat_grace_min": str(max(1, s.CHAT_IMAGE_REPORT_GRACE // 60)),
        "chat_archive": tr(N_("لا نؤرشف صور المحادثات: بعد انتهائها تُحذف نهائيًا (إلا إذا أُبلغ عنها).") if not s.CHAT_IMAGE_ARCHIVE else
                           N_("إعداد الأرشفة مفعّل حاليًا: تُحفظ نسخة من صور المحادثات بعد انتهائها في مستودع التخزين الخاص ولا يراها أي مستخدم."), lang),
        "evidence_days": str(s.MEDIA_EVIDENCE_RETENTION_DAYS),
        "report_retention": _days(s.REPORT_RETENTION, lang),
        "security_retention": _days(s.SECURITY_EVENT_RETENTION, lang),
        "audit_retention": _days(s.AUDIT_LOG_RETENTION, lang),
        "cache_ttl": _days(s.MEDIA_CACHE_TTL, lang),
        "idea_image_limit": str(s.IDEA_IMAGE_LIMIT_PER_24H),
        "avatar_per_day": str(s.AVATAR_CHANGES_PER_DAY),
        "report_threshold": str(s.REPORT_AUTO_HIDE_THRESHOLD or "—"),
        "membership_price": i18n.number(float(s.MEMBERSHIP_PRICE), lang),
        "refund_days": str(s.MEMBERSHIP_REFUND_WINDOW_DAYS),
        "refund_fee": i18n.number(float(s.MEMBERSHIP_REFUND_FEE), lang),
        "refund_policy": (i18n.tr_params(N_("يمكن طلب استرجاعها خلال {n} أيام من تفعيلها، ويُعاد المبلغ ناقص رسوم "
                                            "{fee} USDT، وتتوقف الميزات عند الإرجاع."),
                                         lang, {"n": s.MEMBERSHIP_REFUND_WINDOW_DAYS, "fee": float(s.MEMBERSHIP_REFUND_FEE)})
                          if s.MEMBERSHIP_REFUNDABLE else tr(N_("لا تُسترجع قيمة العضوية بعد تفعيلها."), lang)),
        "referral_reward": i18n.number(float(s.REFERRAL_REWARD), lang),
        "referral_hold_days": str(s.REFERRAL_HOLD_DAYS),
        "withdraw_min": i18n.number(float(s.WITHDRAW_MIN), lang),
        "withdraw_fee": i18n.number(float(s.WITHDRAW_FEE), lang),
        "withdraw_networks": _join(nets, lang),
        "support_email": support or "—",
        "support_line": tr(N_("، أو على بريد الدعم الرسمي {email}"), lang, email=support) if support else "",
    }


def fill(text: str, vals: dict[str, str]) -> str:
    """{{name}} → value for known names only; anything else stays as written."""
    return _VAR.sub(lambda m: vals.get(m.group(1), m.group(0)), text)


# ------------------------------------------------------------------ default texts

PRINCIPLE = ("{{app}} مجتمع للمتداولين ولمشاركة أخبار العملات الرقمية والتحليلات والأفكار، مع حماية بياناتك: "
             "لا يرى أي مستخدم بريدك أو أي بيانات خاصة عنك إلا ما تختار إظهاره بنفسك (الاسم، الصورة، المنشورات).")

PRIVACY = """# 1. ما نجمعه ولماذا
- **البريد الإلكتروني وكلمة مرور مشفّرة** (أو معرّف Google): لتسجيل الدخول، واستعادة الحساب، وردود الدعم، وإشعارات الحساب فقط. لا يظهر لأي مستخدم.
- **الاسم الظاهر والمعرّف العام (DZ-…)**، والصورة الشخصية إن أضفتها، والجنس إن اخترته: تظهر للآخرين لأنك اخترت ذلك.
- **تأكيد العمر (18+)**: لا نطلب تاريخ ميلادك.
- **ما تنشره**: الأفكار وصورها، والتعليقات والردود، والإعجابات (يظهر من أعجبه منشور؛ أما «لم يعجبني» فعدد فقط بلا أسماء).
- **الرسائل**: مؤقتة؛ تُحذف من الخادم بعد {{message_ttl}} على الأكثر، وأسرع بعد قراءتها ({{message_ttl_read}}). المحادثة الخاملة تُحذف بعد {{conversation_idle}}. الرسائل المجهولة العشوائية أُوقفت؛ المحادثات المجهولة القديمة تبقى للقراءة فقط ثم تُحذف بعد {{legacy_anon_days}} أيام.
- **عناوين IP**: لا نحفظها كما هي؛ نحفظ بصمة مشفّرة غير قابلة للعكس فقط لمنع الإساءة والمحاولات الآلية.
- **إشعارات الهاتف** (اختيارية): رمز الجهاز اللازم لإيصال الإشعار فقط، ولا يحمل الإشعار اسمًا أو نص رسالة.
- **الدعم**: نص التذكرة، ويصل إلى بريد فريق الدعم مع معرّفك العام فقط.
- **العضوية والسحب**: المبلغ والشبكة ورقم عملية الدفع (TXID)، وعنوان المحفظة الذي تكتبه أنت عند الاسترجاع أو السحب. لا نطلب اسمًا حقيقيًا ولا وثائق. تُحفظ هذه السجلات المالية لأغراض المحاسبة حتى بعد حذف الحساب (بلا بريد).

# 2. ما يراه الآخرون
اسمك الظاهر وصورتك ومعرّفك العام وأيقونة الجنس إن اخترتها، والنجمة الزرقاء إن كانت لك: في المنشورات والتعليقات وصفحة المستخدمين والمحادثات. لا يرى أحد بريدك أو رقمك أو موقعك أو أي معرّف داخلي.

# 3. اطلاع فريق الإدارة
لتشغيل الخدمة والإشراف عليها وحماية المستخدمين، يستطيع فريق الإدارة الاطلاع على بيانات الحسابات (ومنها البريد) والمنشورات والتعليقات، والرسائل ما دامت محفوظة، والصور المنشورة، وصور المحادثات المبلَّغ عنها فقط. الوصول محمي بحساب مشرف وتحقق بخطوتين، وكل اطلاع وكل إجراء يُسجَّل في سجل تدقيق داخلي. تُفحص الرسائل والتعليقات والصور آليًا بحثًا عن المحتوى الضار.

# 4. مزوّد التخزين الخارجي
الصور تُحفظ في **قناة خاصة على Telegram** يملكها {{app}} ويصل إليها البوت الخاص بالمنصة فقط. خادمنا يمرّر الملفات ولا يحتفظ إلا بنسخة مؤقتة لتسريع العرض. تطبيقك لا يتصل بـTelegram أبدًا، ولا يحصل أي مستخدم على مرجع الملف. تصل نسخة من الصور المنشورة والصور الشخصية (وليس صور المحادثات) إلى مجموعة خاصة بالمشرفين للمراجعة.

# 5. حذف البيانات الوصفية والفحص
قبل الحفظ يُعاد ترميز كل صورة، وتُحذف كل البيانات الوصفية: الموقع (GPS)، ونوع الجهاز، والتاريخ وغيرها. يُفحص الملف على جهازك ثم على الخادم بنموذج آلي لكشف المحتوى الإباحي، ويُرفض المخالف قبل حفظه. لا يوجد فحص آلي يكشف أعمار الأشخاص؛ لذلك نعتمد على البلاغات والمراجعة البشرية.

# 6. صور المحادثات
- إرسال الصور في المحادثات ميزة للأعضاء، ويستطيع أي مستخدم استقبالها وفتحها.
- تصل مموّهة، ولا تظهر إلا عند ضغط المستلم عليها، ثم تبقى {{chat_ttl}} ثانية فقط وتختفي عند الطرفين.
- الصورة التي لا تُفتح تُحذف بعد {{chat_unopened}}.
- بعد انتهائها تُحذف فورًا من الخادم ومن الأجهزة، وتُحذف نسخة التخزين بعد مهلة إبلاغ قصيرة ({{chat_grace_min}} دقيقة) لا يراها خلالها أحد.
- {{chat_archive}}
- الصورة المبلَّغ عنها تُحفظ دليلًا للمراجعة حتى {{evidence_days}} يومًا.
- في تطبيق أندرويد تُمنع لقطة الشاشة أثناء العرض، لكن لا يمكن منع تصوير الشاشة بجهاز آخر: لا ترسل ما لا تقبل أن يُرى.

# 7. العمر
{{app}} للبالغين (18 سنة فأكثر) فقط.

# 8. مدة الاحتفاظ
- الرسائل والمحادثات: كما في البند 1.
- البلاغات: {{report_retention}}. الأحداث الأمنية: {{security_retention}}. سجل تدقيق الإدارة: {{audit_retention}}.
- النسخ المؤقتة على الخادم: تُحذف عند عدم استعمالها {{cache_ttl}}.
- الحساب وما تنشره: ما دام حسابك موجودًا.

# 9. حذف الحساب
من **حسابي ← حذف حسابي**. يُحذف فورًا: الحساب والجلسات، والملف والاسم والصورة، والأفكار وصورها وتعليقاتها وتفاعلاتها، وتعليقاتك وتفاعلاتك، والمحادثات والرسائل، وتذاكر الدعم، ورموز الإشعارات. وتُحذف صورك من مستودع التخزين. يبقى فقط: ما أُبلغ عنه كدليل (حتى {{evidence_days}} يومًا)، والبلاغات التي قدّمها آخرون (حتى {{report_retention}})، والسجلات المالية (بلا بريد)، وسطر في سجل التدقيق بأن حسابًا حُذف (المعرّف العام فقط).

# 10. التواصل
من داخل التطبيق: **حسابي ← الدعم والمساعدة**{{support_line}}."""

TERMS = """# 1. القبول
باستعمالك {{app}} تقبل هذه الشروط وسياسة الخصوصية وإرشادات المجتمع.

# 2. الأهلية
يجب أن يكون عمرك 18 سنة أو أكثر. حساب واحد لكل شخص، وأنت مسؤول عن حماية كلمة مرورك.

# 3. محتواك
أنت مسؤول عمّا تنشره وترسله. تمنح {{app}} إذنًا بعرض محتواك المنشور داخل المنصة. يجوز لنا إخفاء أو حذف أي محتوى يخالف الإرشادات، وإيقاف أو حظر الحسابات المخالفة.

# 4. ليست نصيحة استثمارية
أسعار السوق والمنشورات والتحليلات والتعليقات **للاطلاع فقط، وليست نصيحة استثمارية** ولا توصية بالشراء أو البيع. قرارات التداول مسؤوليتك وحدك، ولا يقدّم {{app}} أي خدمة تداول أو حفظ أموال.

# 5. الخصوصية بين المستخدمين
يمنع كشف هوية مستخدم آخر أو نشر بياناته أو محاولة معرفتها، أو تسجيل المحادثات والصور المؤقتة ونشرها.

# 6. العضوية
العضوية دفعة واحدة ({{membership_price}} USDT حاليًا) تمنح **ميزات داخل التطبيق فقط**: النجمة الزرقاء، ونشر الأفكار مع صورة، وإرسال صور المحادثة. **لا علاقة لها بالتداول ولا بأي ربح أو عائد مالي**، ولا تدخل في شروط أي مكافأة. {{refund_policy}} التفاصيل في [شروط العضوية](/policies/membership_terms).

# 7. المكافآت الترويجية والسحب
مكافآت ترويجية **منفصلة تمامًا عن العضوية** ولا علاقة لها بالتداول، وغير مضمونة. التفاصيل في [شروط المكافآت](/policies/rewards_terms) و[شروط السحب](/policies/withdraw_terms).

# 8. الظرف الأحمر
مسابقة مجانية لا تتطلب عضوية ولا دفعًا. التفاصيل في [شروط الظرف الأحمر](/policies/giveaway_terms).

# 9. الخدمة
نقدّم الخدمة «كما هي» ونعمل على استمرارها، لكن قد تتوقف أحيانًا للصيانة. يمكن تعديل هذه الشروط، ونطلب موافقتك من جديد داخل التطبيق عند أي تغيير جوهري.

# 10. التواصل
حسابي ← الدعم والمساعدة{{support_line}}."""

GUIDELINES = """# مسموح ومرحّب به
النقاش والتحليل وأخبار السوق والأفكار باحترام، ومشاركة التجارب والآراء.

# ممنوع
- التهديد والابتزاز والتحرش والمضايقة والتنمر.
- الاحتيال والوعود بربح مضمون، وجمع الأموال، وطلب كلمات المرور أو العبارات السرية للمحافظ.
- المحتوى الجنسي أو الإباحي أو العاري، في الأفكار أو الصور أو صور المحادثات.
- **أي محتوى يخص قاصرًا بشكل جنسي أو مسيء: يُحذف فورًا ويُحظر صاحبه نهائيًا، ويُحتفظ بنسخة دليل.**
- خطاب الكراهية والتحريض على العنف.
- نشر أرقام الهواتف والحسابات والعناوين والبيانات الشخصية، لك أو لغيرك، أو طلبها.
- الروابط الترويجية والإعلانات والرسائل المزعجة والحسابات الوهمية.
- انتحال صفة {{app}} أو فريقه، أو استعمال رموز تشبه النجمة في الأسماء.

# الصور
صورة مع الفكرة كل 24 ساعة (حاليًا {{idea_image_limit}}) للأعضاء، وتُراجع من فريق الإشراف. صور المحادثات مؤقتة وتُتاح بعد رد الطرف الآخر. يُرفض آليًا ما يبدو مخالفًا.

# البلاغات
استعمل زر «إبلاغ» على أي محتوى. البلاغات سرية. عند {{report_threshold}} بلاغات من أشخاص مختلفين يُخفى المحتوى مؤقتًا حتى المراجعة.

# العقوبات
حسب الخطورة: حذف المحتوى، أو إيقاف مؤقت، أو حظر نهائي، أو سحب النجمة."""

VERIFICATION = """# ما هي النجمة الزرقاء؟
علامة تعني أن الحساب **عضو أو حساب موثوق داخل {{app}}**. **ليست تحققًا من الهوية الحقيقية**، ولا نطلب أي وثيقة أو اسم حقيقي أو صورة شخصية.

# كيف أحصل عليها؟
تأتي النجمة مع **العضوية** (حسابي ← عضويتي). من كان يملك النجمة قبل العضوية يحتفظ بها. نجمة العضوية تزول إن انتهت العضوية أو استُرجعت، وكل نجمة يمكن سحبها عند المخالفة.

# أين تظهر؟
في الملف الشخصي والمنشورات والتعليقات وصفحة المستخدمين والمحادثات."""

MEMBERSHIP_TERMS = """# ما هي العضوية؟
اشتراك بدفعة واحدة ({{membership_price}} USDT حاليًا) مقابل **ميزات داخل التطبيق فقط**:
- النجمة الزرقاء بجانب اسمك.
- نشر الأفكار مع صورة.
- إرسال الصور في المحادثات (تختفي بعد {{chat_ttl}} ثانية من فتحها).

# ما ليست عليه
العضوية **لا ترتبط بأي ربح أو عائد** ولا بالتداول، ولا تمنح أي حق في مكافأة، ولا تدخل في شروط أي مكافأة أو مسابقة.

# الدفع
يُدفع بالعملة والشبكة المعروضتين في «عضويتي» فقط، ثم تكتب رقم العملية (TXID). الإرسال على شبكة أخرى أو إلى عنوان آخر قد يضيع المبلغ ولا يمكن استرجاعه. يراجع الفريق الدفع ثم يفعّل العضوية.

# الاسترجاع
{{refund_policy}} يُطلب من «عضويتي» بكلمة المرور ورمز يصل إلى بريدك، ثم يرسل الفريق المبلغ إلى العنوان الذي تكتبه."""

REWARDS_TERMS = """# مكافآت ترويجية
مكافآت يقدّمها {{app}} للترويج، **منفصلة تمامًا عن العضوية**، وليست عائدًا على أي دفع أو استثمار، وغير مضمونة. تحددها المنصة وفق هذه الشروط، ويمكن تعديلها أو إيقافها.

# دعوة صديق
- {{referral_reward}} USDT عندما يصبح صديق دعوته عضوًا، وتبقى معلّقة {{referral_hold_days}} يومًا قبل أن تُتاح.
- مستوى واحد فقط، ولا مكافأة على دعوة نفسك.
- تُلغى المكافأة إن استُرجعت عضوية الصديق.
- الدعوات المشبوهة (نفس الشبكة، أو أعداد غير طبيعية) تُراجع يدويًا، والوهمية أو المكررة تُلغى وقد يُوقف الحساب.

# المسابقات ومكافآت النشاط
يعلن عنها الفريق داخل التطبيق، ولا تعتمد معاييرها على العضوية أو الدفع أبدًا."""

WITHDRAW_TERMS = """# السحب
- الشبكات المدعومة: {{withdraw_networks}} (USDT).
- الحد الأدنى: {{withdraw_min}} USDT من الرصيد المتاح فقط (المعلّق لا يُسحب).
- رسوم الشبكة: {{withdraw_fee}} USDT تُخصم من المبلغ المرسل، ويُعرض الصافي قبل التأكيد.
- التأكيد بكلمة المرور ورمز يصل إلى بريدك.

# مسؤولية العنوان
تأكد من العنوان والشبكة قبل التأكيد: **الإرسال إلى عنوان أو شبكة خاطئة مسؤوليتك** ولا يمكن استرجاعه. فريق {{app}} لن يطلب منك أبدًا كلمة المرور أو أي رمز."""

GIVEAWAY_TERMS = """# الظرف الأحمر
- مسابقة **مجانية** لا تتطلب عضوية ولا دفعًا.
- مشاركة واحدة لكل شخص في الجولة، ببريد يُؤكَّد برمز.
- بعد انتهاء الجولة يُسحب الفائزون **عشوائيًا** بين المشاركات المؤكدة، وتُستبعد الحسابات الموقوفة والمشاركات المكررة من نفس الشبكة. يُسجَّل السحب للتدقيق.
- يصل رمز الجائزة إلى البريد فقط، ولن نطلبه منك أبدًا. قد تُعرض أسماء الفائزين (الاسم الظاهر فقط)."""

ABOUT = """# عن {{app}}
مجتمع عربي للمتداولين ولمشاركة أخبار العملات الرقمية والتحليلات والأفكار.
- **السوق**: أعلى العملات ربحًا وخسارة خلال 24 ساعة، للاطلاع فقط.
- **الأفكار**: انشر تحليلك أو خبرك، وناقش في التعليقات.
- **المستخدمون والرسائل**: تعرّف على متداولين آخرين وراسلهم.

بياناتك محمية: لا يرى أحد بريدك أو أي بيانات خاصة عنك إلا ما تختار إظهاره."""

FAQ = """# هل العضوية تعطي أرباحًا؟
لا. العضوية ميزات داخل التطبيق فقط (النجمة، صور الأفكار، صور المحادثة)، ولا ترتبط بأي عائد.

# هل يقدّم التطبيق نصائح تداول؟
لا. السوق والمنشورات للاطلاع والنقاش فقط، وقراراتك مسؤوليتك.

# من يرى بريدي؟
لا أحد من المستخدمين. يراه فريق الإدارة فقط عند الحاجة لتشغيل الخدمة وحمايتها.

# كيف أتواصل مع الفريق؟
حسابي ← الدعم والمساعدة، أو حسابي ← تواصل معنا."""

CONTACT = """أسرع طريقة: **تذكرة دعم** من داخل التطبيق (حسابي ← الدعم والمساعدة). يرد الفريق هنا وعلى بريدك.

رسائل {{app}} الآلية (الرموز والإشعارات) لا تُقرأ الردود عليها. لن نطلب منك كلمة المرور أو أي رمز أبدًا."""

MEMBERSHIP_INTRO = ("العضوية تمنح **ميزات داخل {{app}} فقط**: النجمة الزرقاء، ونشر الأفكار مع صورة، وإرسال صور المحادثة. "
                    "لا ترتبط بأي ربح أو عائد ولا بالتداول.")
WELCOME = "مجتمع للمتداولين وأخبار العملات الرقمية والتحليلات. بريدك وبياناتك لا يراها أحد."


def _pages() -> list[Spec]:
    return [
        Spec("privacy", "سياسة الخصوصية", "page", PRIVACY, ack=True),
        Spec("terms", "شروط الاستخدام", "page", TERMS, ack=True),
        Spec("guidelines", "إرشادات المجتمع", "page", GUIDELINES, ack=True),
        Spec("membership_terms", "شروط العضوية", "page", MEMBERSHIP_TERMS),
        Spec("rewards_terms", "شروط المكافآت", "page", REWARDS_TERMS),
        Spec("withdraw_terms", "شروط السحب", "page", WITHDRAW_TERMS),
        Spec("giveaway_terms", "شروط الظرف الأحمر", "page", GIVEAWAY_TERMS),
        Spec("verification", "النجمة الزرقاء", "page", VERIFICATION),
        Spec("about", "عن التطبيق", "page", ABOUT),
        Spec("faq", "أسئلة شائعة", "page", FAQ),
        Spec("contact", "تواصل معنا", "page", CONTACT),
    ]


def _texts() -> list[Spec]:
    return [
        Spec("market_disclaimer", "تنبيه السوق", "text", lambda s: s.MARKET_DISCLAIMER, note="يظهر أسفل قائمة السوق.",
             setting="MARKET_DISCLAIMER"),
        Spec("membership_intro", "مقدمة «عضويتي»", "text", MEMBERSHIP_INTRO, note="أعلى صفحة العضوية."),
        Spec("welcome", "رسالة الترحيب", "text", WELCOME, note="تحت الشعار في صفحة الدخول والتسجيل."),
        Spec("announcement", "إعلان", "text", "", note="شريط أعلى الرئيسية. اتركه فارغًا لإخفائه."),
        Spec("profile_footer", "تذييل «حسابي»", "text", lambda s: s.FOOTER_TEXT, note="سطر أسفل صفحة حسابي (فارغ = لا شيء).",
             setting="FOOTER_TEXT"),
    ]


def _emails() -> list[Spec]:
    from app.services.mail_templates import TEMPLATES

    return [Spec(k, v[0], "email", v[2], note="المتغيرات: " + " ".join("{" + n + "}" for n in ("app", *v[3]))) for k, v in TEMPLATES.items()]


def registry() -> dict[str, Spec]:
    return {s.key: s for s in (*_pages(), *_texts(), *_emails())}


def spec(key: object) -> Spec:
    reg = registry()
    if not isinstance(key, str) or key not in reg:
        raise not_found()
    return reg[key]


def _default(sp: Spec, settings: Settings) -> str:
    return sp.default(settings) if callable(sp.default) else sp.default


def _default_title(sp: Spec) -> str:
    if sp.kind == "email":
        from app.services.mail_templates import TEMPLATES

        return TEMPLATES[sp.key][1]  # the subject
    return sp.title


# ------------------------------------------------------------------ reading

def block(db: Session, key: str) -> ContentBlock | None:
    return db.get(ContentBlock, key)


def translation(db: Session, key: str, lang: str) -> ContentTranslation | None:
    return db.get(ContentTranslation, (key, lang)) if lang != i18n.DEFAULT else None


def _shipped(sp: Spec, settings: Settings, lang: str) -> tuple[str | None, str] | None:
    """The translation shipped in the code (title, body), or None. A text read from a setting the admin changed has
    no shipped translation (it would not say what the admin wrote)."""
    found = content_i18n.default(sp.key, lang)
    if found is None:
        return None
    if sp.setting:
        field = Settings.model_fields.get(sp.setting)
        if field is None or getattr(settings, sp.setting) != field.default:
            return None
    return found


def _arabic(db: Session, settings: Settings, sp: Spec) -> dict:
    b = block(db, sp.key)
    return {"edited": b is not None, "title": (b.title if b is not None and b.title else _default_title(sp)),
            "body": b.body_md if b is not None else _default(sp, settings),
            "updated_at": b.updated_at if b is not None else None, "major_version": b.major_version if b is not None else 1}


def current(db: Session, settings: Settings, key: str, lang: str | None = None) -> dict:
    """The text as stored (edited) or the default, before variables are filled, in lang (default: the request's).
    "missing": no French/English text exists yet, so the Arabic one is returned."""
    sp = spec(key)
    lang = _lang(lang)
    ar = _arabic(db, settings, sp)
    out = {"key": key, "kind": sp.kind, "name": sp.title, "note": sp.note, "lang": lang, "missing": False, **ar,
           "default_title": _default_title(sp), "default_body": _default(sp, settings)}
    if lang == i18n.DEFAULT:
        return out
    shipped = _shipped(sp, settings, lang)
    if shipped is not None:
        out["default_title"] = shipped[0] or out["default_title"]
        out["default_body"] = shipped[1]
    row = translation(db, key, lang)
    if row is not None:
        out.update(edited=True, title=row.title or out["default_title"], body=row.body_md, updated_at=row.updated_at)
    elif shipped is not None:
        out.update(edited=False, title=out["default_title"], body=shipped[1])
    else:
        out["missing"] = bool(ar["body"])  # nothing in this language: the Arabic text is shown
    return out


def updated_label(cur: dict) -> str:
    when = cur["updated_at"]
    return when.date().isoformat() if isinstance(when, datetime) else DEFAULT_UPDATED


def _text_lang(cur: dict) -> str:
    """The language the text is really written in (the Arabic when the translation is missing)."""
    return i18n.DEFAULT if cur["missing"] else cur["lang"]


def page_html(db: Session, settings: Settings, key: str, lang: str | None = None, cur: dict | None = None) -> str:
    cur = cur or current(db, settings, key, lang)
    return markdown.render(fill(cur["body"], values(db, settings, _text_lang(cur))))


def text(db: Session, settings: Settings, key: str, lang: str | None = None) -> str:
    """A short text as plain text (variables filled, Markdown marks removed)."""
    cur = current(db, settings, key, lang)
    return markdown.plain(fill(cur["body"], values(db, settings, _text_lang(cur)))).strip()


def public(db: Session, settings: Settings, key: str, lang: str | None = None) -> dict:
    sp = spec(key)
    if sp.kind == "email":
        raise not_found()  # e-mail texts are for the panel only
    cur = current(db, settings, key, lang)
    vals = values(db, settings, _text_lang(cur))
    return {"key": key, "title": cur["title"], "html": markdown.render(fill(cur["body"], vals)),
            "text": markdown.plain(fill(cur["body"], vals)).strip(), "updated": updated_label(cur),
            "version": cur["major_version"], "lang": _text_lang(cur), "dir": i18n.direction(_text_lang(cur))}


def page_titles(lang: str | None = None) -> dict[str, str]:
    """{key: title} of the policy pages in lang (the shipped translation; the edited title is read per page)."""
    lang = _lang(lang)
    out = {}
    for k, sp in registry().items():
        if sp.kind == "page":
            shipped = content_i18n.default(k, lang) if lang != i18n.DEFAULT else None
            out[k] = (shipped[0] if shipped and shipped[0] else sp.title)
    return out


def principle(settings: Settings, lang: str | None = None) -> str:
    lang = _lang(lang)
    shipped = content_i18n.principle(lang) if lang != i18n.DEFAULT else None
    return fill(shipped or PRINCIPLE, {"app": settings.APP_NAME})


def email_template(db: Session, key: str, lang: str = "ar") -> tuple[str, str] | None:
    """An edited e-mail text (subject, body) in lang, or None to use the default (services/mail_templates.py)."""
    from app.services.mail_templates import TEMPLATES

    if lang != i18n.DEFAULT:
        row = translation(db, key, lang)
        if row is None:
            return None
        shipped = content_i18n.default(key, lang)
        return (row.title or (shipped[0] if shipped else None) or TEMPLATES[key][1]), row.body_md
    b = block(db, key)
    if b is None:
        return None
    return (b.title or TEMPLATES[key][1]), b.body_md


# ------------------------------------------------------------------ re-consent

def ack_version(db: Session) -> int:
    row = db.get(AppSetting, ACK_KEY)
    try:
        stored = int(row.value) if row is not None else 0
    except (TypeError, ValueError):
        stored = 0
    return max(PRIVACY_VERSION, stored)


def _bump_ack(db: Session) -> int:
    new = ack_version(db) + 1
    row = db.get(AppSetting, ACK_KEY)
    if row is None:
        db.add(AppSetting(key=ACK_KEY, value=str(new), updated_at=clock.utcnow()))
    else:
        row.value, row.updated_at = str(new), clock.utcnow()
    return new


# ------------------------------------------------------------------ editing (panel)

def _edit_lang(lang: object) -> str:
    if lang is None:
        return i18n.DEFAULT
    code = i18n.normalize(lang) if isinstance(lang, str) else None
    if code is None or code != str(lang).strip().lower():
        raise AppError(400, "invalid_lang", "لغة غير معروفة.")
    return code


def _clean_title(title: object, sp: Spec) -> str | None:
    if title is None or sp.kind == "text":
        return None
    if not isinstance(title, str):
        raise AppError(400, "invalid_title", "عنوان غير صالح.")
    t = markdown.clean(title).replace("\n", " ").strip()
    if len(t) > MAX_TITLE:
        raise AppError(400, "invalid_title", "العنوان طويل جدًا.")
    return t or None


def save(db: Session, settings: Settings, key: str, *, body: object, title: object = None, note: object = None,
         major: bool = False, actor: str, lang: object = None) -> dict:
    """Save a text in a language (Arabic by default). A «major change» (re-consent) is for the Arabic original."""
    sp = spec(key)
    lang = _edit_lang(lang)
    if not isinstance(body, str):
        raise AppError(400, "invalid_body", "النص غير صالح.")
    text_ = markdown.clean(body).strip()
    if len(text_) > markdown.MAX_LENGTH - 1:
        raise AppError(400, "body_too_long", "النص طويل جدًا.")
    if sp.kind == "page" and not text_:
        raise AppError(400, "empty_body", "لا يمكن أن تكون الصفحة فارغة. لاستعادة النص الأصلي استعمل «النص الافتراضي».")
    title_ = _clean_title(title, sp)
    note_ = markdown.clean(note).replace("\n", " ").strip()[:200] if isinstance(note, str) else None
    now = clock.utcnow()
    if lang != i18n.DEFAULT:
        row = translation(db, key, lang)
        if row is None:
            db.add(ContentTranslation(key=key, lang=lang, title=title_, body_md=text_, updated_at=now, updated_by=actor))
        else:
            row.title, row.body_md, row.updated_at, row.updated_by = title_, text_, now, actor
        b = block(db, key)
        db.add(ContentRevision(key=key, title=title_, body_md=text_, major_version=b.major_version if b else 1,
                               created_at=now, created_by=actor, note=note_, lang=lang))
        db.flush()
        return {"ack_version": None}
    b = block(db, key)
    if b is None:
        b = ContentBlock(key=key, body_md=text_, title=title_, major_version=1, updated_at=now, updated_by=actor)
        db.add(b)
    else:
        b.body_md, b.title, b.updated_at, b.updated_by = text_, title_, now, actor
    if major:
        b.major_version = (b.major_version or 1) + 1
    ack = _bump_ack(db) if (major and sp.ack) else None
    db.add(ContentRevision(key=key, title=title_, body_md=text_, major_version=b.major_version, created_at=now,
                           created_by=actor, note=note_ or ("تغيير جوهري" if major else None)))
    db.flush()
    return {"ack_version": ack}


def _rev_lang(lang: str):
    return ContentRevision.lang.is_(None) if lang == i18n.DEFAULT else ContentRevision.lang == lang


def revisions(db: Session, key: str, limit: int = 30, lang: object = None) -> list[dict]:
    spec(key)
    lang = _edit_lang(lang)
    rows = db.execute(select(ContentRevision).where(ContentRevision.key == key, _rev_lang(lang))
                      .order_by(ContentRevision.id.desc()).limit(limit)).scalars()
    return [{"id": r.id, "created_at": r.created_at, "created_by": r.created_by, "note": r.note,
             "major_version": r.major_version, "length": len(r.body_md or ""), "lang": r.lang or i18n.DEFAULT} for r in rows]


def restore(db: Session, settings: Settings, key: str, revision_id: object, actor: str) -> str:
    """Restore a revision (in its own language). Returns that language."""
    spec(key)
    if not isinstance(revision_id, int):
        raise not_found()
    r = db.get(ContentRevision, revision_id)
    if r is None or r.key != key:
        raise not_found()
    lang = r.lang or i18n.DEFAULT
    save(db, settings, key, body=r.body_md, title=r.title, note=f"استرجاع النسخة #{r.id}", actor=actor, lang=lang)
    return lang


def reset(db: Session, key: str, actor: str, lang: object = None) -> None:
    """Back to the default text of that language (the history is kept)."""
    sp = spec(key)
    lang = _edit_lang(lang)
    b = block(db, key)
    if lang != i18n.DEFAULT:
        row = translation(db, key, lang)
        if row is not None:
            db.delete(row)
    elif b is not None:
        db.delete(b)
    db.add(ContentRevision(key=sp.key, title=None, body_md="", major_version=b.major_version if b else 1,
                           created_at=clock.utcnow(), created_by=actor, note="رجوع إلى النص الافتراضي",
                           lang=None if lang == i18n.DEFAULT else lang))
    db.flush()


def listing(db: Session, settings: Settings) -> list[dict]:
    """Every key with, per language, «edited» / «default» / «missing» (no text in that language: Arabic shown)."""
    edited = {b.key: b for b in db.execute(select(ContentBlock)).scalars()}
    trans: dict[tuple[str, str], ContentTranslation] = {(r.key, r.lang): r for r in db.execute(select(ContentTranslation)).scalars()}
    out = []
    for sp in registry().values():
        b = edited.get(sp.key)
        langs = {i18n.DEFAULT: "edited" if b is not None else "default"}
        for lang in i18n.LANGS:
            if lang == i18n.DEFAULT:
                continue
            if (sp.key, lang) in trans:
                langs[lang] = "edited"
            elif _shipped(sp, settings, lang) is not None:
                langs[lang] = "default"
            else:
                langs[lang] = "missing" if (b.body_md if b is not None else _default(sp, settings)) else "default"
        out.append({"key": sp.key, "name": sp.title, "kind": sp.kind, "ack": sp.ack, "note": sp.note, "edited": b is not None,
                    "updated_at": b.updated_at if b is not None else None, "updated_by": b.updated_by if b is not None else None,
                    "langs": langs})
    return out


def preview(db: Session, settings: Settings, key: str, body: object, lang: object = None) -> dict:
    sp = spec(key)
    lang = _edit_lang(lang)
    src = markdown.clean(body) if isinstance(body, str) else ""
    if sp.kind == "email":
        from app.services.mail_templates import TEMPLATES, fill as efill

        tr = i18n.tr
        sample = {"app": settings.APP_NAME, "code": "123456", "minutes": "30", "hours": "1", "purpose": tr(N_("سحب الأرباح"), lang),
                  "note": tr(N_("مثال"), lang), "topic": tr(N_("السحب"), lang), "message": tr(N_("نص مثال."), lang),
                  "title": tr(N_("ظرف الأسبوع"), lang), "ticket": "1001", "subject": tr(N_("سؤال"), lang),
                  "kind": "تذكرة جديدة", "category": "أخرى", "public_id": "DZ-ABC123",
                  "mailbox": "بريد النظام", "support_line": ""}
        return {"text": efill(src, sample, TEMPLATES[key][3])}
    return {"html": markdown.render(fill(src, values(db, settings, lang)))}
