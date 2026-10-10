// V6 phase 7: «تواصل معنا» — the official support address (set by the admin) and the in-app tickets.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { h, toast } from '../ui.js';

export function renderContact(page, { navigate }) {
  const body = h('section', { class: 'card glass contact' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' }));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'تواصل معنا' })),
    body);

  api.get('/api/contact').then((c) => {
    const email = c.support_email;
    const copy = async () => {
      try { await navigator.clipboard.writeText(email); toast('نُسخ العنوان.'); } catch { toast(email); }
    };
    body.replaceChildren(...[
      h('p', { class: 'contact__lead', text: 'أسرع طريقة: تذكرة دعم من داخل التطبيق. يرد الفريق هنا وعلى بريدك.' }),
      h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: () => navigate('#/support') }, icon('info'), 'فتح تذكرة دعم'),
      email ? h('div', { class: 'contact__mail' },
        h('p', { class: 'muted', text: 'أو راسلنا على بريد الدعم الرسمي:' }),
        h('a', { class: 'contact__addr', href: `mailto:${email}`, dir: 'ltr', text: email }),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: copy }, 'نسخ العنوان')) : null,
      h('p', { class: 'muted contact__note', text: `رسائل ${c.app_name} الآلية (الرموز والإشعارات) لا تُقرأ الردود عليها. لن نطلب منك كلمة المرور أو أي رمز أبدًا.` }),
    ].filter(Boolean));
  }).catch((err) => body.replaceChildren(h('p', { class: 'muted', text: err.message })));
}
