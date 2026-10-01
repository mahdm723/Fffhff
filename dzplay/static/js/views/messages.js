import { icon } from '../icons.js';
import * as store from '../store.js';
import { avatar, formatListTime, h } from '../ui.js';

export function renderMessages(page, { navigate }) {
  const list = h('ul', { class: 'conv-list' });

  function draw() {
    const convs = store.state.conversations;
    if (!convs.length) {
      list.replaceWith(empty);
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

  const empty = h('div', { class: 'empty' },
    icon('bubbles'),
    h('h2', { text: 'لا توجد محادثات بعد' }),
    h('p', { text: 'اكتب رسالة لشخص لا تعرفه، أو انتظر حتى يكتب لك أحدهم.' }),
    h('button', { class: 'btn btn--primary', onclick: () => navigate('#/home') }, 'اكتب أول رسالة'),
  );

  page.replaceChildren(
    h('header', { class: 'topbar' }, h('h1', { class: 'page-title', text: 'الرسائل' })),
    list,
  );
  draw();
  store.sync().catch(() => {});
  return store.subscribe((type) => { if (type === 'sync') draw(); });
}
