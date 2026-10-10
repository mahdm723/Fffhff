// Admin panel — content: all Ideas (with private comments), conversations, search, deletion,
// and multi-selection feeding the team-comments tool.
import { h, toast } from '/js/ui.js';
import {
  attempt, authorLine, call, chip, confirmDanger, detailSheet, emptyState, fmt, hooks, qs, sectionHead, segmented, spinner, when,
} from './admin-common.js';
import { openConversation } from './admin-users.js';

export const selection = { type: 'idea', ids: new Set() };
const view = { tab: 'ideas', q: { ideas: '', conversations: '', search: '' } }; // one search term per sub-tab
let selectionBar = null;

const counts = (o) => `👍 ${fmt(o.real.likes)} · 👎 ${fmt(o.real.dislikes)}`;

function paintSelection() {
  if (!selectionBar) return;
  const n = selection.ids.size;
  selectionBar.hidden = !n;
  selectionBar.querySelector('.admin-selbar__n').textContent = `${fmt(n)} فكرة محددة`;
}

function selectBox(type, id) {
  const box = h('input', { type: 'checkbox', class: 'admin-select-box', 'aria-label': 'تحديد', checked: selection.type === type && selection.ids.has(id) ? true : null });
  box.addEventListener('click', (e) => e.stopPropagation());
  box.addEventListener('change', () => {
    if (selection.type !== type) { selection.type = type; selection.ids.clear(); document.querySelectorAll('.admin-select-box').forEach((b) => { if (b !== box) b.checked = false; }); }
    if (box.checked) selection.ids.add(id); else selection.ids.delete(id);
    paintSelection();
  });
  return box;
}

export function renderContent(main) {
  const body = h('div', { class: 'admin-section' });
  selectionBar = h('div', { class: 'admin-selbar glass glass--blur', hidden: true },
    h('b', { class: 'admin-selbar__n' }),
    h('button', { type: 'button', class: 'btn btn--primary btn--sm', onclick: () => hooks.openEngage('comments', selection) }, 'إضافة تعليقات'),
    h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => {
      selection.ids.clear(); document.querySelectorAll('.admin-select-box').forEach((b) => { b.checked = false; }); paintSelection();
    } }, 'إلغاء التحديد'));
  const show = { ideas: showIdeas, conversations: showConversations, search: showSearch };
  main.replaceChildren(
    sectionHead('المحتوى'),
    segmented([['ideas', 'الأفكار'], ['conversations', 'المحادثات'], ['search', 'بحث']], view.tab,
      (v) => { view.tab = v; show[v](body); }, 'نوع المحتوى'),
    body, selectionBar);
  show[view.tab](body);
  paintSelection();
}

function searchForm(key, placeholder, onSearch) {
  const q = h('input', { class: 'input', type: 'search', dir: 'auto', value: view.q[key], placeholder });
  return h('form', { class: 'admin-filter', onsubmit: (e) => { e.preventDefault(); view.q[key] = q.value.trim(); onSearch(view.q[key]); } },
    q, h('button', { type: 'submit', class: 'btn btn--ghost btn--sm' }, 'بحث'));
}

// ------------------------------------------------------------------ ideas

async function showIdeas(body) {
  const list = h('div', { class: 'admin-list' });
  const load = async (q) => {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/access/ideas${qs({ q, size: 50 })}`));
    list.replaceChildren();
    if (!d) return;
    if (!d.ideas.length) { list.append(emptyState('لا أفكار.')); return; }
    list.append(h('p', { class: 'admin-meta', text: `${fmt(d.total)} فكرة` }), ...d.ideas.map((p) => h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-card__head' }, selectBox('idea', p.id), authorLine(p.author),
        p.status !== 'visible' ? chip('محذوفة', 'chip--hot') : null, h('time', { class: 'admin-card__time', text: when(p.created_at) })),
      h('p', { class: 'admin-text', dir: 'auto', text: p.content }),
      h('p', { class: 'admin-meta', text: `${counts(p)} · 💬 ${fmt(p.comments)}` }),
      h('div', { class: 'admin-actions' },
        h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => openIdea(p.id) }, 'كل التعليقات'),
        h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
          if (!(await confirmDanger('حذف الفكرة؟', p.content.slice(0, 120), 'حذف'))) return;
          if (await attempt(() => call('DELETE', `/api/admin/access/content/idea/${encodeURIComponent(p.id)}`))) { toast('حُذفت.'); load(view.q.ideas); }
        } }, 'حذف')))));
  };
  body.replaceChildren(searchForm('ideas', 'بحث في نص الأفكار', load), list);
  load(view.q.ideas);
}

export function openIdea(pid) {
  detailSheet('فكرة وكل تعليقاتها', async (body, close) => {
    const paint = async () => {
      const d = await attempt(() => call('GET', `/api/admin/access/ideas/${encodeURIComponent(pid)}`));
      if (!d) { close(); return; }
      body.replaceChildren(
        h('div', { class: 'admin-group glass' }, authorLine(d.idea.author), h('p', { class: 'admin-text', dir: 'auto', text: d.idea.content }),
          h('p', { class: 'admin-meta', text: counts(d.idea) })),
        h('p', { class: 'admin-meta', text: 'التعليقات على الأفكار يراها صاحب الفكرة فقط داخل التطبيق.' }),
        ...(d.comments.length ? d.comments.map((c) => commentRow(c, 'idea_comment', paint)) : [emptyState('لا تعليقات.')]));
    };
    paint();
  });
}

function commentRow(c, kind, after) {
  return h('div', { class: `admin-card glass ${c.author && c.author.team ? 'admin-card--team' : ''}` },
    h('div', { class: 'admin-card__head' }, authorLine(c.author), h('time', { class: 'admin-card__time', text: when(c.created_at) })),
    h('p', { class: 'admin-text', dir: 'auto', text: c.content }),
    h('div', { class: 'admin-actions' }, h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
      if (!(await confirmDanger('حذف التعليق؟', c.content.slice(0, 120), 'حذف'))) return;
      if (await attempt(() => call('DELETE', `/api/admin/access/content/${kind}/${encodeURIComponent(c.id)}`))) { toast('حُذف.'); after(); }
    } }, 'حذف')));
}

// ------------------------------------------------------------------ conversations

async function showConversations(body) {
  const list = h('div', { class: 'admin-list' });
  const load = async (q) => {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/access/conversations${qs({ q, size: 50 })}`));
    list.replaceChildren();
    if (!d) return;
    if (!d.conversations.length) { list.append(emptyState('لا محادثات محفوظة.')); return; }
    list.append(h('p', { class: 'admin-meta', text: `${fmt(d.total)} محادثة محفوظة (الرسائل تُحذف تلقائيًا بعد مدتها).` }),
      ...d.conversations.map((c) => h('button', { type: 'button', class: 'admin-card glass admin-card--tap', onclick: () => openConversation(c.id) },
        h('div', { class: 'admin-card__head' }, authorLine(c.initiator), h('span', { text: '⇄' }), authorLine(c.recipient)),
        h('p', { class: 'admin-meta', text: `${fmt(c.messages_stored)} رسالة · آخر نشاط ${when(c.last_message_at)} · ${c.status === 'active' ? 'نشطة' : 'مغلقة'}` }))));
  };
  body.replaceChildren(searchForm('conversations', 'بحث في نص الرسائل', load), list);
  load(view.q.conversations);
}

// ------------------------------------------------------------------ search

function showSearch(body) {
  const out = h('div', { class: 'admin-list' });
  const groups = [['ideas', 'أفكار', 'idea'], ['idea_comments', 'تعليقات الأفكار', 'idea_comment'], ['messages', 'رسائل', 'message']];
  const run = async (q) => {
    if (q.length < 2) { toast('اكتب حرفين على الأقل.', 'error'); return; }
    out.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/access/search${qs({ q })}`));
    out.replaceChildren();
    if (!d) return;
    let any = false;
    for (const [key, label, kind] of groups) {
      const rows = d[key] || [];
      if (!rows.length) continue;
      any = true;
      out.append(h('h3', { class: 'admin-group__title', text: `${label} (${fmt(rows.length)})` }), ...rows.map((r) => h('div', { class: 'admin-card glass' },
        h('p', { class: 'admin-text', dir: 'auto', text: r.content || '' }),
        h('div', { class: 'admin-actions' },
          h('span', { class: 'admin-meta', text: when(r.created_at) }),
          key === 'ideas' || key === 'idea_comments' ? h('button', { type: 'button', class: 'admin-link', onclick: () => openIdea(r.post_id || r.id) }, 'فتح الفكرة') : null,
          key === 'messages' ? h('button', { type: 'button', class: 'admin-link', onclick: () => openConversation(r.conversation_id) }, 'فتح المحادثة') : null,
          kind ? h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
            if (!(await confirmDanger('حذف؟', (r.content || '').slice(0, 120), 'حذف'))) return;
            if (await attempt(() => call('DELETE', `/api/admin/access/content/${kind}/${encodeURIComponent(r.id)}`))) { toast('حُذف.'); run(q); }
          } }, 'حذف') : null))));
    }
    if (!any) out.append(emptyState('لا نتائج.'));
  };
  body.replaceChildren(searchForm('search', 'ابحث في الأفكار والتعليقات والرسائل', run), out);
  if (view.q.search.length >= 2) run(view.q.search);
}
