// V5: support — a new ticket form + «تذاكري» (my tickets) + one ticket's conversation with the team.
import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, formatListTime, h, toast } from '../ui.js';

const CATEGORIES = [
  ['technical', 'مشكلة تقنية'], ['account', 'الحساب'], ['verification', 'التوثيق'], ['payment', 'الدفع'],
  ['report', 'بلاغ'], ['suggestion', 'اقتراح'], ['other', 'أخرى'],
];

function topbar(title, onBack) {
  return h('header', { class: 'topbar topbar--back' },
    h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: onBack }, icon('back')),
    h('h1', { class: 'page-title', text: title }));
}

function statusChip(t) {
  return h('span', { class: `ticket-status ticket-status--${t.status}`, text: t.status_label });
}

export function renderSupport(page, { navigate }) {
  let category = 'technical';
  const chips = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': 'نوع المشكلة' });
  const paintChips = () => chips.replaceChildren(...CATEGORIES.map(([id, label]) => h('button', {
    type: 'button', class: `chip-btn ${id === category ? 'is-on' : ''}`, role: 'radio', 'aria-checked': String(id === category),
    onclick: () => { category = id; paintChips(); },
  }, label)));
  paintChips();
  const subject = h('input', { class: 'input', id: 'ticket-subject', maxlength: '120', placeholder: 'عنوان قصير (اختياري)', dir: 'auto' });
  const body = h('textarea', { class: 'input', id: 'ticket-body', rows: '5', maxlength: '3200', dir: 'auto',
    placeholder: 'اشرح المشكلة بالتفصيل. لا تكتب كلمة المرور أو أي بيانات بنكية.' });
  autoGrow(body, 260);
  const send = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, icon('send'), 'إرسال إلى الدعم');
  body.addEventListener('input', () => { send.disabled = !body.value.trim(); });
  const list = h('ul', { class: 'ticket-list' }, h('li', { class: 'muted', text: 'جارٍ التحميل…' }));

  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      const { ticket } = await api.post('/api/support/tickets', { category, subject: subject.value.trim() || null, body: body.value.trim() });
      subject.value = ''; body.value = '';
      toast(`أُرسلت تذكرتك رقم #${ticket.number}. سيصلك الرد هنا وعلى بريدك.`);
      load();
    } catch (err) { toast(err.message, 'error', 4500); send.disabled = false; }
  });

  async function load() {
    try {
      const { tickets } = await api.get('/api/support/tickets');
      if (!tickets.length) { list.replaceChildren(h('li', { class: 'muted', text: 'لا توجد تذاكر بعد.' })); return; }
      list.replaceChildren(...tickets.map((t) => h('li', {}, h('button', {
        class: `ticket-item glass ${t.unread ? 'is-unread' : ''}`, type: 'button', onclick: () => navigate(`#/support/${t.id}`),
      },
      h('span', { class: 'ticket-item__num', text: `#${t.number}` }),
      h('span', { class: 'ticket-item__main' },
        h('b', { text: t.subject, dir: 'auto' }),
        h('small', { text: `${t.category_label} · ${formatListTime(t.updated_at)}` })),
      t.unread ? h('span', { class: 'chip chip--hot', text: 'رد جديد' }) : statusChip(t)))));
    } catch (err) { list.replaceChildren(h('li', { class: 'muted', text: err.message })); }
  }

  page.replaceChildren(
    topbar('الدعم والمساعدة', () => navigate('#/profile')),
    h('section', { class: 'card glass', 'data-kb-block': '' },
      h('h2', { class: 'card__title', text: 'تذكرة جديدة' }),
      h('p', { class: 'card__note', text: 'يرى فريق الدعم معرّفك العام فقط، ويرد عليك هنا وعلى بريدك المسجّل.' }),
      chips,
      h('div', { class: 'field' }, subject),
      h('div', { class: 'field' }, body),
      send),
    h('h2', { class: 'section-title', text: 'تذاكري' }),
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
  const input = h('textarea', { class: 'input', rows: '2', maxlength: '3200', dir: 'auto', placeholder: 'اكتب ردك…', 'aria-label': 'ردك' });
  autoGrow(input, 200);
  const send = h('button', { class: 'btn btn--primary', type: 'button', disabled: true }, icon('send'), 'إرسال');
  input.addEventListener('input', () => { send.disabled = !input.value.trim(); });

  async function load() {
    let data;
    try { data = await api.get(`/api/support/tickets/${ticketId}`); }
    catch (err) { toast(err.message, 'error'); navigate('#/support'); return; }
    const t = data.ticket;
    if (store.state.me) store.state.me.support_unread = 0;
    head.replaceChildren(h('h2', { dir: 'auto', text: `#${t.number} — ${t.subject}` }),
      h('p', { class: 'muted' }, `${t.category_label} · `, statusChip(t)));
    thread.replaceChildren(...data.messages.map((m) => h('div', { class: `ticket-msg ticket-msg--${m.from}` },
      h('small', { text: m.from === 'me' ? 'أنت' : 'فريق الدعم' }),
      h('p', { dir: 'auto', text: m.body }),
      h('time', { datetime: m.created_at, text: formatListTime(m.created_at) }))));
    if (t.status === 'closed') {
      footer.replaceChildren(h('p', { class: 'muted', text: 'هذه التذكرة مغلقة. افتح تذكرة جديدة إذا احتجت.' }));
    } else {
      footer.replaceChildren(h('div', { class: 'ticket-composer', 'data-kb-block': '' }, input, send),
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: closeTicket }, 'إغلاق التذكرة (حُلّت المشكلة)'));
    }
  }
  send.addEventListener('click', async () => {
    send.disabled = true;
    try {
      await api.post(`/api/support/tickets/${ticketId}/messages`, { body: input.value.trim() });
      input.value = '';
      load();
    } catch (err) { toast(err.message, 'error'); send.disabled = false; }
  });
  async function closeTicket() {
    try { await api.post(`/api/support/tickets/${ticketId}/close`); load(); } catch (err) { toast(err.message, 'error'); }
  }
  page.replaceChildren(topbar('تذكرة دعم', () => navigate('#/support')), head, thread, footer);
  load();
  const onReply = () => load();
  document.addEventListener('dz:support', onReply);
  return () => document.removeEventListener('dz:support', onReply);
}
