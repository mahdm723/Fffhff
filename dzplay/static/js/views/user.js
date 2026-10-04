// Public profile: chosen name, gender icon, DZ-ID, public idea stats and posts. Never the e-mail.
// Reached by profile ref (#/u/<ref>, from Ideas) or by public ID (#/id/DZ-XXXXXX, from chats).
import { api } from '../api.js';
import { icon } from '../icons.js';
import { profilePosts } from '../ideas.js';
import { messagePerson } from '../people.js';
import { h, idChip, nameLine, personAvatar } from '../ui.js';

export function renderUser(page, { ref, publicId, navigate }) {
  const stats = h('div', { class: 'stats' });
  const posts = h('div');
  const card = h('section', { class: 'id-card glass' }, personAvatar('dzplay', { size: 'xl' }), h('div', { class: 'id-card__name' }, nameLine('…')));
  const stat = (num, label) => h('div', { class: 'stat glass' }, h('div', { class: 'stat__num', text: String(num) }), h('div', { class: 'stat__label', text: label }));

  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => (history.length > 1 ? history.back() : navigate('#/home')) }, icon('back')),
      h('h1', { class: 'page-title', text: 'ملف عام' }),
    ),
    card,
    stats,
    posts,
  );
  stats.replaceChildren(stat('–', 'منشورات'), stat('–', 'إعجاب'), stat('–', 'عدم إعجاب'));

  const unavailable = () => {
    card.replaceChildren(personAvatar('dzplay', { size: 'xl' }), h('div', { class: 'id-card__name' }, nameLine('dzplay')));
    posts.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('h2', { text: 'هذا الملف غير متاح' })));
  };

  (async () => {
    let person = null;
    if (publicId) {
      try { person = await api.get(`/api/people/${encodeURIComponent(publicId)}`); } catch { unavailable(); return; }
      ref = person.profile_ref;
    }
    let p;
    try { p = await api.get(`/api/profiles/${encodeURIComponent(ref)}`); } catch { unavailable(); return; }
    if (p.is_me) { navigate('#/profile'); return; }
    if (!person && p.public_id) person = await api.get(`/api/people/${encodeURIComponent(p.public_id)}`).catch(() => null);
    const msgBtn = person
      ? h('button', { class: 'btn btn--primary', type: 'button', onclick: () => messagePerson(person.public_id, navigate, person) },
        icon('chat'), person.conversation_id ? 'فتح المحادثة' : 'مراسلة')
      : null;
    if (msgBtn && !person.can_message && !person.conversation_id) {
      msgBtn.disabled = true;
      msgBtn.title = 'لا يستقبل رسائل مباشرة';
    }
    card.replaceChildren(...[
      personAvatar(p.name, { size: 'xl' }),
      h('div', { class: 'id-card__name' }, nameLine(p.name, p.gender, '', p.verified)),
      idChip(p.public_id),
      msgBtn ? h('div', { class: 'id-card__actions' }, msgBtn) : null,
    ].filter(Boolean));
    stats.replaceChildren(stat(p.stats.posts, 'منشورات'), stat(p.stats.likes, 'إعجاب'), stat(p.stats.dislikes, 'عدم إعجاب'));
    posts.replaceChildren(
      h('h2', { class: 'section-title', text: 'منشوراته' }),
      profilePosts(ref, { navigate, emptyText: 'لا توجد منشورات.' }),
    );
  })();
}
