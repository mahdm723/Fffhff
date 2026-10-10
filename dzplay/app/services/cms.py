"""V6 phase 8: the content system — every non-essential text (policies, terms, help pages, notices, e-mails)
editable from the panel, with a revision history and «major change» re-consent.

* pages  — /policies/<key> (Markdown, rendered by services/markdown.py), with «آخر تحديث».
* texts  — short texts used inside the app (market disclaimer, membership intro, welcome, announcement, footer).
* emails — the e-mail texts (email.*, services/mail_templates.py): subject = title, body = plain text.

Defaults live in the code (below); an edited text is a ContentBlock row, each save a ContentRevision. Texts can
use {{variables}} filled from the running settings (TTLs, prices, limits…), so they keep matching what the app
really does; only the names in values() are replaced. No personal information about the owner, anywhere.
Reads go straight to the database (a few small rows by primary key): always current on every instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import clock
from app.config import PRIVACY_VERSION, Settings
from app.errors import AppError, not_found
from app.models import AppSetting, ContentBlock, ContentRevision
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


def _days(seconds: int) -> str:
    d = seconds / 86400
    return f"{d:g} يوم" if d >= 1 else f"{max(1, seconds // 3600)} ساعة"


def values(db: Session | None, settings: Settings) -> dict[str, str]:
    """The {{variables}} available in pages and texts (all plain text)."""
    s = settings
    support = ""
    if db is not None:
        from app.services import mail

        support = mail.support_address(db, s)
    nets = [n.strip() for n in str(s.WITHDRAW_NETWORKS).split(",") if n.strip()]
    return {
        "app": s.APP_NAME,
        "message_ttl": _days(s.MESSAGE_TTL),
        "message_ttl_read": _days(s.MESSAGE_TTL_AFTER_READ),
        "conversation_idle": _days(s.CONVERSATION_IDLE_TTL),
        "legacy_anon_days": str(s.LEGACY_ANON_RETENTION_DAYS),
        "chat_ttl": str(s.CHAT_IMAGE_TTL_AFTER_VIEW),
        "chat_unopened": _days(s.CHAT_IMAGE_UNOPENED_TTL),
        "chat_grace_min": str(max(1, s.CHAT_IMAGE_REPORT_GRACE // 60)),
        "chat_archive": ("لا نؤرشف صور المحادثات: بعد انتهائها تُحذف نهائيًا (إلا إذا أُبلغ عنها)." if not s.CHAT_IMAGE_ARCHIVE else
                         "إعداد الأرشفة مفعّل حاليًا: تُحفظ نسخة من صور المحادثات بعد انتهائها في مستودع التخزين الخاص ولا يراها أي مستخدم."),
        "evidence_days": str(s.MEDIA_EVIDENCE_RETENTION_DAYS),
        "report_retention": _days(s.REPORT_RETENTION),
        "security_retention": _days(s.SECURITY_EVENT_RETENTION),
        "audit_retention": _days(s.AUDIT_LOG_RETENTION),
        "cache_ttl": _days(s.MEDIA_CACHE_TTL),
        "idea_image_limit": str(s.IDEA_IMAGE_LIMIT_PER_24H),
        "avatar_per_day": str(s.AVATAR_CHANGES_PER_DAY),
        "report_threshold": str(s.REPORT_AUTO_HIDE_THRESHOLD or "—"),
        "membership_price": f"{s.MEMBERSHIP_PRICE:g}",
        "refund_days": str(s.MEMBERSHIP_REFUND_WINDOW_DAYS),
        "refund_fee": f"{s.MEMBERSHIP_REFUND_FEE:g}",
        "refund_policy": (f"يمكن طلب استرجاعها خلال {s.MEMBERSHIP_REFUND_WINDOW_DAYS} أيام من تفعيلها، ويُعاد المبلغ ناقص رسوم "
                          f"{s.MEMBERSHIP_REFUND_FEE:g} USDT، وتتوقف الميزات عند الإرجاع." if s.MEMBERSHIP_REFUNDABLE
                          else "لا تُسترجع قيمة العضوية بعد تفعيلها."),
        "referral_reward": f"{s.REFERRAL_REWARD:g}",
        "referral_hold_days": str(s.REFERRAL_HOLD_DAYS),
        "withdraw_min": f"{s.WITHDRAW_MIN:g}",
        "withdraw_fee": f"{s.WITHDRAW_FEE:g}",
        "withdraw_networks": " و".join(nets) or "—",
        "support_email": support or "—",
        "support_line": f"، أو على بريد الدعم الرسمي {support}" if support else "",
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
        Spec("market_disclaimer", "تنبيه السوق", "text", lambda s: s.MARKET_DISCLAIMER, note="يظهر أسفل قائمة السوق."),
        Spec("membership_intro", "مقدمة «عضويتي»", "text", MEMBERSHIP_INTRO, note="أعلى صفحة العضوية."),
        Spec("welcome", "رسالة الترحيب", "text", WELCOME, note="تحت الشعار في صفحة الدخول والتسجيل."),
        Spec("announcement", "إعلان", "text", "", note="شريط أعلى الرئيسية. اتركه فارغًا لإخفائه."),
        Spec("profile_footer", "تذييل «حسابي»", "text", lambda s: s.FOOTER_TEXT, note="سطر أسفل صفحة حسابي (فارغ = لا شيء)."),
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


def current(db: Session, settings: Settings, key: str) -> dict:
    """The text as stored (edited) or the default, before variables are filled."""
    sp = spec(key)
    b = block(db, key)
    return {"key": key, "kind": sp.kind, "name": sp.title, "note": sp.note, "edited": b is not None,
            "title": (b.title if b is not None and b.title else _default_title(sp)),
            "body": b.body_md if b is not None else _default(sp, settings),
            "default_title": _default_title(sp), "default_body": _default(sp, settings),
            "updated_at": b.updated_at if b is not None else None, "major_version": b.major_version if b is not None else 1}


def updated_label(cur: dict) -> str:
    when = cur["updated_at"]
    return when.date().isoformat() if isinstance(when, datetime) else DEFAULT_UPDATED


def page_html(db: Session, settings: Settings, key: str) -> str:
    cur = current(db, settings, key)
    return markdown.render(fill(cur["body"], values(db, settings)))


def text(db: Session, settings: Settings, key: str) -> str:
    """A short text as plain text (variables filled, Markdown marks removed)."""
    return markdown.plain(fill(current(db, settings, key)["body"], values(db, settings))).strip()


def public(db: Session, settings: Settings, key: str) -> dict:
    sp = spec(key)
    if sp.kind == "email":
        raise not_found()  # e-mail texts are for the panel only
    cur = current(db, settings, key)
    return {"key": key, "title": cur["title"], "html": page_html(db, settings, key), "text": text(db, settings, key),
            "updated": updated_label(cur), "version": cur["major_version"]}


def principle(settings: Settings) -> str:
    return fill(PRINCIPLE, {"app": settings.APP_NAME})


def email_template(db: Session, key: str) -> tuple[str, str] | None:
    """An edited e-mail text (subject, body), or None to use the default (services/mail_templates.py)."""
    b = block(db, key)
    if b is None:
        return None
    from app.services.mail_templates import TEMPLATES

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
         major: bool = False, actor: str) -> dict:
    sp = spec(key)
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


def revisions(db: Session, key: str, limit: int = 30) -> list[dict]:
    spec(key)
    rows = db.execute(select(ContentRevision).where(ContentRevision.key == key)
                      .order_by(ContentRevision.id.desc()).limit(limit)).scalars()
    return [{"id": r.id, "created_at": r.created_at, "created_by": r.created_by, "note": r.note,
             "major_version": r.major_version, "length": len(r.body_md or "")} for r in rows]


def restore(db: Session, settings: Settings, key: str, revision_id: object, actor: str) -> None:
    spec(key)
    if not isinstance(revision_id, int):
        raise not_found()
    r = db.get(ContentRevision, revision_id)
    if r is None or r.key != key:
        raise not_found()
    save(db, settings, key, body=r.body_md, title=r.title, note=f"استرجاع النسخة #{r.id}", actor=actor)


def reset(db: Session, key: str, actor: str) -> None:
    """Back to the default text (the history is kept)."""
    sp = spec(key)
    b = block(db, key)
    if b is not None:
        db.delete(b)
    db.add(ContentRevision(key=sp.key, title=None, body_md="", major_version=b.major_version if b else 1,
                           created_at=clock.utcnow(), created_by=actor, note="رجوع إلى النص الافتراضي"))
    db.flush()


def listing(db: Session) -> list[dict]:
    edited = {b.key: b for b in db.execute(select(ContentBlock)).scalars()}
    out = []
    for sp in registry().values():
        b = edited.get(sp.key)
        out.append({"key": sp.key, "name": sp.title, "kind": sp.kind, "ack": sp.ack, "note": sp.note, "edited": b is not None,
                    "updated_at": b.updated_at if b is not None else None, "updated_by": b.updated_by if b is not None else None})
    return out


def preview(db: Session, settings: Settings, key: str, body: object) -> dict:
    sp = spec(key)
    src = markdown.clean(body) if isinstance(body, str) else ""
    if sp.kind == "email":
        from app.services.mail_templates import TEMPLATES, fill as efill

        sample = {"app": settings.APP_NAME, "code": "123456", "minutes": "30", "hours": "1", "purpose": "السحب",
                  "note": "مثال", "topic": "السحب", "message": "نص مثال.", "title": "ظرف الأسبوع", "ticket": "1001",
                  "subject": "سؤال", "kind": "تذكرة جديدة", "category": "أخرى", "public_id": "DZ-ABC123",
                  "mailbox": "بريد النظام", "support_line": ""}
        return {"text": efill(src, sample, TEMPLATES[key][3])}
    return {"html": markdown.render(fill(src, values(db, settings)))}
