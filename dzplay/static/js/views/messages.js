// Messages: direct chats and message requests (V6: random anonymous messages were removed; old anonymous
// chats stay listed, read-only, until they are deleted).
import { icon } from '../icons.js';
import * as store from '../store.js';
import { formatListTime, h, nameLine, personAvatar } from '../ui.js';

// the old random-message draft is not used any more
try { localStorage.removeItem('dz:draft'); } catch { /* ignore */ }

const SYSTEM_PREVIEW = { reveal: 'كُشفت الهوية' };

function previewText(c) {
  const last = c.last_message;
  if (!last) return c.request && c.request.state === 'pending' && !c.request.incoming ? 'بانتظار قبول طلب المراسلة' : '';
  if (last.kind === 'system') return SYSTEM_PREVIEW.reveal;
  return last.mine ? `أنت: ${last.preview}` : last.preview;
}

function normalize(s) {
  return (s || '').toLocaleLowerCase('ar').replace(/[\u064B-\u0652\u0640]/g, '')
    .replace(/[أإآ]/g, 'ا').replace(/ة/g, 'ه').replace(/ى/g, 'ي');
}

export function renderMessages(page, { navigate }) {
  const list = h('ul', { class: 'conv-list' });
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
    const label = `${anon ? 'محادثة مجهولة قديمة' : `محادثة مع ${c.peer}`}${c.unread ? `، ${c.unread} رسائل جديدة` : ''}`;
    return h('li', {},
      h('button', { class: `conv-item ${c.unread ? 'is-unread' : ''} ${c.muted ? 'is-muted' : ''}`, onclick: () => navigate(`#/chat/${c.id}`), 'aria-label': label },
        personAvatar(c.peer, { anonymous: anon, active: !!card.active, url: card.avatar_url }),
        h('div', { class: 'conv-item__body' },
          h('div', { class: 'conv-item__top' },
            nameLine(c.peer, card.gender, 'conv-item__name', card.verified),
            c.kind !== 'direct' ? h('span', { class: 'conv-item__kind', title: 'محادثة مجهولة قديمة للقراءة فقط' }, icon('mask'), 'مجهول قديم') : null,
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
          : [icon('bubbles'), h('h2', { text: 'لا توجد محادثات بعد' }),
            h('p', { text: 'ابحث عن شخص بمعرّفه DZ أو باسمه من «المستخدمون»، ثم اضغط «مراسلة».' }),
            h('button', { type: 'button', class: 'btn btn--primary', onclick: () => navigate('#/users') }, icon('search'), 'ابحث عن شخص')]));
      if (list.isConnected) list.replaceWith(empty);
      return;
    }
    if (empty.isConnected) empty.replaceWith(list);
    list.replaceChildren(...convs.map(row));
  }

  page.replaceChildren(
    h('header', { class: 'topbar' }, h('h1', { class: 'page-title', text: 'الرسائل' }),
      h('button', { type: 'button', class: 'icon-btn glass', 'aria-label': 'ابحث عن شخص لمراسلته', onclick: () => navigate('#/users') }, icon('search'))),
    search,
    tabs,
    list,
  );
  draw();
  store.sync().catch(() => {});
  const unsub = store.subscribe((type) => { if (type === 'sync') draw(); });
  return () => unsub();
}
