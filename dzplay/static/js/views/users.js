// V6 phase 3: «المستخدمون» — search (name or DZ-ID) on top, then everyone who chose to be listed, most
// recently active first. Only the public card: picture, name, star, DZ-ID. Tapping opens the public profile.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { infiniteSentinel } from '../ideas.js';
import { searchBox } from '../people.js';
import { h, nameLine, personAvatar, toast } from '../ui.js';

export function renderUsers(page, { navigate }) {
  const list = h('div', { class: 'users-list', role: 'list', 'aria-label': 'كل المستخدمين' });
  const status = h('div', { class: 'feed-status' });
  let cursor = null;
  let done = false;
  let loading = false;

  function personRow(p) {
    return h('button', { class: 'user-row', type: 'button', role: 'listitem', onclick: () => navigate(`#/id/${p.public_id}`) },
      personAvatar(p.name, { url: p.avatar_url }),
      h('span', { class: 'user-row__text' }, nameLine(p.name, p.gender, 'user-row__name', p.verified),
        h('small', { class: 'user-row__id', dir: 'ltr', text: p.public_id })),
      icon('chev', 'chev'));
  }

  async function load() {
    if (loading || done) return;
    loading = true;
    status.replaceChildren(h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' }));
    try {
      const d = await api.get(`/api/people${cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''}`);
      list.append(...d.results.map(personRow));
      cursor = d.next_cursor;
      done = !d.next_cursor;
      status.replaceChildren(list.children.length ? '' : h('p', { class: 'feed-end', text: 'لا يوجد مستخدمون آخرون بعد. ادعُ أصدقاءك.' }));
    } catch (err) {
      status.replaceChildren(h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => load() }, 'إعادة المحاولة'));
      if (!err.isNetwork) toast(err.message, 'error');
    } finally {
      loading = false;
    }
  }

  const sentinel = infiniteSentinel(() => { if (list.children.length) load(); });
  page.replaceChildren(
    h('header', { class: 'topbar' }, h('h1', { class: 'page-title', text: 'المستخدمون' })),
    searchBox(navigate),
    h('h2', { class: 'section-title', text: 'نشطون مؤخرًا' }),
    h('section', { class: 'users glass' }, list),
    status, sentinel);
  load();
  return () => sentinel.stop();
}
