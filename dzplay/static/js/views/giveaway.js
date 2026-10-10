// V6 phase 5c: «الظرف الأحمر» — enter the current round with a confirmed e-mail; winners are drawn at random after
// the end and receive their prize code by e-mail (never in the app).
import { api } from '../api.js';
import { icon } from '../icons.js';
import { formatDay, formatTime, h, toast } from '../ui.js';

export function renderGiveaway(page, { navigate }) {
  const body = h('div', { class: 'giveaway' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'الظرف الأحمر' })),
    body);

  function codeStep(r) {
    const code = h('input', { class: 'input', id: 'gw-code', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: '000000' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'تأكيد البريد');
    const err = h('div', { class: 'form-error', role: 'alert' });
    go.addEventListener('click', async () => {
      err.textContent = '';
      go.disabled = true;
      try { await api.post(`/api/giveaway/${r.id}/confirm`, { code: code.value.trim() }); toast('تمت مشاركتك 🧧'); load(); }
      catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    return h('div', { class: 'gw-step' }, h('div', { class: 'field' }, h('label', { for: 'gw-code', text: 'الرمز الذي وصلك بالبريد' }), code), err, go);
  }

  function joinForm(d) {
    const r = d.round;
    const other = h('input', { class: 'input', id: 'gw-email', type: 'email', dir: 'ltr', autocomplete: 'email', placeholder: 'بريد آخر (اختياري)' });
    const err = h('div', { class: 'form-error', role: 'alert' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, '🧧 شارك الآن');
    const slot = h('div');
    go.addEventListener('click', async () => {
      err.textContent = '';
      go.disabled = true;
      try {
        const res = await api.post(`/api/giveaway/${r.id}/enter`, other.value.trim() ? { email: other.value.trim() } : {});
        if (res.confirmed) { toast('تمت مشاركتك 🧧'); load(); return; }
        toast(`أُرسل رمز إلى ${res.sent_to}`);
        slot.replaceChildren(codeStep(r));
      } catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    return h('section', { class: 'gw-join glass' },
      h('p', { text: `يصل رمز الجائزة إلى بريدك إن ربحت. الافتراضي بريد حسابك (${d.account_email})${d.account_email_verified ? '، وهو مؤكد.' : '، ونؤكده برمز مرة واحدة.'}` }),
      h('div', { class: 'field' }, h('label', { for: 'gw-email', text: 'أو بريد آخر' }), other), err, go, slot);
  }

  function paint(d) {
    const r = d.round;
    if (!r) {
      body.replaceChildren(h('div', { class: 'empty glass' }, h('div', { class: 'gw-emoji', text: '🧧' }), h('h2', { text: 'لا توجد جولة حاليًا' }),
        h('p', { text: 'ستظهر هنا الجولة القادمة. تابع الإشعارات.' })));
      return;
    }
    const head = h('section', { class: 'gw-head glass' }, h('div', { class: 'gw-emoji', text: '🧧' }),
      h('div', {}, h('h2', { text: r.title }), r.description ? h('p', { dir: 'auto', text: r.description }) : '',
        h('p', { class: 'muted', text: `${r.winners_count} فائز · ${r.entries} مشارك مؤكد · ${r.open ? `تنتهي ${formatDay(r.ends_at)} ${formatTime(r.ends_at)}` : 'انتهت المشاركة'}` })));
    let main;
    if (d.entry && d.entry.confirmed) main = h('p', { class: 'gw-ok' }, icon('check'), `أنت مشارك ببريد ${d.entry.email}. حظًا موفقًا!`);
    else if (d.entry && r.open) main = h('section', { class: 'gw-join glass' }, h('p', { text: `أكّد بريدك ${d.entry.email} لتكتمل مشاركتك.` }), codeStep(r));
    else if (r.open) main = joinForm(d);
    else main = h('p', { class: 'muted', text: 'انتهت المشاركة في هذه الجولة.' });
    body.replaceChildren(head, main,
      r.i_won ? h('p', { class: 'gw-ok' }, '🎉 ربحت! أُرسل رمز الجائزة إلى بريدك.') : '',
      r.winners ? h('section', { class: 'gw-winners glass' }, h('h3', { text: 'الفائزون' }), h('ul', {}, ...r.winners.map((n) => h('li', { dir: 'auto', text: n })))) : '',
      h('p', { class: 'muted gw-terms', text: 'المشاركة مجانية ولا تتطلب عضوية. السحب عشوائي ويُسجَّل للتدقيق. لن نطلب منك رمز الجائزة أبدًا.' }));
  }

  async function load() {
    try { paint(await api.get('/api/giveaway')); }
    catch (err) { body.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('p', { text: err.message }))); }
  }
  load();
}
