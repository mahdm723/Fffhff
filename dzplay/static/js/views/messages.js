// Messages: anonymous conversations + a fixed composer to write to a random person.
import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, avatar, formatListTime, h, toast } from '../ui.js';

const DRAFT_KEY = 'dz:draft';

function readDraft() { try { return localStorage.getItem(DRAFT_KEY) || ''; } catch { return ''; } }
function writeDraft(v) { try { v ? localStorage.setItem(DRAFT_KEY, v) : localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ } }

export function renderMessages(page, { config, navigate }) {
  const list = h('ul', { class: 'conv-list' });
  const max = config.max_message_length;

  const empty = h('div', { class: 'empty glass' },
    icon('bubbles'),
    h('h2', { text: 'لا توجد محادثات بعد' }),
    h('p', { text: 'اكتب في الأسفل رسالة لشخص لا تعرفه، أو انتظر حتى يكتب لك أحدهم.' }),
  );

  function draw() {
    const convs = store.state.conversations;
    if (!convs.length) {
      if (list.isConnected) list.replaceWith(empty);
      return;
    }
    if (empty.isConnected) empty.replaceWith(list);
    list.replaceChildren(...convs.map((c) => {
      const last = c.last_message;
      const preview = last ? (last.mine ? `أنت: ${last.preview}` : last.preview) : '';
      return h('li', {},
        h('button', {
          class: `conv-item ${c.unread ? 'is-unread' : ''}`,
          onclick: () => navigate(`#/chat/${c.id}`),
          'aria-label': c.unread ? `محادثة مع dzplay، ${c.unread} رسائل جديدة` : 'محادثة مع dzplay',
        },
        avatar(),
        h('div', { class: 'conv-item__body' },
          h('div', { class: 'conv-item__top' },
            h('span', { class: 'conv-item__name', text: c.peer }),
            h('span', { class: 'conv-item__time', text: formatListTime(c.last_message_at) }),
          ),
          h('div', { class: 'conv-item__bottom' },
            h('span', { class: 'conv-item__preview', text: preview }),
            c.status !== 'active' ? h('span', { class: 'conv-item__tag', text: 'مغلقة' }) : null,
            c.unread ? h('span', { class: 'badge', text: String(c.unread) }) : null,
          ),
        )),
      );
    }));
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
