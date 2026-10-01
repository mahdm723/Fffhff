// Public profile preview: "dzplay", public idea stats and posts. Nothing personal.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { profilePosts } from '../ideas.js';
import { avatar, h } from '../ui.js';

export function renderUser(page, { ref, navigate }) {
  const stats = h('div', { class: 'stats' });
  const posts = h('div');
  const stat = (num, label) => h('div', { class: 'stat glass' }, h('div', { class: 'stat__num', text: String(num) }), h('div', { class: 'stat__label', text: label }));

  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => (history.length > 1 ? history.back() : navigate('#/home')) }, icon('back')),
      h('h1', { class: 'page-title', text: 'ملف عام' }),
    ),
    h('section', { class: 'id-card glass' },
      avatar('xl'),
      h('div', { class: 'id-card__name', text: 'dzplay' }),
      h('p', { class: 'id-card__hint', text: 'هوية مجهولة. يظهر هنا فقط ما شاركه علنًا.' }),
    ),
    stats,
    posts,
  );
  stats.replaceChildren(stat('–', 'منشورات'), stat('–', 'إعجاب'), stat('–', 'عدم إعجاب'));

  api.get(`/api/profiles/${encodeURIComponent(ref)}`).then((p) => {
    if (p.is_me) { navigate('#/profile'); return; }
    stats.replaceChildren(stat(p.stats.posts, 'منشورات'), stat(p.stats.likes, 'إعجاب'), stat(p.stats.dislikes, 'عدم إعجاب'));
    posts.replaceChildren(
      h('h2', { class: 'section-title', text: 'أفكاره' }),
      profilePosts(ref, { navigate, emptyText: 'لا توجد منشورات.' }),
    );
  }).catch(() => {
    posts.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('h2', { text: 'هذا الملف غير متاح' })));
  });
}
