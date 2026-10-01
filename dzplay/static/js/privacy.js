// The privacy policy shown in Profile → Privacy and, once, as a notice when it changes.
import { api } from './api.js';
import { h, sheet } from './ui.js';

function protectionSection() {
  return [
    h('h3', { text: 'حماية المستخدمين' }),
    h('ul', {},
      h('li', { text: 'تُفحص الرسائل والتعليقات الخاصة آليًا بحثًا عن التهديد أو الابتزاز أو التحرش أو السب أو طلب الأرقام والحسابات.' }),
      h('li', { text: 'إذا رُصد شيء من ذلك تُحفظ نسخة من ذلك النص فقط ليراجعها المشرف، ولا يُمنع وصول الرسالة.' }),
      h('li', { text: 'إذا وصل بلاغ ضد مستخدم أو رُصدت له رسالة، يمكن للمشرف مراجعة محادثاته المحفوظة على الخادم، بما فيها ردود الطرف الآخر فيها.' }),
      h('li', { text: 'لا يطّلع المشرف على رسائل أي مستخدم آخر. كل اطلاع يُسجَّل، ولا يرى المشرف بريد أي أحد.' }),
    ),
  ];
}

/** `changesFirst`: lead with what changed (the one-time notice). */
export function privacyContent({ changesFirst = false } = {}) {
  return h('div', { class: 'prose' },
    changesFirst ? protectionSection() : null,
    h('h3', { text: 'ما يراه الآخرون' }),
    h('p', { text: 'لا شيء عنك. كل المستخدمين يظهرون باسم dzplay فقط. لا يُعرض بريدك أو رقمك أو موقعك أو أي معرّف داخلي.' }),
    h('h3', { text: 'ما نحتفظ به' }),
    h('ul', {},
      h('li', { text: 'بريدك وكلمة مرور مشفّرة (أو معرّف Google) لتسجيل الدخول فقط.' }),
      h('li', { text: 'الرسائل مؤقتة: تُحذف من الخادم تلقائيًا بعد قراءتها بوقت قصير أو بعد انتهاء مدتها.' }),
      h('li', { text: 'عناوين IP تُحفظ مشفّرة (بصمة غير قابلة للعكس) فقط لمنع الإساءة والمحاولات الآلية.' }),
    ),
    changesFirst ? null : protectionSection(),
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
export function showPrivacyNotice(me) {
  if (!me || !me.privacy_notice) return;
  const ack = () => {
    me.privacy_notice = false;
    api.post('/api/me/privacy-ack').catch(() => {}); // shown again next time if this fails
  };
  sheet((panel, close) => {
    panel.classList.add('privacy-notice');
    panel.append(
      h('h2', { text: 'تحديث في سياسة الخصوصية' }),
      h('p', { class: 'privacy-notice__lead', text: 'لحمايتك من التهديد والابتزاز والتحرش، أضفنا مراجعة آلية للمحتوى المسيء. هذا ما تغيّر، ثم باقي السياسة كما هي:' }),
      privacyContent({ changesFirst: true }),
      h('div', { class: 'actions' }, h('button', { class: 'btn btn--primary btn--block', onclick: close }, 'فهمت')),
    );
  }, ack);
}
