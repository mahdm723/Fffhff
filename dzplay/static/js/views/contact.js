// V6 phase 7: «تواصل معنا» — the official support address (set by the admin) and the in-app tickets.
import { contentBlock } from '../content.js';
import { api } from '../api.js';
import { icon } from '../icons.js';
import { h, toast } from '../ui.js';
import { t } from '../i18n.js';

export function renderContact(page, { navigate }) {
  const body = h('section', { class: 'card glass contact' }, h('span', { class: 'spinner', 'aria-label': t('جارٍ التحميل') }));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': t('رجوع'), onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: t('تواصل معنا') })),
    body);

  api.get('/api/contact').then((c) => {
    const email = c.support_email;
    const copy = async () => {
      try { await navigator.clipboard.writeText(email); toast(t('نُسخ العنوان.')); } catch { toast(email); }
    };
    body.replaceChildren(...[
      contentBlock('contact', 'contact__lead'),
      h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: () => navigate('#/support') }, icon('info'), t('فتح تذكرة دعم')),
      email ? h('div', { class: 'contact__mail' },
        h('p', { class: 'muted', text: t('أو راسلنا على بريد الدعم الرسمي:') }),
        h('a', { class: 'contact__addr', href: `mailto:${email}`, dir: 'ltr', text: email }),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: copy }, t('نسخ العنوان'))) : null,
    ].filter(Boolean));
  }).catch((err) => body.replaceChildren(h('p', { class: 'muted', text: err.message })));
}
