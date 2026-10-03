// Admin panel — content: all Ideas (with private comments), Reels, conversations, search, deletion,
// and multi-selection feeding the engagement tools.
import { h, sheet, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  attempt, authorLine, call, chip, confirmDanger, detailSheet, emptyState, fmt, hooks, qs, sectionHead, segmented, spinner, when,
} from './admin-common.js';
import { openConversation } from './admin-users.js';

export const selection = { type: 'idea', ids: new Set() };
const view = { tab: 'ideas', q: { ideas: '', conversations: '', search: '' } }; // one search term per sub-tab
let selectionBar = null;

const counts = (o) => `👍 ${fmt(o.real.likes)} حقيقي + ${fmt(o.boost.likes)} مضاف · 👎 ${fmt(o.real.dislikes)} + ${fmt(o.boost.dislikes)}`;

function paintSelection() {
  if (!selectionBar) return;
  const n = selection.ids.size;
  selectionBar.hidden = !n;
  selectionBar.querySelector('.admin-selbar__n').textContent = `${fmt(n)} ${selection.type === 'idea' ? 'فكرة' : 'Reel'} محدد`;
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
    h('button', { type: 'button', class: 'btn btn--primary btn--sm', onclick: () => hooks.openEngage('boost', selection) }, 'تعزيز التفاعل'),
    h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => hooks.openEngage('comments', selection) }, 'إضافة تعليقات'),
    h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => {
      selection.ids.clear(); document.querySelectorAll('.admin-select-box').forEach((b) => { b.checked = false; }); paintSelection();
    } }, 'إلغاء التحديد'));
  const show = { ideas: showIdeas, reels: showReels, conversations: showConversations, calls: showCalls, search: showSearch };
  main.replaceChildren(
    sectionHead('المحتوى'),
    segmented([['ideas', 'الأفكار'], ['reels', 'Reels'], ['conversations', 'المحادثات'], ['calls', 'المكالمات'], ['search', 'بحث']], view.tab,
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

// ------------------------------------------------------------------ reels

const REEL_STATUS = { visible: 'ظاهر', hidden: 'مخفي', processing: 'قيد التجهيز', failed: 'فشل' };

async function showReels(body) {
  const list = h('div', { class: 'admin-list' }, spinner());
  body.replaceChildren(h('p', { class: 'admin-meta', text: 'يُرفع المحتوى من بوت Telegram. هنا الإدارة والتعليقات والتفاعل.' }), list);
  const d = await attempt(() => call('GET', '/api/admin/reels?limit=100'));
  list.replaceChildren();
  if (!d) return;
  if (!d.reels.length) { list.append(emptyState('لا يوجد محتوى بعد.')); return; }
  list.append(...d.reels.map((r) => reelCard(r, () => showReels(body))));
}

function reelCard(r, reload) {
  const post = async (path, body, msg) => { if (await attempt(() => call('POST', path, body))) { toast(msg); reload(); } };
  const base = `/api/admin/reels/${encodeURIComponent(r.id)}`;
  return h('article', { class: 'admin-card glass admin-reel' },
    h('div', { class: 'admin-reel__row' },
      r.thumb ? h('img', { class: 'admin-reel__thumb', src: r.thumb, alt: '', loading: 'lazy' }) : h('div', { class: 'admin-reel__thumb' }, icon('reels')),
      h('div', { class: 'admin-reel__info' },
        h('div', { class: 'admin-card__head' }, selectBox('reel', r.id), h('code', { dir: 'ltr', text: r.short_id }),
          chip(REEL_STATUS[r.status] || r.status, r.status === 'visible' ? '' : 'chip--hot'), r.pinned_until ? chip('📌 مثبّت') : null),
        h('p', { class: 'admin-text admin-text--clamp', dir: 'auto', text: r.caption || '(بلا وصف)' }),
        h('p', { class: 'admin-meta', text: `👍 ${fmt(r.likes)}+${fmt(r.boost.likes)} · 👎 ${fmt(r.dislikes)}+${fmt(r.boost.dislikes)} · 💬 ${fmt(r.comments)} · 👁 ${fmt(r.views)}` }))),
    h('div', { class: 'admin-actions' },
      r.status === 'visible' ? h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => post(`${base}/status`, { status: 'hidden' }, 'أُخفي.') }, 'إخفاء')
        : r.status === 'hidden' ? h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => post(`${base}/status`, { status: 'visible' }, 'أصبح ظاهرًا.') }, 'إظهار') : null,
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => post(`${base}/pin`, r.pinned_until ? { pinned: false } : { pinned: true }, 'تم.') },
        r.pinned_until ? 'إلغاء التثبيت' : 'تثبيت'),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => editCaption(r, reload) }, 'تعديل الوصف'),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => openReel(r.id) }, 'التعليقات'),
      h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
        if (!(await confirmDanger(`حذف ${r.short_id}؟`, 'يُحذف المحتوى وتعليقاته نهائيًا.', 'حذف'))) return;
        if (await attempt(() => call('DELETE', base))) { toast('حُذف.'); reload(); }
      } }, 'حذف')));
}

function editCaption(r, reload) {
  sheet((panel, close) => {
    const ta = h('textarea', { class: 'input', rows: '5', dir: 'auto' });
    ta.value = r.caption || '';
    panel.append(h('h2', { text: 'تعديل الوصف' }), ta, h('div', { class: 'actions' },
      h('button', { type: 'button', class: 'btn btn--primary btn--block', onclick: async () => {
        if (await attempt(() => call('PUT', `/api/admin/reels/${encodeURIComponent(r.id)}/caption`, { caption: ta.value }))) { toast('حُفظ.'); close(); reload(); }
      } }, 'حفظ'), h('button', { type: 'button', class: 'btn btn--ghost btn--block', onclick: close }, 'إلغاء')));
  });
}

export function openReel(rid) {
  detailSheet('تعليقات المحتوى', async (body, close) => {
    const paint = async () => {
      const d = await attempt(() => call('GET', `/api/admin/access/reels/${encodeURIComponent(rid)}`));
      if (!d) { close(); return; }
      body.replaceChildren(h('div', { class: 'admin-group glass' }, h('p', { class: 'admin-text', dir: 'auto', text: d.reel.caption || '(بلا وصف)' }),
        h('p', { class: 'admin-meta', text: `${counts(d.reel)} · 👁 ${fmt(d.reel.views)}` })),
      ...(d.comments.length ? d.comments.map((c) => commentRow(c, 'reel_comment', paint)) : [emptyState('لا تعليقات.')]));
    };
    paint();
  });
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
  const groups = [['ideas', 'أفكار', 'idea'], ['idea_comments', 'تعليقات الأفكار', 'idea_comment'], ['reel_comments', 'تعليقات Reels', 'reel_comment'],
    ['messages', 'رسائل', 'message'], ['reels', 'Reels', null]];
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
        h('p', { class: 'admin-text', dir: 'auto', text: r.content || r.caption || '' }),
        h('div', { class: 'admin-actions' },
          h('span', { class: 'admin-meta', text: when(r.created_at) }),
          key === 'ideas' || key === 'idea_comments' ? h('button', { type: 'button', class: 'admin-link', onclick: () => openIdea(r.post_id || r.id) }, 'فتح الفكرة') : null,
          key === 'messages' ? h('button', { type: 'button', class: 'admin-link', onclick: () => openConversation(r.conversation_id) }, 'فتح المحادثة') : null,
          key === 'reel_comments' || key === 'reels' ? h('button', { type: 'button', class: 'admin-link', onclick: () => openReel(r.reel_id || r.id) }, 'فتح') : null,
          kind ? h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
            if (!(await confirmDanger('حذف؟', (r.content || '').slice(0, 120), 'حذف'))) return;
            if (await attempt(() => call('DELETE', `/api/admin/access/content/${kind}/${encodeURIComponent(r.id)}`))) { toast('حُذف.'); run(q); }
          } }, 'حذف') : null))));
    }
    if (!any) out.append(emptyState('لا نتائج.'));
  };
  body.replaceChildren(searchForm('search', 'ابحث في الأفكار والتعليقات والرسائل والأوصاف', run), out);
  if (view.q.search.length >= 2) run(view.q.search);
}

// ------------------------------------------------------------------ calls (metadata + quality only, never media)

const CALL_STATE = {
  ended: 'انتهت', missed: 'فائتة', declined: 'مرفوضة', busy: 'مشغول', failed: 'فشلت', canceled: 'أُلغيت',
  calling: 'يتصل', ringing: 'يرن', connected: 'جارية',
};

function callParty(p) {
  if (!p) return h('span', { class: 'admin-meta', text: 'حساب محذوف' });
  return h('button', { type: 'button', class: 'admin-link', onclick: () => hooks.openUser && hooks.openUser(p.id) },
    h('bdi', { text: p.display_name || 'dzplay' }), ' ', h('code', { dir: 'ltr', text: p.public_id || '' }));
}

function qualityLine(q) {
  if (!q) return null;
  const side = (label, s) => (s ? `${label}: RTT ${s.rtt_ms ?? '–'} ms · فقد ${s.loss_pct ?? '–'}% · ${s.kbps_out ?? '–'} kbit/s${s.relay === false ? ' · ⚠️ غير مُرحَّل' : ''}${s.codec_video ? ` · ${s.codec_video}` : ''}${s.height_max ? ` ${s.height_max}p` : ''}` : null);
  return h('span', { class: 'admin-meta', dir: 'auto', text: [side('المتصل', q.caller), side('المستقبِل', q.callee)].filter(Boolean).join(' — ') });
}

async function showCalls(body) {
  body.replaceChildren(spinner());
  const [stats, log] = await Promise.all([
    attempt(() => call('GET', '/api/admin/calls/stats?days=7')),
    attempt(() => call('GET', '/api/admin/calls')),
  ]);
  if (!stats || !log) { body.replaceChildren(emptyState('تعذّر التحميل.')); return; }
  const summary = h('section', { class: 'admin-group glass' },
    h('h3', { text: `آخر 7 أيام${stats.enabled ? '' : ' — المكالمات غير مفعّلة (TURN_SECRET غير مضبوط)'}` }),
    h('dl', { class: 'admin-group__rows' },
      ...[['كل المكالمات', fmt(stats.total)], ['تم الرد', fmt(stats.answered)], ['مجموع الدقائق', fmt(stats.total_minutes)],
        ['متوسط المدة', stats.avg_duration_s == null ? '—' : `${Math.round(stats.avg_duration_s)} ث`],
        ['متوسط RTT', stats.avg_rtt_ms == null ? '—' : `${stats.avg_rtt_ms} ms`], ['متوسط الفقد', stats.avg_loss_pct == null ? '—' : `${stats.avg_loss_pct}%`],
        ['متوسط الإرسال', stats.avg_kbps_out == null ? '—' : `${stats.avg_kbps_out} kbit/s`],
        ['عبر TURN فقط', stats.all_relay ? 'نعم (لا تنكشف عناوين IP)' : '⚠️ لا'],
        ['النتائج', Object.entries(stats.by_state).map(([k, v]) => `${CALL_STATE[k] || k} ${fmt(v)}`).join(' · ') || '—']]
        .map(([k, v]) => h('div', { class: 'admin-row' }, h('dt', { text: k }), h('dd', { dir: 'auto', text: String(v) })))),
    h('p', { class: 'admin-meta', text: 'لا تُسجَّل المكالمات أبدًا: يُحفظ فقط من اتصل بمن ومتى والمدة وجودة الاتصال.' }));
  const list = log.calls.length ? log.calls.map((c) => h('article', { class: 'admin-card glass' },
    h('div', { class: 'admin-card__head' },
      callParty(c.caller), h('span', { text: '←' }), callParty(c.callee),
      chip(c.kind === 'video' || c.video_used ? 'فيديو' : 'صوت'),
      chip(CALL_STATE[c.state] || c.state, ['failed', 'busy'].includes(c.state) ? 'chip--hot' : ''),
      c.reported ? chip('مُبلَّغ عنها', 'chip--hot') : null),
    h('div', { class: 'admin-card__meta' },
      h('span', { text: when(c.created_at) }),
      c.duration != null ? h('span', { text: `المدة ${Math.floor(c.duration / 60)}:${String(c.duration % 60).padStart(2, '0')}` }) : null,
      c.end_reason ? h('span', { text: `السبب: ${c.end_reason}` }) : null),
    qualityLine(c.quality))) : [emptyState('لا توجد مكالمات بعد.')];
  body.replaceChildren(summary, h('p', { class: 'admin-meta', text: `${fmt(log.total)} مكالمة` }), ...list);
}
