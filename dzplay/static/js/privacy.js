// The privacy policy shown in Profile → Privacy and, once, as a notice when the policies change.
// V6 phase 8: the text comes from the content system (editable from the panel), like the /policies pages.
import { api } from './api.js';
import { appName } from './brand.js';
import { forgetContent, loadContent, safeHtml } from './content.js';
import { h, sheet } from './ui.js';
import { t } from './i18n.js';

function links() {
  const a = (href, text) => h('a', { href, target: '_blank', rel: 'noopener' }, text);
  return h('p', { class: 'policy-links' }, a('/policies/privacy', t('سياسة الخصوصية')), ' · ', a('/policies/terms', t('شروط الاستخدام')),
    ' · ', a('/policies/guidelines', t('إرشادات المجتمع')), ' · ', a('/policies', t('كل السياسات')));
}

function policy(key) {
  const box = h('div', { class: 'prose' }, h('p', { class: 'muted', text: t('جارٍ التحميل…') }));
  loadContent(key).then((c) => box.replaceChildren(...(c
    ? [safeHtml(c.html), h('p', { class: 'muted', text: t('آخر تحديث: {updated}', { updated: c.updated }) })]
    : [h('p', { text: t('تعذّر التحميل. افتح الرابط أدناه.') })])));
  return box;
}

export function privacySheet() {
  sheet((panel, close) => {
    panel.append(
      h('h2', { text: t('الخصوصية في {app}', { app: appName() }) }),
      policy('privacy'),
      links(),
      h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', onclick: close }, t('إغلاق'))),
    );
  });
}

/** Shown once to accounts that have not accepted the current version of the policies (me.privacy_notice). */
export function showPrivacyNotice(me, then = () => {}) {
  if (!me || !me.privacy_notice) { then(); return; }
  forgetContent(); // the text has just changed
  const ack = () => {
    me.privacy_notice = false;
    api.post('/api/me/privacy-ack').catch(() => {}); // shown again next time if this fails
    then();
  };
  sheet((panel, close) => {
    panel.classList.add('privacy-notice');
    panel.append(
      h('h2', { text: t('تحديث في سياسة الخصوصية') }),
      h('p', { class: 'privacy-notice__lead', text: t('حدّثنا سياسات {app}. اقرأها، ويمكنك فتح الشروط والإرشادات من الروابط:', { app: appName() }) }),
      links(),
      policy('privacy'),
      h('div', { class: 'actions' }, h('button', { class: 'btn btn--primary btn--block', onclick: close }, t('فهمت'))),
    );
  }, ack);
}
