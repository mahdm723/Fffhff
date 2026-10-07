// The privacy policy shown in Profile → Privacy and, once, as a notice when it changes.
import { api } from './api.js';
import { h, sheet } from './ui.js';

function v5Section() {
  return [
    h('h3', { text: 'الصور والنجمة الزرقاء' }),
    h('ul', {},
      h('li', { text: 'الصور تُحفظ في مستودع خاص على Telegram يملكه DZPLAY، ويمرّرها خادمنا فقط. تُحذف منها كل البيانات الوصفية (الموقع ونوع الجهاز والتاريخ) وتُفحص آليًا قبل الحفظ.' }),
      h('li', { text: 'صور المحادثات مموّهة حتى تفتحها، وتختفي عند الطرفين بعد مدة قصيرة، ولا يراها فريق الإشراف إلا إذا أُبلغ عنها.' }),
      h('li', { text: 'النجمة الزرقاء علامة حساب رسمي داخل المنصة، وليست تحققًا من الهوية، ولا نطلب أي وثيقة.' }),
      h('li', { text: 'يمكنك حذف حسابك بنفسك من حسابي ← حذف حسابي.' }),
    ),
    h('p', {}, h('a', { href: '/policies/privacy', target: '_blank', rel: 'noopener' }, 'سياسة الخصوصية كاملة'), ' · ',
      h('a', { href: '/policies', target: '_blank', rel: 'noopener' }, 'كل السياسات والشروط')),
  ];
}

function whatsNewSection() {
  return [
    h('h3', { text: 'الأسماء والمعرّف' }),
    h('ul', {},
      h('li', { text: 'لكل حساب اسم يختاره صاحبه (أو dzplay) ومعرّف عام مثل DZ-7K4P2M يظهران للآخرين. البريد لا يظهر أبدًا.' }),
      h('li', { text: 'الجنس اختياري ويظهر كأيقونة صغيرة فقط، ولا يظهر إن اخترت "أفضّل عدم الذكر".' }),
      h('li', { text: 'الرسائل العشوائية تبقى مجهولة باسم dzplay. لا تُكشف هويتك إلا إذا اخترت "كشف هويتي" بنفسك.' }),
      h('li', { text: 'يمكنك إيقاف الرسائل المجهولة أو المباشرة أو الظهور في البحث بالاسم من حسابي ← الخصوصية والتواصل.' }),
    ),
  ];
}

function protectionSection() {
  return [
    h('h3', { text: 'ما يستطيع فريق DZPLAY الاطلاع عليه' }),
    h('ul', {},
      h('li', { text: 'لتشغيل التطبيق والإشراف عليه وحماية المستخدمين، يستطيع فريق إدارة DZPLAY الاطلاع على بيانات الحسابات (ومنها البريد الإلكتروني وتاريخ التسجيل والنشاط)، والمنشورات، والتعليقات بما فيها التعليقات الخاصة، والرسائل والمحادثات ما دامت محفوظة على الخادم.' }),
      h('li', { text: 'الوصول محمي بحساب مشرف وتحقق بخطوتين، وكل اطلاع وكل إجراء يُسجَّل في سجل داخلي.' }),
      h('li', { text: 'تُفحص الرسائل والتعليقات آليًا بحثًا عن التهديد أو الابتزاز أو التحرش أو السب أو طلب الأرقام والحسابات، ويُحفظ النص المرصود للمراجعة.' }),
      h('li', { text: 'قد يضيف فريق DZPLAY تعليقات لتنشيط المحتوى، تظهر باسم dzplay أو باسم الحساب الرسمي. أعداد الإعجاب وعدم الإعجاب حقيقية دائمًا.' }),
    ),
    h('h3', { text: 'ما لا يتغير' }),
    h('p', { text: 'لا يعرف الآخرون بريدك أو رقمك أو موقعك، وتعليقات أفكارك يراها أنت فقط.' }),
  ];
}

/** `changesFirst`: lead with what changed (the one-time notice). */
export function privacyContent({ changesFirst = false } = {}) {
  return h('div', { class: 'prose' },
    changesFirst ? v5Section() : null,
    h('h3', { text: 'ما يراه الآخرون' }),
    h('p', { text: 'اسمك الظاهر ومعرّفك العام DZ وأيقونة الجنس إن اخترتها، في الأفكار والتعليقات والمحادثات المباشرة. في الرسائل العشوائية تظهر باسم dzplay ما لم تكشف هويتك. لا يُعرض لهم بريدك أو رقمك أو موقعك أو أي معرّف داخلي.' }),
    h('h3', { text: 'ما نحتفظ به' }),
    h('ul', {},
      h('li', { text: 'بريدك وكلمة مرور مشفّرة (أو معرّف Google) لتسجيل الدخول فقط.' }),
      h('li', { text: 'الرسائل مؤقتة: تُحذف من الخادم تلقائيًا بعد قراءتها بوقت قصير أو بعد انتهاء مدتها.' }),
      h('li', { text: 'عناوين IP لا تُحفظ كما هي: نحفظ بصمة مشفّرة غير قابلة للعكس فقط لمنع الإساءة والمحاولات الآلية.' }),
    ),
    whatsNewSection(),
    changesFirst ? null : v5Section(),
    protectionSection(),
    h('h3', { text: 'على جهازك' }),
    h('p', { text: 'نسخة من محادثاتك تُحفظ على جهازك لتبقى مرئية لك، وتُمسح عند تسجيل الخروج.' }),
  );
}

export function privacySheet() {
  sheet((panel, close) => {
    panel.append(
      h('h2', { text: 'الخصوصية في DZPLAY' }),
      privacyContent(),
      h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', onclick: close }, 'إغلاق')),
    );
  });
}

/** Shown once to accounts that have not seen the current policy (me.privacy_notice). */
export function showPrivacyNotice(me, then = () => {}) {
  if (!me || !me.privacy_notice) { then(); return; }
  const ack = () => {
    me.privacy_notice = false;
    api.post('/api/me/privacy-ack').catch(() => {}); // shown again next time if this fails
    then();
  };
  sheet((panel, close) => {
    panel.classList.add('privacy-notice');
    panel.append(
      h('h2', { text: 'تحديث في سياسة الخصوصية' }),
      h('p', { class: 'privacy-notice__lead', text: 'حدّثنا سياسة الخصوصية. هذا ما تغيّر، ثم باقي السياسة:' }),
      privacyContent({ changesFirst: true }),
      h('div', { class: 'actions' }, h('button', { class: 'btn btn--primary btn--block', onclick: close }, 'فهمت')),
    );
  }, ack);
}
