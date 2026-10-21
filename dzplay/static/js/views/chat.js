// One conversation, Messenger-style: grouped bubbles, time on tap, day separators,
// sent / delivered / seen, typing indicator, system messages, message requests,
// and the conversation menu (mute, report, block, delete). V6: old anonymous chats are read-only.
import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import {
  REPORT_REASONS, autoGrow, confirmSheet, formatDay, formatTime, h, idChip, nameLine, personAvatar, sheet, toast,
} from '../ui.js';
import { t } from '../i18n.js';

const STATUS = () => ({  // translated when shown
  pending: ['clock', t('قيد الإرسال')],
  sent: ['check', t('أُرسلت')],
  delivered: ['checks', t('وصلت')],
  read: ['checks', t('شوهدت')],
});
const GROUP_MS = 5 * 60 * 1000;
const TYPING_SEND_EVERY = 3000;
const TYPING_IDLE = 4000;
const TYPING_SHOW_MAX = 6000;

function statusIcon(status) {
  const s = STATUS()[status];
  if (!s) return null;
  return h('span', { class: `status status--${status}`, title: s[1] }, icon(s[0]), h('span', { class: 'sr-only', text: s[1] }));
}

function systemText(m) {
  const meta = m.meta || {};
  if (meta.event === 'reveal') return m.mine ? t('كشفتَ هويتك.') : t('كشف الطرف الآخر هويته.');
  return m.content || '';
}

const isAnonymous = (conv) => !conv || (conv.kind !== 'direct' && (!conv.peer_card || conv.peer_card.anonymous));

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
  const header = h('header', { class: 'chat__head' },
    h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': t('رجوع'), onclick: () => navigate('#/messages') }, icon('back')),
    h('button', { class: 'chat__who', type: 'button', 'aria-label': t('معلومات المحادثة'), onclick: () => openMenu() },
      headAvatar, h('div', { class: 'chat__title' }, headName, headSub)),
    h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': t('معلومات وخيارات'), onclick: () => openMenu() }, icon('info')),
  );

  function drawHeader(conv) {
    const anon = isAnonymous(conv);
    const card = (conv && conv.peer_card) || {};
    headAvatar.replaceChildren(personAvatar(conv ? conv.peer : 'dzplay', { size: 'sm', anonymous: anon, active: !!card.active, url: card.avatar_url }));
    headName.replaceChildren(nameLine(conv ? conv.peer : 'dzplay', card.gender, '', card.verified));
    let sub;
    if (!conv) sub = [];
    else if (!anon && card.active) sub = [h('span', { class: 'online-dot' }), t('نشط الآن')];
    else if (conv.kind === 'direct') sub = [t('محادثة مباشرة')];
    else sub = [icon('lock'), t('محادثة مجهولة قديمة · للقراءة فقط')];
    headSub.replaceChildren(...sub);
  }

  // ---------------- composer (+ typing signal)
  const textarea = h('textarea', { rows: '1', placeholder: t('اكتب رسالة…'), 'aria-label': t('اكتب رسالة'), dir: 'auto' });
  const sendBtn = h('button', { class: 'send-btn', type: 'button', 'aria-label': t('إرسال'), disabled: true }, icon('send'));
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
  // V6 phase 17: pictures in chats are switched off for good (the server refuses them before reading a byte)
  const composer = h('div', { class: 'chat__composer-wrap' }, requestHint,
    h('div', { class: 'chat__composer' }, textarea, sendBtn));

  const imageContent = () => ({ // an old picture: deleted everywhere (only a reported one is kept, for the moderators)
    node: h('span', { class: 'img-msg img-msg--gone' }, icon('image'), h('span', { text: t('صورة محذوفة') })), action: null,
  });

  function send() {
    const content = textarea.value.trim();
    const max = store.state.config.max_message_length;
    if (!content) return;
    if (content.length > max) { toast(t('الرسالة أطول من {n} حرف.', { n: max }), 'error'); return; }
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
    const img = m.kind === 'image' ? imageContent(m) : null;
    const b = h('button', { class: `bubble ${img ? 'bubble--image' : ''}`, type: 'button', 'aria-expanded': String(expanded.has(key)) },
      img ? img.node : h('span', { text: m.content, dir: 'auto' }));
    const detail = h('div', { class: 'bubble__detail' },
      h('time', { datetime: m.created_at, text: formatTime(m.created_at) }),
      m.mine && STATUS()[m.status] ? h('span', { text: ` · ${STATUS()[m.status][1]}` }) : null,
      !m.mine ? h('button', { class: 'link-btn', type: 'button', onclick: () => reportSheet(m.id) }, t('إبلاغ')) : null,
    );
    b.addEventListener('click', () => {
      if (img && img.action) { img.action(); return; }
      if (expanded.has(key)) expanded.delete(key); else expanded.add(key);
      row.classList.toggle('show-meta');
      b.setAttribute('aria-expanded', String(expanded.has(key)));
    });
    const col = h('div', { class: 'bubble-col' }, b, detail);
    if (m.mine && m.id === lastMineId && m.status !== 'failed') {
      col.append(h('div', { class: `bubble__status bubble__status--${m.status}` }, statusIcon(m.status), h('span', { text: (STATUS()[m.status] || [])[1] || '' })));
    }
    if (m.status === 'failed') {
      col.append(h('div', { class: 'bubble__failed' },
        h('span', { text: m.error || t('لم تُرسل') }),
        h('button', { type: 'button', onclick: () => store.retryMessage(conversationId, m.client_id) }, t('إعادة المحاولة')),
        h('button', { type: 'button', onclick: () => store.discardMessage(conversationId, m.client_id) }, t('حذف')),
      ));
    }
    if (!m.mine) {
      const anon = isAnonymous(conv);
      row.append(h('div', { class: 'bubble-face' }, last ? personAvatar(conv ? conv.peer : 'dzplay', { size: 'xs', anonymous: anon, url: conv && conv.peer_card ? conv.peer_card.avatar_url : null }) : null));
    }
    row.append(col);
    return row;
  }

  const typingRow = h('div', { class: 'bubble-row theirs grp-first grp-last typing-row', 'aria-label': t('يكتب الآن') },
    h('div', { class: 'bubble-face' }),
    h('div', { class: 'bubble-col' }, h('div', { class: 'bubble bubble--typing' }, h('i'), h('i'), h('i'))));

  function draw() {
    const conv = store.getConversation(conversationId);
    const msgs = store.getMessages(conversationId);
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 120;
    drawHeader(conv);
    const direct = conv && conv.kind === 'direct';
    const nodes = [h('p', { class: 'notice' }, icon(direct ? 'info' : 'lock'), direct
      ? t('محادثة مباشرة: يرى كل طرف اسم الآخر ومعرّفه فقط، دون بريد أو رقم. تُحذف الرسائل من الخادم تلقائيًا بعد مدة.')
      : t('أُوقفت الرسائل المجهولة العشوائية. هذه محادثة قديمة للقراءة فقط وستُحذف قريبًا.'))];
    let day = null;
    const lastMine = [...msgs].reverse().find((m) => m.mine && m.kind !== 'system');
    msgs.forEach((m, i) => {
      const d = formatDay(m.created_at);
      if (d !== day) { nodes.push(h('div', { class: 'day-sep', text: d })); day = d; }
      if (m.kind === 'system') {
        nodes.push(h('div', { class: 'sys-msg', role: 'note' }, h('span', {}, systemText(m))));
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
    if (conv && conv.read_only) {
      const when = conv.deleted_after ? formatDay(conv.deleted_after) : '';
      footer.replaceChildren(h('div', { class: 'closed-bar', text: when ? t('محادثة مجهولة قديمة للقراءة فقط، تُحذف في {when}.', { when }) : t('محادثة مجهولة قديمة للقراءة فقط.') }));
      return;
    }
    if (conv && conv.status !== 'active') {
      footer.replaceChildren(h('div', { class: 'closed-bar', text: t('هذه المحادثة مغلقة ولم يعد بالإمكان الرد فيها.') }));
      return;
    }
    if (store.isRequest(conv)) {
      footer.replaceChildren(h('div', { class: 'request-bar' },
        h('p', { class: 'request-bar__title' }, nameLine(conv.peer, conv.peer_card && conv.peer_card.gender, '', !!(conv.peer_card && conv.peer_card.verified)), t(' يريد مراسلتك')),
        h('p', { class: 'request-bar__text', text: t('لن يعرف أنك قرأت رسائله حتى تقبل. إذا تجاهلت الطلب يختفي دون إشعاره.') }),
        h('div', { class: 'request-bar__actions' },
          h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => answer('accept') }, t('قبول')),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => answer('ignore') }, t('تجاهل')),
          h('button', { class: 'btn btn--danger btn--sm', type: 'button', onclick: () => blockFlow() }, t('حظر')),
        )));
      return;
    }
    const waiting = conv && conv.request && conv.request.state !== 'accepted' && !conv.request.incoming;
    requestHint.hidden = !waiting;
    if (waiting) {
      const n = store.state.config.direct_before_reply;
      requestHint.textContent = n ? t('أُرسل طلب مراسلة. يمكنك إرسال {n} رسائل على الأكثر حتى يرد.', { n }) : t('أُرسل طلب مراسلة. يمكنك إرسال رسائل قليلة حتى يرد.');
    }
    if (!composer.isConnected) footer.replaceChildren(composer);
  }

  async function answer(action) {
    try {
      await store.answerRequest(conversationId, action);
      if (action === 'ignore') { toast(t('تم تجاهل الطلب.')); navigate('#/messages'); }
      else toast(t('قبلت الطلب. يمكنكما الآن التحدث.'));
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
          personAvatar(conv.peer, { size: 'lg', anonymous: anon, active: !!card.active, url: card.avatar_url }),
          h('h2', {}, nameLine(conv.peer, card.gender, '', !!card.verified)),
          card.public_id ? idChip(card.public_id) : h('p', { text: t('هوية هذا الشخص مخفية. يظهر باسم dzplay.') }),
        ),
        h('div', { class: 'actions' },
          card.public_id ? act('user', t('عرض الملف'), () => navigate(`#/id/${card.public_id}`)) : null,
          act(conv.muted ? 'bell' : 'bellOff', conv.muted ? t('إلغاء كتم الإشعارات') : t('كتم الإشعارات'), () => setMuted(!conv.muted)),
          act('flag', t('إبلاغ'), () => reportSheet(null)),
          active ? act('block', t('حظر'), blockFlow, 'btn--danger') : null,
          act('trash', t('حذف المحادثة'), deleteFlow),
        ),
      );
    });
  }

  async function setMuted(muted) {
    try {
      await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/mute`, { muted });
      store.patchConversation(conversationId, { muted });
      toast(muted ? t('لن تصلك إشعارات من هذه المحادثة.') : t('أُعيدت الإشعارات.'));
    } catch (err) { toast(err.message, 'error'); }
  }

  async function blockFlow() {
    const ok = await confirmSheet({
      title: t('حظر هذا الشخص؟'),
      text: t('لن يتمكن من مراسلتك أو العثور عليك بالبحث. ستُغلق هذه المحادثة.'),
      confirm: t('حظر'), danger: true,
    });
    if (!ok) return;
    try {
      await api.post(`/api/conversations/${encodeURIComponent(conversationId)}/block`);
      store.removeConversationLocally(conversationId);
      toast(t('تم الحظر.'));
      navigate('#/messages');
    } catch (err) { toast(err.message, 'error'); }
  }

  async function deleteFlow() {
    const ok = await confirmSheet({
      title: t('حذف المحادثة؟'),
      text: t('ستختفي المحادثة من قائمتك. إذا كتب لك الطرف الآخر مجددًا ستعود للظهور.'),
      confirm: t('حذف'), danger: true,
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
      const details = h('textarea', { class: 'input', rows: '3', maxlength: '500', placeholder: t('تفاصيل إضافية (اختياري)') });
      const alsoBlock = h('input', { type: 'checkbox', checked: true });
      const choices = REPORT_REASONS().map(([value, label], i) =>
        h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'reason', value, checked: i === 0 }), h('span', { text: label })));
      const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, t('إرسال البلاغ'));
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
            toast(t('شكرًا لك. تم إرسال البلاغ وحظر هذا الشخص.'));
            navigate('#/messages');
            return;
          }
          close();
          toast(t('شكرًا لك. تم إرسال البلاغ وسيُراجع.'));
        } catch (err) {
          toast(err.message, 'error');
          submit.disabled = false;
        }
      });
      panel.append(
        h('h2', { text: messageId ? t('الإبلاغ عن رسالة') : t('الإبلاغ عن محادثة') }),
        h('p', { text: t('البلاغات سرية. نحتفظ فقط بنص الرسالة المُبلّغ عنها لمراجعتها.') }),
        ...choices,
        h('div', { class: 'field' }, details),
        h('label', { class: 'choice' }, alsoBlock, h('span', { text: t('احظر هذا الشخص أيضًا') })),
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
      toast(t('هذه المحادثة لم تعد متاحة.'), 'error');
      navigate('#/messages');
    }
  });

  const unsub = store.subscribe((type, detail) => {
    if (type === 'sync') draw();
    else if (type === 'media') store.loadConversation(conversationId).catch(() => {});
    else if (type === 'typing' && detail.conversation_id === conversationId) {
      clearTimeout(typingTimer);
      peerTyping = !!detail.on;
      if (peerTyping) typingTimer = setTimeout(() => { peerTyping = false; draw(); }, TYPING_SHOW_MAX);
      draw();
    }
  });
  // The peer's new message ends their typing indicator.
  const unsubMsg = store.subscribe((type) => { if (type === 'incoming' && peerTyping) { peerTyping = false; draw(); } });
  // countdowns on opened pictures (both sides); at 0 the bubble turns into "expired"
  const countdown = setInterval(() => {
    let ended = false;
    for (const el of body.querySelectorAll('[data-expires]')) {
      const left = Math.max(0, Math.ceil((Date.parse(el.dataset.expires) - Date.now()) / 1000));
      el.textContent = t('{n} ث', { n: left });
      if (left <= 0) ended = true;
    }
    if (ended) draw();
  }, 1000);
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
    clearInterval(countdown);
    clearTimeout(typingTimer);
    unsub();
    unsubMsg();
    document.removeEventListener('visibilitychange', onVis);
    if (vv) vv.removeEventListener('resize', fit);
  };
}
