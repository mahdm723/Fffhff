// V6 phase 5c: «الظرف الأحمر» — enter the current round with a confirmed e-mail; winners are drawn at random after
// the end and receive their prize code by e-mail (never in the app).
import { api } from '../api.js';
import { icon } from '../icons.js';
import { formatDay, formatTime, h, toast } from '../ui.js';
import { t } from '../i18n.js';

export function renderGiveaway(page, { navigate }) {
  const body = h('div', { class: 'giveaway' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': t('جارٍ التحميل') })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': t('رجوع'), onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: t('الظرف الأحمر') })),
    body);

  function codeStep(r) {
    const code = h('input', { class: 'input', id: 'gw-code', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: '000000' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, t('تأكيد البريد'));
    const err = h('div', { class: 'form-error', role: 'alert' });
    go.addEventListener('click', async () => {
      err.textContent = '';
      go.disabled = true;
      try { await api.post(`/api/giveaway/${r.id}/confirm`, { code: code.value.trim() }); toast(t('تمت مشاركتك 🧧')); load(); }
      catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    return h('div', { class: 'gw-step' }, h('div', { class: 'field' }, h('label', { for: 'gw-code', text: t('الرمز الذي وصلك بالبريد') }), code), err, go);
  }

  function joinForm(d) {
    const r = d.round;
    const other = h('input', { class: 'input', id: 'gw-email', type: 'email', dir: 'ltr', autocomplete: 'email', placeholder: t('بريد آخر (اختياري)') });
    const err = h('div', { class: 'form-error', role: 'alert' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, t('🧧 شارك الآن'));
    const slot = h('div');
    go.addEventListener('click', async () => {
      err.textContent = '';
      go.disabled = true;
      try {
        const res = await api.post(`/api/giveaway/${r.id}/enter`, other.value.trim() ? { email: other.value.trim() } : {});
        if (res.confirmed) { toast(t('تمت مشاركتك 🧧')); load(); return; }
        toast(t('أُرسل رمز إلى {sent_to}', { sent_to: res.sent_to }));
        slot.replaceChildren(codeStep(r));
      } catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    return h('section', { class: 'gw-join glass' },
      h('p', { text: d.account_email_verified ? t('يصل رمز الجائزة إلى بريدك إن ربحت. الافتراضي بريد حسابك ({email})، وهو مؤكد.', { email: d.account_email }) : t('يصل رمز الجائزة إلى بريدك إن ربحت. الافتراضي بريد حسابك ({email})، ونؤكده برمز مرة واحدة.', { email: d.account_email }) }),
      h('div', { class: 'field' }, h('label', { for: 'gw-email', text: t('أو بريد آخر') }), other), err, go, slot);
  }

  function paint(d) {
    const r = d.round;
    if (!r) {
      body.replaceChildren(h('div', { class: 'empty glass' }, h('div', { class: 'gw-emoji', text: '🧧' }), h('h2', { text: t('لا توجد جولة حاليًا') }),
        h('p', { text: t('ستظهر هنا الجولة القادمة. تابع الإشعارات.') })));
      return;
    }
    const head = h('section', { class: 'gw-head glass' }, h('div', { class: 'gw-emoji', text: '🧧' }),
      h('div', {}, h('h2', { text: r.title }), r.description ? h('p', { dir: 'auto', text: r.description }) : '',
        h('p', { class: 'muted', text: r.open ? t('{winners} فائز · {entries} مشارك مؤكد · تنتهي {day} {time}', { winners: r.winners_count, entries: r.entries, day: formatDay(r.ends_at), time: formatTime(r.ends_at) }) : t('{winners} فائز · {entries} مشارك مؤكد · انتهت المشاركة', { winners: r.winners_count, entries: r.entries }) })));
    let main;
    if (d.entry && d.entry.confirmed) main = h('p', { class: 'gw-ok' }, icon('check'), t('أنت مشارك ببريد {email}. حظًا موفقًا!', { email: d.entry.email }));
    else if (d.entry && r.open) main = h('section', { class: 'gw-join glass' }, h('p', { text: t('أكّد بريدك {email} لتكتمل مشاركتك.', { email: d.entry.email }) }), codeStep(r));
    else if (r.open) main = joinForm(d);
    else main = h('p', { class: 'muted', text: t('انتهت المشاركة في هذه الجولة.') });
    body.replaceChildren(head, main,
      r.i_won ? h('p', { class: 'gw-ok' }, t('🎉 ربحت! أُرسل رمز الجائزة إلى بريدك.')) : '',
      r.winners ? h('section', { class: 'gw-winners glass' }, h('h3', { text: t('الفائزون') }), h('ul', {}, ...r.winners.map((n) => h('li', { dir: 'auto', text: n })))) : '',
      h('p', { class: 'muted gw-terms', text: t('المشاركة مجانية ولا تتطلب عضوية. السحب عشوائي ويُسجَّل للتدقيق. لن نطلب منك رمز الجائزة أبدًا.') }));
  }

  async function load() {
    try { paint(await api.get('/api/giveaway')); }
    catch (err) { body.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('p', { text: err.message }))); }
  }
  load();
}
