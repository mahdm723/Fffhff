// V6 phase 4: «الإشعارات» — comments on my ideas, replies to my comments (later: membership, rewards…).
// Opening the page marks everything read.
import { api } from '../api.js';
import { icon } from '../icons.js';
import * as store from '../store.js';
import { formatListTime, h, personAvatar, toast } from '../ui.js';

const TEXT = {
  comment: (n) => `${n} علّق على فكرتك`,
  reply: (n) => `${n} ردّ على تعليقك`,
};

export function notificationText(n) {
  const who = (n.actor && n.actor.name) || 'DZPLAY';
  if (TEXT[n.kind]) return TEXT[n.kind](who);
  return (n.data && n.data.text) || 'إشعار جديد';
}

export function renderNotifications(page, { navigate, onMe }) {
  const list = h('ul', { class: 'notif-list' }, h('li', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => (history.length > 1 ? history.back() : navigate('#/home')) }, icon('back')),
      h('h1', { class: 'page-title', text: 'الإشعارات' })),
    list);

  function item(n) {
    const open = () => { if (n.post_id) navigate(`#/post/${n.post_id}${n.comment_id ? `/${n.comment_id}` : ''}`); };
    return h('li', {}, h('button', { class: `notif ${n.read ? '' : 'is-unread'}`, type: 'button', onclick: open },
      personAvatar(n.actor ? n.actor.name : 'DZPLAY', { size: 'sm', url: n.actor ? n.actor.avatar_url : null }),
      h('span', { class: 'notif__text' }, h('b', { text: notificationText(n) }),
        n.preview ? h('small', { class: 'notif__preview', dir: 'auto', text: n.preview }) : '',
        h('small', { class: 'notif__time', text: formatListTime(n.created_at) })),
      n.read ? '' : h('span', { class: 'notif__dot', 'aria-label': 'جديد' })));
  }

  (async () => {
    try {
      const d = await api.get('/api/notifications');
      list.replaceChildren(...(d.notifications.length ? d.notifications.map(item)
        : [h('li', { class: 'empty glass' }, icon('bell'), h('h2', { text: 'لا إشعارات بعد' }),
          h('p', { text: 'ستصلك هنا التعليقات على أفكارك والردود على تعليقاتك.' }))]));
      if (d.unread) {
        await api.post('/api/notifications/read', {});
        if (store.state.me) store.state.me.unread_notifications = 0;
        if (onMe) onMe({ unread_notifications: 0 });
      }
    } catch (err) {
      list.replaceChildren(h('li', { class: 'feed-status', text: err.message }));
      if (!err.isNetwork) toast(err.message, 'error');
    }
  })();
}
