// V5: support — a new ticket form + «تذاكري» (my tickets) + one ticket's conversation with the team.
import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, formatListTime, h, toast } from '../ui.js';
import { t } from '../i18n.js';

const CATEGORIES = [
  ['technical', t('مشكلة تقنية')], ['account', t('الحساب')], ['verification', t('التوثيق')], ['payment', t('الدفع')],
  ['report', t('بلاغ')], ['suggestion', t('اقتراح')], ['other', t('أخرى')],
];

function topbar(title, onBack) {
  return h('header', { class: 'topbar topbar--back' },
    h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': t('رجوع'), onclick: onBack }, icon('back')),
    h('h1', { class: 'page-title', text: title }));
}

function statusChip(tk) {
  return h('span', { class: `ticket-status ticket-status--${tk.status}`, text: tk.status_label });
}

export function renderSupport(page, { navigate }) {
  let category = 'technical';
  const chips = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': t('نوع المشكلة') });
  const paintChips = () => chips.replaceChildren(...CATEGORIES.map(([id, label]) => h('button', {
    type: 'button', class: `chip-btn ${id === category ? 'is-on' : ''}`, role: 'radio', 'aria-checked': String(id === category),
    onclick: () => { category = id; paintChips(); },
  }, label)));
  paintChips();
  const subject = h('input', { class: 'input', id: 'ticket-subject', maxlength: '120', placeholder: t('عنوان قصير (اختياري)'), dir: 'auto' });
  const body = h('textarea', { class: 'input', id: 'ticket-body', rows: '5', maxlength: '3200', dir: 'auto',
    placeholder: t('اشرح المشكلة بالتفصيل. لا تكتب كلمة المرور أو أي بيانات بنكية.') });
  autoGrow(body, 260);
  const send = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, icon('send'), t('إرسال إلى الدعم'));
  body.addEventListener('input', () => { send.disabled = !body.value.trim(); });
  const list = h('ul', { class: 'ticket-list' }, h('li', { class: 'muted', text: t('جارٍ التحميل…') }));

  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      const { ticket } = await api.post('/api/support/tickets', { category, subject: subject.value.trim() || null, body: body.value.trim() });
      subject.value = ''; body.value = '';
      toast(t('أُرسلت تذكرتك رقم #{number}. سيصلك الرد هنا وعلى بريدك.', { number: ticket.number }));
      load();
    } catch (err) { toast(err.message, 'error', 4500); send.disabled = false; }
  });

  async function load() {
    try {
      const { tickets } = await api.get('/api/support/tickets');
      if (!tickets.length) { list.replaceChildren(h('li', { class: 'muted', text: t('لا توجد تذاكر بعد.') })); return; }
      list.replaceChildren(...tickets.map((tk) => h('li', {}, h('button', {
        class: `ticket-item glass ${tk.unread ? 'is-unread' : ''}`, type: 'button', onclick: () => navigate(`#/support/${tk.id}`),
      },
      h('span', { class: 'ticket-item__num', text: `#${tk.number}` }),
      h('span', { class: 'ticket-item__main' },
        h('b', { text: tk.subject, dir: 'auto' }),
        h('small', { text: `${tk.category_label} · ${formatListTime(tk.updated_at)}` })),
      tk.unread ? h('span', { class: 'chip chip--hot', text: t('رد جديد') }) : statusChip(tk)))));
    } catch (err) { list.replaceChildren(h('li', { class: 'muted', text: err.message })); }
  }

  page.replaceChildren(
    topbar(t('الدعم والمساعدة'), () => navigate('#/profile')),
    h('section', { class: 'card glass' },
      h('h2', { class: 'card__title', text: t('تذكرة جديدة') }),
      h('p', { class: 'card__note', text: t('يرى فريق الدعم معرّفك العام فقط، ويرد عليك هنا وعلى بريدك المسجّل.') }),
      chips,
      h('div', { class: 'field' }, subject),
      // with the keyboard open, the text you type and its send button stay on screen together
      h('div', { 'data-kb-block': '' }, h('div', { class: 'field' }, body), send)),
    h('h2', { class: 'section-title', text: t('تذاكري') }),
    list,
  );
  load();
  const onReply = () => load();
  document.addEventListener('dz:support', onReply);
  return () => document.removeEventListener('dz:support', onReply);
}

export function renderTicket(page, { navigate, ticketId }) {
  const head = h('div', { class: 'ticket-head' });
  const thread = h('div', { class: 'ticket-thread', 'aria-live': 'polite' });
  const footer = h('div');
  // the same composer as a chat: one line that grows with the text (up to 140px), a round send button
  const input = h('textarea', { rows: '1', maxlength: '3200', dir: 'auto', placeholder: t('اكتب ردك…'), 'aria-label': t('ردك') });
  autoGrow(input, 140);
  const send = h('button', { class: 'send-btn', type: 'button', disabled: true, 'aria-label': t('إرسال الرد') }, icon('send'));
  input.addEventListener('input', () => { send.disabled = !input.value.trim(); });

  async function load() {
    let data;
    try { data = await api.get(`/api/support/tickets/${ticketId}`); }
    catch (err) { toast(err.message, 'error'); navigate('#/support'); return; }
    const tk = data.ticket;
    if (store.state.me) store.state.me.support_unread = 0;
    head.replaceChildren(h('h2', { dir: 'auto', text: `#${tk.number} — ${tk.subject}` }),
      h('p', { class: 'muted' }, `${tk.category_label} · `, statusChip(tk)));
    thread.replaceChildren(...data.messages.map((m) => h('div', { class: `ticket-msg ticket-msg--${m.from}` },
      h('small', { text: m.from === 'me' ? t('أنت') : t('فريق الدعم') }),
      h('p', { dir: 'auto', text: m.body }),
      h('time', { datetime: m.created_at, text: formatListTime(m.created_at) }))));
    if (tk.status === 'closed') {
      footer.replaceChildren(h('p', { class: 'muted', text: t('هذه التذكرة مغلقة. افتح تذكرة جديدة إذا احتجت.') }));
    } else {
      footer.replaceChildren(h('div', { class: 'ticket-composer', 'data-kb-block': '' }, input, send),
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: closeTicket }, t('إغلاق التذكرة (حُلّت المشكلة)')));
    }
  }
  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      await api.post(`/api/support/tickets/${ticketId}/messages`, { body: input.value.trim() });
      input.value = '';
      input.dispatchEvent(new Event('input')); // back to one line
      load();
    } catch (err) { toast(err.message, 'error'); send.disabled = false; }
  });
  async function closeTicket() {
    try { await api.post(`/api/support/tickets/${ticketId}/close`); load(); } catch (err) { toast(err.message, 'error'); }
  }
  page.replaceChildren(topbar(t('تذكرة دعم'), () => navigate('#/support')), head, thread, footer);
  load();
  const onReply = () => load();
  document.addEventListener('dz:support', onReply);
  return () => document.removeEventListener('dz:support', onReply);
}
