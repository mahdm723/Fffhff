// Messages: chats (anonymous + direct), message requests, and the fixed composer to a random person.
import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, formatListTime, h, nameLine, personAvatar, toast } from '../ui.js';

const DRAFT_KEY = 'dz:draft';

function readDraft() { try { return localStorage.getItem(DRAFT_KEY) || ''; } catch { return ''; } }
function writeDraft(v) { try { v ? localStorage.setItem(DRAFT_KEY, v) : localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ } }

const SYSTEM_PREVIEW = { reveal: 'كُشفت الهوية' };
// last_message.preview already carries the call summary text for call events

function previewText(c) {
  const last = c.last_message;
  if (!last) return c.request && c.request.state === 'pending' && !c.request.incoming ? 'بانتظار قبول طلب المراسلة' : '';
  if (last.kind === 'system') return /مكالمة/.test(last.preview || '') ? last.preview : SYSTEM_PREVIEW.reveal;
  return last.mine ? `أنت: ${last.preview}` : last.preview;
}

function normalize(s) {
  return (s || '').toLocaleLowerCase('ar').replace(/[\u064B-\u0652\u0640]/g, '')
    .replace(/[أإآ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي');
}

export function renderMessages(page, { config, navigate }) {
  const list = h('ul', { class: 'conv-list' });
  const max = config.max_message_length;
  let tab = 'chats';
  let filter = '';

  const empty = h('div', { class: 'empty glass' });
  const tabs = h('div', { class: 'segmented segmented--tabs', role: 'tablist', 'aria-label': 'نوع المحادثات' });
  const searchIn = h('input', {
    class: 'input conv-search__input', type: 'search', placeholder: 'ابحث في محادثاتك', 'aria-label': 'ابحث في محادثاتك', maxlength: '40',
  });
  searchIn.addEventListener('input', () => { filter = normalize(searchIn.value.trim()); draw(); });
  const search = h('label', { class: 'conv-search' }, icon('search'), searchIn);

  function tabButton(id, label, count) {
    return h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(tab === id),
      onclick: () => { tab = id; draw(); },
    }, label, count ? h('span', { class: 'badge badge--inline', text: String(count) }) : null);
  }

  function row(c) {
    const anon = c.kind !== 'direct' && (!c.peer_card || c.peer_card.anonymous);
    const card = c.peer_card || {};
    const label = `${anon ? 'محادثة مجهولة' : `محادثة مع ${c.peer}`}${c.unread ? `، ${c.unread} رسائل جديدة` : ''}`;
    return h('li', {},
      h('button', { class: `conv-item ${c.unread ? 'is-unread' : ''} ${c.muted ? 'is-muted' : ''}`, onclick: () => navigate(`#/chat/${c.id}`), 'aria-label': label },
        personAvatar(c.peer, { anonymous: anon, active: !!card.active }),
        h('div', { class: 'conv-item__body' },
          h('div', { class: 'conv-item__top' },
            nameLine(c.peer, card.gender, 'conv-item__name', card.verified),
            c.kind !== 'direct' ? h('span', { class: 'conv-item__kind', title: 'محادثة مجهولة' }, icon('mask'), 'مجهول') : null,
            c.muted ? h('span', { class: 'conv-item__muted', title: 'مكتومة' }, icon('bellOff')) : null,
            h('span', { class: 'conv-item__time', text: formatListTime(c.last_message_at) }),
          ),
          h('div', { class: 'conv-item__bottom' },
            h('span', { class: 'conv-item__preview', text: previewText(c) }),
            c.status !== 'active' ? h('span', { class: 'conv-item__tag', text: 'مغلقة' }) : null,
            c.unread ? h('span', { class: 'unread-dot', 'aria-hidden': 'true' }) : null,
          ),
        )),
    );
  }

  function draw() {
    const all = store.state.conversations;
    const requests = all.filter(store.isRequest);
    tabs.replaceChildren(tabButton('chats', 'المحادثات', 0), tabButton('requests', 'طلبات الرسائل', requests.length));
    let convs = tab === 'requests' ? requests : all.filter((c) => !store.isRequest(c));
    if (filter) convs = convs.filter((c) => normalize(c.peer).includes(filter) || normalize(c.last_message && c.last_message.preview).includes(filter));
    search.hidden = !all.length;
    if (!convs.length) {
      empty.replaceChildren(...(tab === 'requests'
        ? [icon('inbox'), h('h2', { text: 'لا توجد طلبات رسائل' }), h('p', { text: 'عندما يراسلك شخص لأول مرة عبر معرّفك أو اسمك تظهر رسالته هنا، ولا يعرف أنك قرأتها حتى تقبل.' })]
        : filter
          ? [icon('search'), h('h2', { text: 'لا نتائج' }), h('p', { text: 'لا توجد محادثة تطابق بحثك.' })]
          : [icon('bubbles'), h('h2', { text: 'لا توجد محادثات بعد' }), h('p', { text: 'اكتب في الأسفل رسالة لشخص لا تعرفه، أو ابحث عن صديق من صفحة حسابي.' })]));
      if (list.isConnected) list.replaceWith(empty);
      return;
    }
    if (empty.isConnected) empty.replaceWith(list);
    list.replaceChildren(...convs.map(row));
  }

  // ------------------------------------------------------------ fixed anonymous composer
  const textarea = h('textarea', { rows: '1', 'aria-label': 'رسالة لشخص عشوائي', placeholder: 'اكتب رسالة لشخص عشوائي…', maxlength: String(max + 200) });
  textarea.value = readDraft();
  const send = h('button', { class: 'send-btn', type: 'button', 'aria-label': 'إرسال لشخص عشوائي' }, icon('send'));
  const counter = h('span', { class: 'counter' });
  const dock = h('section', { class: 'msg-dock glass glass--blur', 'aria-label': 'شارك رسالة مع شخص مجهول' },
    h('div', { class: 'msg-dock__head' },
      h('span', { class: 'msg-dock__title' }, icon('mask'), 'شارك رسالة مع شخص مجهول'),
      counter,
    ),
    h('div', { class: 'msg-dock__row' }, textarea, send),
  );
  const update = () => {
    const len = textarea.value.length;
    counter.textContent = len > max * 0.8 ? `${len} / ${max}` : '';
    counter.classList.toggle('is-over', len > max);
    send.disabled = !textarea.value.trim() || len > max;
    writeDraft(textarea.value);
  };
  textarea.addEventListener('input', update);
  // While typing, give the composer the space of the bottom navigation (mobile keyboard open).
  textarea.addEventListener('focus', () => document.body.classList.add('is-typing'));
  textarea.addEventListener('blur', () => document.body.classList.remove('is-typing'));
  autoGrow(textarea, 120);
  update();
  // Keep focus (and the keyboard) in the textarea when tapping send: otherwise the blur
  // brings the navigation back, the dock jumps up and the tap misses the button.
  send.addEventListener('pointerdown', (e) => e.preventDefault());

  send.addEventListener('click', async () => {
    const content = textarea.value.trim();
    if (!content) return;
    send.disabled = true;
    try {
      const res = await store.sendAnonymous(content);
      textarea.value = '';
      update();
      if (res.status === 'queued') toast('لا يوجد اتصال. ستُرسل رسالتك تلقائيًا عند عودة الإنترنت.');
      else {
        toast('وصلت رسالتك إلى شخص ما ✨');
        tab = 'chats';
        draw();
        const item = list.querySelector('.conv-item');
        if (item) item.classList.add('is-new');
      }
    } catch (err) {
      toast(err.message, 'error', 4500);
      update();
    }
  });

  page.classList.add('page--with-dock');
  page.replaceChildren(
    h('header', { class: 'topbar' }, h('h1', { class: 'page-title', text: 'الرسائل' })),
    search,
    tabs,
    list,
    dock,
  );
  draw();
  store.sync().catch(() => {});
  const unsub = store.subscribe((type) => { if (type === 'sync') draw(); });
  return () => {
    unsub();
    page.classList.remove('page--with-dock');
    document.body.classList.remove('is-typing');
  };
}
