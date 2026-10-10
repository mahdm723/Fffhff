// V6 phase 4: one idea on its own page (from a notification), with its comments opened.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { postCard } from '../ideas.js';
import { h } from '../ui.js';

export function renderPost(page, { postId, commentId, navigate }) {
  const slot = h('div', { class: 'feed' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => (history.length > 1 ? history.back() : navigate('#/home')) }, icon('back')),
      h('h1', { class: 'page-title', text: 'فكرة' })),
    slot);
  (async () => {
    try {
      const post = await api.get(`/api/posts/${encodeURIComponent(postId)}`);
      const card = postCard(post, { navigate, onRemoved: () => navigate('#/home') });
      slot.replaceChildren(card);
      card.openComments(commentId);
    } catch {
      slot.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('h2', { text: 'هذه الفكرة لم تعد متاحة' })));
    }
  })();
}
