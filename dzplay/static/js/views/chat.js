// One conversation, Messenger-style: grouped bubbles, time on tap, day separators,
// sent / delivered / seen, typing indicator, system messages, message requests,
// and the conversation menu (reveal my identity, mute, report, block, delete).
import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import {
  REPORT_REASONS, autoGrow, confirmSheet, formatDay, formatTime, h, idChip, nameLine, personAvatar, sheet, toast,
} from '../ui.js';

const STATUS = {
  pending: ['clock', 'قيد الإرسال'],
  sent: ['check', 'أُرسلت'],
  delivered: ['checks', 'وصلت'],
  read: ['checks', 'شوهدت'],
};
const GROUP_MS = 5 * 60 * 1000;
const TYPING_SEND_EVERY = 3000;
const TYPING_IDLE = 4000;
const TYPING_SHOW_MAX = 6000;

function statusIcon(status) {
  const s = STATUS[status];
  if (!s) return null;
  return h('span', { class: `status status--${status}`, title: s[1] }, icon(s[0]), h('span', { class: 'sr-only', text: s[1] }));
}

function systemText(m) {
  const meta = m.meta || {};
  if (meta.event === 'call') {
    if (['missed', 'canceled'].includes(meta.outcome)) return m.mine ? 'لم يرد على مكالمتك' : m.content;
    if (meta.outcome === 'busy') return m.mine ? 'كان الطرف الآخر في مكالمة أخرى' : m.content;
    if (meta.outcome === 'declined') return m.mine ? 'رُفضت مكالمتك' : 'رفضت المكالمة';
    return m.content;
  }
  if (meta.event === 'reveal') {
    return m.mine
      ? 'كشفتَ هويتك. يرى هذا الشخص الآن اسمك ومعرّفك.'
      : `كشف الطرف الآخر هويته: ${meta.name || ''}${meta.public_id ? ` · ${meta.public_id}` : ''}`;
  }
  return m.content || '';
}

const isAnonymous = (conv) => !conv || (conv.kind !== 'direct' && (!conv.peer_card || conv.peer_card.anonymous));
const canCall = (conv) => !!conv && conv.status === 'active' && conv.peer_has_replied && !store.isRequest(conv)
  && !(conv.request && conv.request.state !== 'accepted');

export function renderChat(root, { conversationId, navigate }) {
  const body = h('div', { class: 'chat__body', role: 'log', 'aria-live': 'polite' });
  const footer = h('div', { class: 'chat__footer' });
  const expanded = new Set(); // messages whose time line was tapped open
  let lastRenderedCount = -1;
  let peerTyping = false;
  let typingTimer = null;

  // ---------------- header
  const headAvatar = h('div', { class: 'chat__avatar' });
  const headName = h('div', { class: 'chat__name' });
  const headSub = h('span', { class: 'chat__sub' });
  const callBtn = h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'مكالمة صوتية', onclick: () => startCall('audio') }, icon('phone'));
  const videoBtn = h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'مكالمة فيديو', onclick: () => startCall('video') }, icon('video'));
  const header = h('header', { class: 'chat__head' },
    h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/messages') }, icon('back')),
    h('button', { class: 'chat__who', type: 'button', 'aria-label': 'معلومات المحادثة', onclick: () => openMenu() },
      headAvatar, h('div', { class: 'chat__title' }, headName, headSub)),
    callBtn, videoBtn,
    h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'معلومات وخيارات', onclick: () => openMenu() }, icon('info')),
  );

  function drawHeader(conv) {
    const anon = isAnonymous(conv);
    const card = (conv && conv.peer_card) || {};
    headAvatar.replaceChildren(personAvatar(conv ? conv.peer : 'dzplay', { size: 'sm', anonymous: anon, active: !!card.active }));
    headName.replaceChildren(nameLine(conv ? conv.peer : 'dzplay', card.gender));
    let sub;
    if (!conv) sub = [];
    else if (!anon && card.active) sub = [h('span', { class: 'online-dot' }), 'نشط الآن'];
    else if (conv.kind === 'direct') sub = ['محادثة مباشرة'];
    else if (!anon) sub = [icon('eye'), 'كشف هويته لك'];
    else if (conv.me_revealed) sub = [icon('lock'), 'هويته مخفية · أنت كشفت هويتك'];
    else sub = [icon('lock'), 'هوية مخفية للطرفين'];
    headSub.replaceChildren(...sub);
    const allowed = canCall(conv);
    const enabled = !!(store.state.config && store.state.config.calls_enabled);
    for (const b of [callBtn, videoBtn]) {
      b.hidden = !enabled;
      b.disabled = !allowed;
      b.title = allowed ? '' : 'تتاح المكالمة بعد أن يرد الطرف الآخر';
    }
  }

  function startCall(kind) {
    const conv = store.getConversation(conversationId);
    if (!canCall(conv)) { toast('تتاح المكالمة بعد أن يرد الطرف الآخر على رسائلك.'); return; }
    if (window.dzCalls) window.dzCalls.start(conversationId, kind);
    else toast('المكالمات غير متاحة حاليًا.');
  }

  // ---------------- composer (+ typing signal)
  const textarea = h('textarea', { rows: '1', placeholder: 'اكتب رسالة…', 'aria-label': 'اكتب رسالة', dir: 'auto' });
  const sendBtn = h('button', { class: 'send-btn', type: 'button', 'aria-label': 'إرسال', disabled: true }, icon('send'));
  autoGrow(textarea);
  let typingSentAt = 0;
  let typingIdle = null;
  const stopTyping = () => {
    clearTimeout(typingIdle);
    if (typingSentAt) { store.sendTyping(conversationId, false); typingSentAt = 0; }
  };
  textarea.addEventListener('input', () => {
    sendBtn.disabled = !textarea.value.trim();
    if (!textarea.value.trim()) { stopTyping(); return; }
    const now = Date.now();
    if (now - typingSentAt > TYPING_SEND_EVERY) { store.sendTyping(conversationId, true); typingSentAt = now; }
    clearTimeout(typingIdle);
    typingIdle = setTimeout(stopTyping, TYPING_IDLE);
  });
  textarea.addEventListener('blur', stopTyping);
  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing && matchMedia('(pointer: fine)').matches) { e.preventDefault(); send(); }
  });
  sendBtn.addEventListener('pointerdown', (e) => e.preventDefault()); // keep the keyboard open
  sendBtn.addEventListener('click', send);
  const requestHint = h('p', { class: 'chat__hint', hidden: true });
  const composer = h('div', { class: 'chat__composer-wrap' }, requestHint, h('div', { class: 'chat__composer' }, textarea, sendBtn));

  function send() {
    const content = textarea.value.trim();
    const max = store.state.config.max_message_length;
    if (!content) return;
    if (content.length > max) { toast(`الرسالة أطول من ${max} حرف.`, 'error'); return; }
    stopTyping();
    store.sendReply(conversationId, content);
    textarea.value = '';
    textarea.dispatchEvent(new Event('input'));
    textarea.focus();
  }

  // ---------------- rendering
  const ts = (m) => Date.parse(m.created_at);
  const groupable = (a, b) => a && b && a.kind !== 'system' && b.kind !== 'system' && a.mine === b.mine
    && Math.abs(ts(a) - ts(b)) < GROUP_MS && formatDay(a.created_at) === formatDay(b.created_at);

  function bubble(m, prev, next, conv, lastMineId) {
    const first = !groupable(prev, m);
    const last = !groupable(m, next);
    const key = m.client_id || m.id;
    const row = h('div', {
      class: `bubble-row ${m.mine ? 'mine' : 'theirs'} ${first ? 'grp-first' : ''} ${last ? 'grp-last' : ''} ${m.status === 'failed' ? 'failed' : ''} ${expanded.has(key) ? 'show-meta' : ''}`,
    });
    const b = h('button', { class: 'bubble', type: 'button', 'aria-expanded': String(expanded.has(key)) }, h('span', { text: m.content, dir: 'auto' }));
    const detail = h('div', { class: 'bubble__detail' },
      h('time', { datetime: m.created_at, text: formatTime(m.created_at) }),
      m.mine && STATUS[m.status] ? h('span', { text: ` · ${STATUS[m.status][1]}` }) : null,
      !m.mine ? h('button', { class: 'link-btn', type: 'button', onclick: () => reportSheet(m.id) }, 'إبلاغ') : null,
    );
    b.addEventListener('click', () => {
      if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
      row.classList.toggle('show-meta');
      b.setAttribute('aria-expanded', String(expanded.has(key)));
    });
    const col = h('div', { class: 'bubble-col' }, b, detail);
    if (m.mine && m.id === lastMineId && m.status !== 'failed') {
      col.append(h('div', { class: `bubble__status bubble__status--${m.status}` }, statusIcon(m.status), h('span', { text: (STATUS[m.status] || [])[1] || '' })));
    }
    if (m.status === 'failed') {
      col.append(h('div', { class: 'bubble__failed' },
        h('span', { text: m.error || 'لم تُرسل' }),
        h('button', { type: 'button', onclick: () => store.retryMessage(conversationId, m.client_id) }, 'إعادة المحاولة'),
        h('button', { type: 'button', onclick: () => store.discardMessage(conversationId, m.client_id) }, 'حذف'),
      ));
    }
    if (!m.mine) {
      const anon = isAnonymous(conv);
      row.append(h('div', { class: 'bubble-face' }, last ? personAvatar(conv ? conv.peer : 'dzplay', { size: 'xs', anonymous: anon }) : null));
    }
    row.append(col);
    return row;
  }

  const typingRow = h('div', { class: 'bubble-row theirs grp-first grp-last typing-row', 'aria-label': 'يكتب الآن' },
    h('div', { class: 'bubble-face' }),
    h('div', { class: 'bubble-col' }, h('div', { class: 'bubble bubble--typing' }, h('i'), h('i'), h('i'))));

  function draw() {
    const conv = store.getConversation(conversationId);
    const msgs = store.getMessages(conversationId);
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
    drawHeader(conv);
    const direct = conv && conv.kind === 'direct';
    const nodes = [h('p', { class: 'notice' }, icon(direct ? 'info' : 'lock'), direct
      ? 'محادثة مباشرة: يرى كل طرف اسم الآخر ومعرّفه فقط، دون بريد أو رقم. تُحذف الرسائل من الخادم تلقائيًا بعد مدة.'
      : 'محادثة مجهولة: يظهر كل طرف باسم dzplay حتى يختار هو كشف هويته. تُحذف الرسائل من الخادم تلقائيًا بعد مدة.')];
    let day = null;
    const lastMine = [...msgs].reverse().find((m) => m.mine && m.kind !== 'system');
    msgs.forEach((m, i) => {
      const d = formatDay(m.created_at);
      if (d !== day) { nodes.push(h('div', { class: 'day-sep', text: d })); day = d; }
      if (m.kind === 'system') {
        const isCall = m.meta && m.meta.event === 'call';
        nodes.push(h('div', { class: `sys-msg ${isCall ? 'sys-msg--call' : ''}`, role: 'note' },
          h('span', {}, isCall ? icon(m.meta.kind === 'video' ? 'video' : 'phone') : null, systemText(m))));
      }
      else nodes.push(bubble(m, msgs[i - 1], msgs[i + 1], conv, lastMine && lastMine.id));
    });
    if (peerTyping) nodes.push(typingRow);
    body.replaceChildren(...nodes);
    if (nearBottom || lastRenderedCount !== msgs.length) body.scrollTop = body.scrollHeight;
    lastRenderedCount = msgs.length;
    drawFooter(conv);
    if (conv && conv.unread && !store.isRequest(conv) && document.visibilityState === 'visible') store.markRead(conversationId);
  }

  function drawFooter(conv) {
    if (conv && conv.status !== 'active') {
      footer.replaceChildren(h('div', { class: 'closed-bar', text: 'هذه المحادثة مغلقة ولم يعد بالإمكان الرد فيها.' }));
      return;
    }
    if (store.isRequest(conv)) {
      footer.replaceChildren(h('div', { class: 'request-bar' },
        h('p', { class: 'request-bar__title' }, nameLine(conv.peer, conv.peer_card && conv.peer_card.gender), ' يريد مراسلتك'),
        h('p', { class: 'request-bar__text', text: 'لن يعرف أنك قرأت رسائله حتى تقبل. إذا تجاهلت الطلب يختفي دون إشعاره.' }),
        h('div', { class: 'request-bar__actions' },
          h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => answer('accept') }, 'قبول'),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => answer('ignore') }, 'تجاهل'),
          h('button', { class: 'btn btn--danger btn--sm', type: 'button', onclick: () => blockFlow() }, 'حظر'),
        )));
      return;
    }
    const waiting = conv && conv.request && conv.request.state !== 'accepted' && !conv.request.incoming;
    requestHint.hidden = !waiting;
    if (waiting) {
      const n = store.state.config.direct_before_reply;
      requestHint.textContent = `أُرسل طلب مراسلة. ${n ? `يمكنك إرسال ${n} رسائل على الأكثر` : 'يمكنك إرسال رسائل قليلة'} حتى يرد.`;
    }
    if (!composer.isConnected) footer.replaceChildren(composer);
  }

  async function answer(action) {
    try {
      await store.answerRequest(conversationId, action);
      if (action === 'ignore') { toast('تم تجاهل الطلب.'); navigate('#/messages'); }
      else toast('قبلت الطلب. يمكنكما الآن التحدث.');
    } catch (err) { toast(err.message, 'error'); }
  }

  // ---------------- conversation menu (info + actions)
  function openMenu() {
    const conv = store.getConversation(conversationId);
    if (!conv) return;
    const anon = isAnonymous(conv);
    const card = conv.peer_card || {};
    const active = conv.status === 'active';
    sheet((panel, close) => {
      const act = (ic, label, fn, cls = 'btn--ghost') => h('button', { class: `btn ${cls} btn--block`, type: 'button', onclick: () => { close(); fn(); } }, icon(ic), label);
      panel.append(
        h('div', { class: 'peer-info' },
          personAvatar(conv.peer, { size: 'lg', anonymous: anon, active: !!card.active }),
          h('h2', {}, nameLine(conv.peer, card.gender)),
          card.public_id ? idChip(card.public_id) : h('p', { text: 'هوية هذا الشخص مخفية. يظهر باسم dzplay.' }),
        ),
        h('div', { class: 'actions' },
          card.public_id ? act('user', 'عرض الملف', () => navigate(`#/id/${card.public_id}`)) : null,
          conv.kind !== 'direct' && active && !conv.me_revealed ? act('eye', 'كشف هويتي', revealFlow) : null,
          act(conv.muted ? 'bell' : 'bellOff', conv.muted ? 'إلغاء كتم الإشعارات' : 'كتم الإشعارات', () => setMuted(!conv.muted)),
          act('flag', 'إبلاغ', () => reportSheet(null)),
          active ? act('block', 'حظر', blockFlow, 'btn--danger') : null,
          act('trash', 'حذف المحادثة', deleteFlow),
        ),
      );
    });
  }

  async function revealFlow() {
    const ok = await confirmSheet({
      title: 'كشف هويتك؟',
      text: 'سيرى هذا الشخص اسمك ومعرّفك DZ فقط (لا بريد ولا رقم). لا يمكن التراجع عن ذلك، ولن تُكشف هويته هو.',
      confirm: 'كشف هويتي',
    });
    if (!ok) return;
    try {
      await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/reveal`);
      await store.loadConversation(conversationId);
    } catch (err) { toast(err.message, 'error'); }
  }

  async function setMuted(muted) {
    try {
      await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/mute`, { muted });
      store.patchConversation(conversationId, { muted });
      toast(muted ? 'لن تصلك إشعارات من هذه المحادثة.' : 'أُعيدت الإشعارات.');
    } catch (err) { toast(err.message, 'error'); }
  }

  async function blockFlow() {
    const ok = await confirmSheet({
      title: 'حظر هذا الشخص؟',
      text: 'لن يتمكن من مراسلتك أو الاتصال بك أو العثور عليك بالبحث، ولن يُختار لك في الرسائل العشوائية. ستُغلق هذه المحادثة.',
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
      const choices = REPORT_REASONS.map(([value, label], i) =>
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

  // ---------------- mount
  const chatEl = h('div', { class: 'chat' }, header, body, footer);
  root.replaceChildren(chatEl);
  footer.replaceChildren(composer);
  draw();

  store.loadConversation(conversationId).catch((err) => {
    if (err.status === 404) {
      store.removeConversationLocally(conversationId);
      toast('هذه المحادثة لم تعد متاحة.', 'error');
      navigate('#/messages');
    }
  });

  const unsub = store.subscribe((type, detail) => {
    if (type === 'sync') draw();
    else if (type === 'typing' && detail.conversation_id === conversationId) {
      clearTimeout(typingTimer);
      peerTyping = !!detail.on;
      if (peerTyping) typingTimer = setTimeout(() => { peerTyping = false; draw(); }, TYPING_SHOW_MAX);
      draw();
    }
  });
  // The peer's new message ends their typing indicator.
  const unsubMsg = store.subscribe((type) => { if (type === 'incoming' && peerTyping) { peerTyping = false; draw(); } });
  const onVis = () => { if (document.visibilityState === 'visible') draw(); };
  document.addEventListener('visibilitychange', onVis);

  // Keyboard: keep the composer above the on-screen keyboard (iOS has no interactive-widget).
  const vv = window.visualViewport;
  const fit = () => {
    if (!vv) return;
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 160;
    chatEl.style.height = `${Math.round(vv.height)}px`;
    if (nearBottom) body.scrollTop = body.scrollHeight;
  };
  if (vv) { vv.addEventListener('resize', fit); fit(); }

  return () => {
    stopTyping();
    clearTimeout(typingTimer);
    unsub();
    unsubMsg();
    document.removeEventListener('visibilitychange', onVis);
    if (vv) vv.removeEventListener('resize', fit);
  };
}
