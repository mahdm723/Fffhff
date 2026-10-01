import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, avatar, confirmSheet, formatDay, formatTime, h, sheet, toast } from '../ui.js';

const REASONS = [
  ['spam', 'رسائل مزعجة (سبام)'],
  ['harassment', 'تحرش أو مضايقة'],
  ['threat', 'تهديد'],
  ['inappropriate', 'محتوى غير لائق'],
  ['other', 'سبب آخر'],
];

const STATUS = {
  pending: ['clock', 'قيد الإرسال'],
  sent: ['check', 'أُرسلت'],
  delivered: ['checks', 'وصلت'],
  read: ['checks', 'قُرئت'],
};

function statusIcon(status) {
  const s = STATUS[status];
  if (!s) return null;
  return h('span', { class: `status status--${status}`, title: s[1] }, icon(s[0]), h('span', { class: 'sr-only', text: s[1] }));
}

export function renderChat(root, { conversationId, navigate }) {
  const body = h('div', { class: 'chat__body', role: 'log', 'aria-live': 'polite' });
  const footer = h('div');
  let lastRenderedCount = -1;

  const header = h('header', { class: 'chat__head' },
    h('button', { class: 'icon-btn icon-btn--plain', 'aria-label': 'رجوع', onclick: () => navigate('#/messages') }, icon('back')),
    avatar('sm'),
    h('div', { class: 'chat__title' },
      h('span', { class: 'chat__name', text: 'dzplay' }),
      h('span', { class: 'chat__sub' }, icon('lock'), 'هوية مخفية للطرفين'),
    ),
    h('button', { class: 'icon-btn icon-btn--plain', 'aria-label': 'خيارات', onclick: () => openMenu() }, icon('more')),
  );

  // ---------------- composer
  const textarea = h('textarea', { rows: '1', placeholder: 'اكتب ردّك…', 'aria-label': 'اكتب ردّك' });
  const sendBtn = h('button', { class: 'send-btn', type: 'button', 'aria-label': 'إرسال', disabled: true }, icon('send'));
  autoGrow(textarea);
  textarea.addEventListener('input', () => { sendBtn.disabled = !textarea.value.trim(); });
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && matchMedia('(pointer: fine)').matches) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('click', send);
  const composer = h('div', { class: 'chat__composer' }, textarea, sendBtn);

  function send() {
    const content = textarea.value.trim();
    const max = store.state.config.max_message_length;
    if (!content) return;
    if (content.length > max) { toast(`الرسالة أطول من ${max} حرف.`, 'error'); return; }
    store.sendReply(conversationId, content);
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    textarea.focus();
  }

  // ---------------- rendering
  function bubble(m, prev) {
    const row = h('div', { class: `bubble-row ${m.mine ? 'mine' : 'theirs'} ${m.status === 'failed' ? 'failed' : ''} ${prev && prev.mine === m.mine ? 'same' : ''}` });
    const meta = h('span', { class: 'bubble__meta' }, h('span', { text: formatTime(m.created_at) }), m.mine ? statusIcon(m.status) : null);
    const b = h(m.mine ? 'div' : 'button', { class: 'bubble', type: m.mine ? null : 'button' }, h('span', { text: m.content }), meta);
    if (!m.mine) b.addEventListener('click', () => messageActions(m));
    const wrap = h('div', {}, b);
    if (m.status === 'failed') {
      wrap.append(h('div', { class: 'bubble__failed' },
        h('span', { text: m.error || 'لم تُرسل' }),
        h('button', { type: 'button', onclick: () => store.retryMessage(conversationId, m.client_id) }, 'إعادة المحاولة'),
        h('button', { type: 'button', onclick: () => store.discardMessage(conversationId, m.client_id) }, 'حذف'),
      ));
    }
    row.append(wrap);
    return row;
  }

  function draw() {
    const conv = store.getConversation(conversationId);
    const msgs = store.getMessages(conversationId);
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
    const nodes = [h('p', { class: 'notice' }, icon('info'),
      'هذه محادثة مجهولة. لا يعرف أي طرف هوية الآخر. تُحذف الرسائل من الخادم تلقائيًا بعد مدة.')];
    let day = null;
    let prev = null;
    for (const m of msgs) {
      const d = formatDay(m.created_at);
      if (d !== day) { nodes.push(h('div', { class: 'day-sep', text: d })); day = d; prev = null; }
      nodes.push(bubble(m, prev));
      prev = m;
    }
    body.replaceChildren(...nodes);
    if (nearBottom || lastRenderedCount !== msgs.length) body.scrollTop = body.scrollHeight;
    lastRenderedCount = msgs.length;

    if (conv && conv.status !== 'active') {
      footer.replaceChildren(h('div', { class: 'closed-bar', text: 'هذه المحادثة مغلقة ولم يعد بالإمكان الرد فيها.' }));
    } else if (!composer.isConnected) {
      footer.replaceChildren(composer);
    }
    if (conv && conv.unread && document.visibilityState === 'visible') store.markRead(conversationId);
  }

  // ---------------- actions
  function messageActions(m) {
    sheet((panel, close) => {
      panel.append(
        h('h2', { text: 'رسالة من dzplay' }),
        h('div', { class: 'actions' },
          h('button', { class: 'btn btn--ghost btn--block', onclick: () => { close(); reportSheet(m.id); } }, icon('flag'), 'الإبلاغ عن هذه الرسالة'),
          h('button', { class: 'btn btn--ghost btn--block', onclick: close }, 'إلغاء'),
        ),
      );
    });
  }

  function openMenu() {
    const conv = store.getConversation(conversationId);
    sheet((panel, close) => {
      panel.append(
        h('h2', { text: 'خيارات المحادثة' }),
        h('div', { class: 'actions' },
          h('button', { class: 'btn btn--ghost btn--block', onclick: () => { close(); reportSheet(null); } }, icon('flag'), 'الإبلاغ عن المحادثة'),
          conv && conv.status === 'active'
            ? h('button', { class: 'btn btn--danger btn--block', onclick: () => { close(); blockFlow(); } }, icon('block'), 'حظر هذا الشخص')
            : null,
          h('button', { class: 'btn btn--ghost btn--block', onclick: () => { close(); deleteFlow(); } }, icon('trash'), 'حذف المحادثة'),
        ),
      );
    });
  }

  async function blockFlow() {
    const ok = await confirmSheet({
      title: 'حظر هذا الشخص؟',
      text: 'لن يتمكن من مراسلتك مجددًا، ولن يُختار لك أو تُختار له في الرسائل العشوائية. ستُغلق هذه المحادثة.',
      confirm: 'حظر', danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/block`);
      store.removeConversationLocally(conversationId);
      toast('تم الحظر.');
      navigate('#/messages');
    } catch (err) { toast(err.message, 'error'); }
  }

  async function deleteFlow() {
    const ok = await confirmSheet({
      title: 'حذف المحادثة؟',
      text: 'ستختفي المحادثة من قائمتك. إذا كتب لك الطرف الآخر مجددًا ستعود للظهور.',
      confirm: 'حذف', danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/conversations/${encodeURIComponent(conversationId)}`);
      store.removeConversationLocally(conversationId);
      navigate('#/messages');
    } catch (err) { toast(err.message, 'error'); }
  }

  function reportSheet(messageId) {
    sheet((panel, close) => {
      const details = h('textarea', { class: 'input', rows: '3', maxlength: '500', placeholder: 'تفاصيل إضافية (اختياري)' });
      const alsoBlock = h('input', { type: 'checkbox', checked: true });
      const choices = REASONS.map(([value, label], i) =>
        h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'reason', value, checked: i === 0 }), h('span', { text: label })));
      const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال البلاغ');
      submit.addEventListener('click', async () => {
        const reason = panel.querySelector('input[name="reason"]:checked')?.value;
        submit.disabled = true;
        try {
          const path = messageId
            ? `/api/messages/${encodeURIComponent(messageId)}/report`
            : `/api/conversations/${encodeURIComponent(conversationId)}/report`;
          await api.post(path, { reason, details: details.value.trim() || null });
          const conv = store.getConversation(conversationId);
          if (alsoBlock.checked && conv && conv.status === 'active') {
            await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/block`);
            store.removeConversationLocally(conversationId);
            close();
            toast('شكرًا لك. تم إرسال البلاغ وحظر هذا الشخص.');
            navigate('#/messages');
            return;
          }
          close();
          toast('شكرًا لك. تم إرسال البلاغ وسيُراجع.');
        } catch (err) {
          toast(err.message, 'error');
          submit.disabled = false;
        }
      });
      panel.append(
        h('h2', { text: messageId ? 'الإبلاغ عن رسالة' : 'الإبلاغ عن محادثة' }),
        h('p', { text: 'البلاغات سرية. نحتفظ فقط بنص الرسالة المُبلّغ عنها لمراجعتها.' }),
        ...choices,
        h('div', { class: 'field' }, details),
        h('label', { class: 'choice' }, alsoBlock, h('span', { text: 'احظر هذا الشخص أيضًا' })),
        h('div', { class: 'actions' }, submit),
      );
    });
  }

  root.replaceChildren(h('div', { class: 'chat' }, header, body, footer));
  footer.replaceChildren(composer);
  draw();

  store.loadConversation(conversationId).catch((err) => {
    if (err.status === 404) {
      store.removeConversationLocally(conversationId);
      toast('هذه المحادثة لم تعد متاحة.', 'error');
      navigate('#/messages');
    }
  });

  const unsub = store.subscribe((type) => { if (type === 'sync') draw(); });
  const onVis = () => { if (document.visibilityState === 'visible') draw(); };
  document.addEventListener('visibilitychange', onVis);
  return () => { unsub(); document.removeEventListener('visibilitychange', onVis); };
}
