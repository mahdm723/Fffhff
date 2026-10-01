import { icon } from '../icons.js';
import * as store from '../store.js';
import { autoGrow, h, toast, wordmark } from '../ui.js';

const DRAFT_KEY = 'dz:draft';

function readDraft() { try { return localStorage.getItem(DRAFT_KEY) || ''; } catch { return ''; } }
function writeDraft(v) { try { v ? localStorage.setItem(DRAFT_KEY, v) : localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ } }

export function renderHome(page, { config, navigate }) {
  const max = config.max_message_length;

  function composer() {
    const textarea = h('textarea', {
      id: 'compose', 'aria-label': 'رسالتك', placeholder: 'اكتب ما تريد قوله لشخص لا تعرفه…', maxlength: String(max + 200),
    });
    textarea.value = readDraft();
    const counter = h('span', { class: 'counter', 'aria-live': 'off' });
    const send = h('button', { class: 'btn btn--primary', type: 'button' }, icon('send'), 'إرسال');

    const update = () => {
      const len = textarea.value.trim().length;
      counter.textContent = `${textarea.value.length} / ${max}`;
      counter.classList.toggle('is-over', textarea.value.length > max);
      send.disabled = len === 0 || textarea.value.length > max;
      writeDraft(textarea.value);
    };
    textarea.addEventListener('input', update);
    autoGrow(textarea, window.innerHeight * 0.55);
    update();

    send.addEventListener('click', async () => {
      const content = textarea.value.trim();
      if (!content) return;
      send.disabled = true;
      send.replaceChildren('جارٍ الإرسال…');
      try {
        const res = await store.sendAnonymous(content);
        writeDraft('');
        draw(res.status === 'queued' ? queuedCard() : sentCard(res.conversation));
      } catch (err) {
        toast(err.message, 'error', 4500);
        send.replaceChildren(icon('send'), 'إرسال');
        update();
      }
    });

    return h('div', {},
      h('section', { class: 'hero' },
        h('h1', {}, 'شارك مشاعرك ', h('span', { class: 'glow', text: 'مع شخص آخر' })),
        h('p', { text: 'ستصل رسالتك إلى شخص عشوائي لا يعرفك ولا تعرفه، ويمكنه الرد عليك.' }),
      ),
      h('div', { class: 'composer-card' },
        textarea,
        h('div', { class: 'composer-card__bar' }, counter, send),
      ),
      h('p', { class: 'privacy-note' }, icon('shield'), 'هويتك مخفية دائمًا. الطرف الآخر يرى فقط: dzplay'),
    );
  }

  function sentCard(conversation) {
    return h('div', { class: 'sent-card' },
      h('div', { class: 'sent-card__icon' }, icon('check')),
      h('h2', { text: 'وصلت رسالتك' }),
      h('p', { text: 'استلمها شخص ما الآن. ستجد ردّه في الرسائل.' }),
      h('div', { class: 'actions' },
        h('button', { class: 'btn btn--primary btn--block', onclick: () => navigate(`#/chat/${conversation.id}`) }, 'افتح المحادثة'),
        h('button', { class: 'btn btn--ghost btn--block', onclick: () => draw(composer()) }, 'اكتب رسالة أخرى'),
      ),
    );
  }

  function queuedCard() {
    return h('div', { class: 'sent-card' },
      h('div', { class: 'sent-card__icon' }, icon('clock')),
      h('h2', { text: 'رسالتك في الانتظار' }),
      h('p', { text: 'لا يوجد اتصال الآن. سنرسلها تلقائيًا عند عودة الإنترنت.' }),
      h('div', { class: 'actions' },
        h('button', { class: 'btn btn--ghost btn--block', onclick: () => draw(composer()) }, 'حسنًا'),
      ),
    );
  }

  const body = h('div');
  function draw(node) { body.replaceChildren(node); }

  page.replaceChildren(
    h('header', { class: 'topbar' }, wordmark()),
    body,
  );
  draw(composer());
}
