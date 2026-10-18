// Admin panel — users: search/filters, full user page, actions, conversations.
import { appName } from '/js/brand.js';
import { h, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  attempt, authorLine, call, chip, confirmDanger, detailSheet, emptyState, fmt, hooks, qs, sectionHead, shortRef, spinner,
  userRef, when,
} from './admin-common.js';
import { userV5Section } from './admin-v5.js';
import { userMoneySection } from './admin-money.js';

export const STATUS = { active: 'نشط', suspended: 'موقوف', banned: 'محظور' };
const METHOD = { email: 'بريد', google: 'Google', 'google+email': 'بريد + Google' };
// V6 phase 15: how the signup was confirmed
const VERIFIED = { email: 'تأكيد بالبريد', admin_code: 'رمز إداري', google: 'تأكيد Google' };
const filters = { q: '', status: '', method: '', flagged: false, has_posts: false };

function select(options, value) {
  const el = h('select', { class: 'input admin-select' }, ...options.map(([v, t]) => h('option', { value: v, text: t })));
  el.value = value;
  return el;
}

function check(label, value) {
  const input = h('input', { type: 'checkbox', checked: value || null });
  return { el: h('label', { class: 'admin-check' }, input, label), input };
}

export function renderUsers(main) {
  const q = h('input', { class: 'input', type: 'search', dir: 'auto', value: filters.q, placeholder: 'بحث بالبريد أو المعرّف أو مرجع الملف' });
  const status = select([['', 'كل الحالات'], ...Object.entries(STATUS)], filters.status);
  const method = select([['', 'كل طرق التسجيل'], ['email', 'بريد'], ['google', 'Google']], filters.method);
  const flagged = check('عليهم بلاغات أو رصد', filters.flagged);
  const posts = check('لديهم منشورات', filters.has_posts);
  const total = h('p', { class: 'admin-meta' });
  const list = h('div', { class: 'admin-list' });
  const more = h('button', { type: 'button', class: 'btn btn--ghost btn--block', hidden: true }, 'عرض المزيد');
  let page = 0;

  async function load(reset) {
    if (reset) { page = 0; list.replaceChildren(spinner()); }
    const data = await attempt(() => call('GET', `/api/admin/access/users${qs({ ...filters, page })}`));
    if (reset) list.replaceChildren();
    if (!data) return;
    total.textContent = `${fmt(data.total)} حساب`;
    if (!data.users.length && reset) list.append(emptyState('لا نتائج.'));
    list.append(...data.users.map(userCard));
    more.hidden = (page + 1) * data.size >= data.total;
  }
  more.addEventListener('click', () => { page += 1; load(false); });
  const form = h('form', {
    class: 'admin-filters glass',
    onsubmit: (e) => {
      e.preventDefault();
      Object.assign(filters, { q: q.value.trim(), status: status.value, method: method.value, flagged: flagged.input.checked,
        has_posts: posts.input.checked });
      load(true);
    },
  }, q, h('div', { class: 'admin-filters__row' }, status, method), h('div', { class: 'admin-filters__row' }, flagged.el, posts.el),
  h('button', { type: 'submit', class: 'btn btn--primary btn--block' }, icon('user'), 'بحث'));
  main.replaceChildren(sectionHead('المستخدمون'), form, imagesPanel(), total, list, more);
  load(true);
}

// V6 phase 17: who may post pictures — granted account by account (or several at once), never by the membership
function imagesPanel() {
  const picked = new Set();
  const count = h('span', { class: 'admin-meta' });
  const body = h('div', { class: 'admin-images__body' });
  async function bulk(on) {
    if (!picked.size) return;
    if (!on && !(await confirmDanger('سحب إذن نشر الصور؟', `${fmt(picked.size)} حساب`, 'سحب'))) return;
    const r = await attempt(() => call('POST', '/api/admin/images/bulk', { ids: [...picked], on }));
    if (r) { toast(`تم: ${fmt(r.changed)} حساب.`); picked.clear(); load(); }
  }
  const bar = h('div', { class: 'admin-actions admin-images__bar', hidden: true }, count,
    h('button', { type: 'button', class: 'btn btn--sm btn--primary', onclick: () => bulk(true) }, 'منح المحددين'),
    h('button', { type: 'button', class: 'btn btn--sm btn--danger', onclick: () => bulk(false) }, 'سحب من المحددين'));
  const paintCount = () => { count.textContent = `${fmt(picked.size)} محدد`; bar.hidden = !picked.size; };
  const row = (u) => {
    const box = h('input', { type: 'checkbox', 'aria-label': 'تحديد', checked: picked.has(u.id) ? true : null });
    box.addEventListener('change', () => { if (box.checked) picked.add(u.id); else picked.delete(u.id); paintCount(); });
    const meta = [u.email, u.image_posts ? `${fmt(u.image_posts)} منشور بصورة، آخرها ${when(u.last_image_post)}` : '',
      u.allowed && u.granted_at ? `مسموح منذ ${when(u.granted_at)}` : ''].filter(Boolean).join(' · ');
    return h('label', { class: 'admin-check admin-pick' }, box,
      h('span', {}, h('bdi', { text: u.name || 'dzplay' }), ' ', h('code', { dir: 'ltr', text: u.public_id || '' })),
      u.allowed ? chip('مسموح', 'chip--team') : null,
      h('span', { class: 'admin-meta', dir: 'auto', text: meta }));
  };
  async function load() {
    body.replaceChildren(spinner());
    const d = await attempt(() => call('GET', '/api/admin/images'));
    if (!d) return;
    body.replaceChildren(
      h('h4', { text: `لديهم الإذن الآن (${fmt(d.holders.length)})` }),
      d.holders.length ? h('div', { class: 'admin-picklist' }, ...d.holders.map(row)) : emptyState('لا أحد بعد.'),
      h('h4', { text: `من نشر صورًا سابقًا (${fmt(d.former.length)})` }),
      d.former.length ? h('div', { class: 'admin-picklist' }, ...d.former.map(row)) : emptyState('لا أحد.'));
    paintCount();
  }
  const fold = h('details', { class: 'admin-group glass admin-images' },
    h('summary', { text: 'إذن نشر الصور' }),
    h('p', { class: 'admin-meta', text: 'يُمنح لكل حساب على حدة ولا يرتبط بالعضوية. الحد اليومي من «الإعدادات ← الأفكار». المنشورات القديمة بالصور تبقى كما هي.' }),
    body, bar);
  fold.addEventListener('toggle', () => { if (fold.open) load(); });
  return fold;
}

function userCard(u) {
  return h('article', { class: 'admin-card glass admin-card--tap', tabindex: '0', role: 'button', onclick: () => openUser(u.id),
    onkeydown: (e) => { if (e.key === 'Enter') openUser(u.id); } },
  h('div', { class: 'admin-card__head' },
    h('b', { class: 'admin-email', dir: 'ltr', text: u.email || '—' }),
    h('span', { class: 'admin-name', dir: 'auto', text: u.display_name || 'dzplay' }),
    u.public_id ? h('code', { dir: 'ltr', text: u.public_id }) : null,
    chip(STATUS[u.status] || u.status, u.status !== 'active' ? 'chip--hot' : ''),
    chip(METHOD[u.method] || u.method),
    u.verified_via ? chip(VERIFIED[u.verified_via] || u.verified_via, u.verified_via === 'admin_code' ? 'chip--warn' : '') : null),
  h('div', { class: 'admin-card__meta' },
    h('span', {}, 'التسجيل: ', when(u.created_at)),
    h('span', {}, 'آخر نشاط: ', when(u.last_active_at)),
    h('span', {}, 'المعرّف: ', h('code', { dir: 'ltr', text: shortRef(u.id) }))),
  h('div', { class: 'admin-card__meta' },
    h('span', {}, `منشورات ${fmt(u.stats.posts)}`), h('span', {}, `رسائل ${fmt(u.stats.messages_sent)}`),
    h('span', {}, `محادثات ${fmt(u.stats.conversations)}`),
    u.stats.reports_against ? h('b', { class: 'admin-warn', text: `بلاغات ${fmt(u.stats.reports_against)}` }) : null,
    u.stats.flags ? h('b', { class: 'admin-warn', text: `رصد ${fmt(u.stats.flags)}` }) : null));
}

function section(title, count, ...children) {
  return h('details', { class: 'admin-fold glass', open: count ? null : null },
    h('summary', {}, h('span', { text: title }), h('span', { class: 'chip', text: fmt(count) })), ...children);
}

const GENDER = { male: 'رجل', female: 'أنثى', unspecified: 'أفضّل عدم الذكر' };

const LEVEL = { tee: 'TEE (عتاد آمن)', strongbox: 'StrongBox (شريحة آمنة)' };

// V6 phase 16: «مشبوه» — no automatic membership grant for this account (or for any account on a marked phone)
function markButton(url, on, what, paint) {
  const reason = h('input', { class: 'input', maxlength: '255', placeholder: `سبب تعليم ${what} كمشبوه` });
  const go = async () => {
    if (!on && reason.value.trim().length < 3) { toast('اكتب السبب (3 أحرف على الأقل).', 'error'); reason.focus(); return; }
    if (await attempt(() => call('POST', url, { on: !on, reason: on ? null : reason.value.trim() }))) { toast('تم.'); paint(); }
  };
  return h('div', { class: 'admin-actions' }, on ? null : reason,
    h('button', { type: 'button', class: `btn btn--sm ${on ? 'btn--ghost' : 'btn--danger'}`, onclick: go },
      on ? `إزالة علامة ${what} المشبوه` : `تعليم ${what} كمشبوه`));
}

// V6 phase 17: the picture permission of this account (granted by an admin only; the membership never gives it)
function imagesBox(u, im, paint) {
  const on = !!im.allowed;
  return h('section', { class: 'admin-group glass admin-images' },
    h('h3', { class: 'admin-group__title', text: 'نشر الصور مع الأفكار' }),
    h('p', { class: 'admin-meta', text: on ? `مسموح منذ ${when(im.granted_at)} (${im.granted_by || '—'})` : 'غير مسموح. لا يرتبط بالعضوية.' }),
    h('p', { class: 'admin-meta', text: `منشورات بصور: ${fmt(im.image_posts || 0)}` }),
    h('div', { class: 'admin-actions' }, h('button', {
      type: 'button', class: `btn btn--sm ${on ? 'btn--danger' : 'btn--primary'}`,
      onclick: async () => {
        if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(u.id)}/images`, { on: !on }))) {
          toast(on ? 'سُحب الإذن.' : 'مُنح الإذن.');
          paint();
        }
      },
    }, on ? 'سحب إذن نشر الصور' : 'منح إذن نشر الصور')));
}

function suspectBox(u, s, paint) {
  const reasons = s.reasons || [];
  return h('section', { class: 'admin-group glass admin-suspect' },
    h('h3', { class: 'admin-group__title', text: 'مشبوه؟ (لا منح تلقائي للعضوية)' }),
    reasons.length ? h('ul', { class: 'admin-auto__why' }, ...reasons.map((x) => h('li', { text: x })))
      : h('p', { class: 'admin-meta', text: 'لا إشارات. مشاركة الشبكة نفسها لا تُعدّ إشارة.' }),
    markButton(`/api/admin/users/${encodeURIComponent(u.id)}/suspicious`, !!s.flagged, 'الحساب', paint));
}

function privacyText(p) {
  if (!p) return '—';
  return [p.accept_direct === 'everyone' ? 'يستقبل المباشرة' : 'لا يستقبل المباشرة',
    p.searchable_by_name ? 'يظهر في البحث' : 'مخفي من البحث بالاسم'].join(' · ');
}

export function openUser(id) {
  detailSheet('صفحة المستخدم', async (body, close) => {
    const paint = async () => {
      const d = await attempt(() => call('GET', `/api/admin/access/users/${encodeURIComponent(id)}`));
      if (!d) { close(); return; }
      const u = d.user;
      const act = (label, cls, fn) => h('button', { type: 'button', class: `btn btn--sm ${cls}`, onclick: fn }, label);
      const setStatus = async (s) => {
        if (s !== 'active' && !(await confirmDanger(`${s === 'banned' ? 'حظر' : 'إيقاف'} الحساب؟`, u.email || u.id))) return;
        if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(u.id)}/status`, { status: s }))) { toast('تم.'); paint(); }
      };
      body.replaceChildren(
        h('section', { class: 'admin-group glass' },
          h('h3', { class: 'admin-email', dir: 'ltr', text: u.email || (u.team === 'official' ? `${appName()} الرسمي` : 'حساب نظام') }),
          h('dl', { class: 'admin-group__rows' },
            ...[['الاسم الظاهر', u.display_name || 'dzplay (افتراضي)'], ['المعرّف العام', u.public_id || '—'],
              ['الجنس', GENDER[u.gender] || 'غير محدد'], ['تأكيد 18+', u.age_confirmed_at ? when(u.age_confirmed_at) : 'لم يؤكد بعد'],
              ['الخصوصية', privacyText(u.privacy)],
              ['المعرّف الداخلي', u.id], ['طريقة التسجيل', METHOD[u.method] || u.method],
              ['تأكيد التسجيل', VERIFIED[u.verified_via] || (u.email_verified_at ? 'بريد مؤكد' : 'دون تأكيد (قبل التحديث أو التأكيد موقوف)')],
              ['الحالة', STATUS[u.status] || u.status],
              ['التسجيل', when(u.created_at)], ['آخر نشاط', when(u.last_active_at)], ['جلسات نشطة', fmt(u.sessions_active)],
              ['رسائل مرسلة / مستلمة', `${fmt(u.stats.messages_sent)} / ${fmt(u.stats.messages_received)}`],
              ['مرجع الملف العام', u.profile_ref || '—']]
              .map(([k, v]) => h('div', { class: 'admin-row' }, h('dt', { text: k }), h('dd', { dir: 'auto', text: String(v) })))),
          h('div', { class: 'admin-actions' },
            u.status !== 'active' ? act('رفع الإيقاف/الحظر', 'btn--ghost', () => setStatus('active')) : null,
            u.status !== 'suspended' ? act('إيقاف', 'btn--danger', () => setStatus('suspended')) : null,
            u.status !== 'banned' ? act('حظر', 'btn--danger', () => setStatus('banned')) : null,
            act('إنهاء كل الجلسات', 'btn--ghost', async () => {
              const r = await attempt(() => call('POST', `/api/admin/access/users/${encodeURIComponent(u.id)}/revoke-sessions`));
              if (r) { toast(`أُنهيت ${fmt(r.revoked)} جلسة.`); paint(); }
            }),
            act('حذف الحساب', 'btn--danger', async () => {
              if (!(await confirmDanger('حذف الحساب نهائيًا؟', 'تُحذف منشوراته وتعليقاته ومحادثاته وتفاعلاته. لا يمكن التراجع.', 'حذف'))) return;
              if (await attempt(() => call('DELETE', `/api/admin/access/users/${encodeURIComponent(u.id)}`))) { toast('حُذف الحساب.'); close(); }
            }))),
        await userV5Section(u.id, paint),
        await userMoneySection(u.id, paint),
        imagesBox(u, d.images || {}, paint),
        suspectBox(u, d.suspicion || {}, paint),
        section('الأجهزة (تطبيق أندرويد)', (d.devices || []).length,
          h('p', { class: 'admin-meta', text: 'هواتف أثبت فيها التطبيق نفسه (Key Attestation). على iPhone لا يوجد ربط بالجهاز.' }),
          ...(d.devices || []).map((v) => h('div', { class: 'admin-line admin-line--col' },
            h('span', {}, chip(LEVEL[v.security_level] || v.security_level), v.registered_here ? chip('أُنشئ منه الحساب', 'chip--warn') : null,
              chip(`${fmt(v.accounts)} حساب على هذا الجهاز`, v.accounts > 1 ? 'chip--hot' : ''),
              v.suspicious ? chip(`مشبوه: ${v.suspicious_reason || ''}`, 'chip--hot') : null,
              h('code', { dir: 'ltr', text: shortRef(v.id) })),
            h('span', { class: 'admin-meta', text: `الإصدار ${v.version_code || '—'} · أول ظهور ${when(v.first_seen)} · آخر ظهور ${when(v.last_seen)}` }),
            markButton(`/api/admin/devices/${encodeURIComponent(v.id)}/suspicious`, v.suspicious, 'الجهاز', paint)))),
        section('سجل الأسماء (للإدارة فقط)', d.name_history.length, ...d.name_history.map((n) => h('div', { class: 'admin-line' },
          h('bdi', { text: n.old || 'dzplay' }), h('span', { text: '←' }), h('bdi', { text: n.new || 'dzplay' }),
          h('span', { class: 'admin-meta', text: when(n.at) })))),
        section('الشبكات والحسابات المشتركة', d.shared_network_accounts.length,
          h('p', { class: 'admin-meta', text: `بصمات الشبكات: ${d.networks.join('، ') || '—'} (لا تُحفظ عناوين IP الحقيقية).` }),
          ...d.shared_network_accounts.map((a) => h('div', { class: 'admin-line' }, authorLine(a), chip(STATUS[a.status] || a.status)))),
        section('المنشورات', d.posts.length, ...d.posts.map((p) => h('div', { class: 'admin-line admin-line--col' },
          h('p', { class: 'admin-text', dir: 'auto', text: p.content }),
          h('span', { class: 'admin-meta' }, `${when(p.created_at)} · 👍 ${fmt(p.real.likes)} · 👎 ${fmt(p.real.dislikes)} · 💬 ${fmt(p.comments)}`,
            h('button', { type: 'button', class: 'admin-link', onclick: () => hooks.openIdea && hooks.openIdea(p.id) }, ' التعليقات'))))),
        section('تعليقاته على الأفكار', d.idea_comments.length, ...d.idea_comments.map((c) => h('div', { class: 'admin-line admin-line--col' },
          h('p', { class: 'admin-text', dir: 'auto', text: c.content }), h('span', { class: 'admin-meta', text: when(c.created_at) })))),
        section('التفاعلات', d.reactions.length, ...d.reactions.map((r) => h('div', { class: 'admin-line' },
          h('span', { text: `${r.reaction === 'like' ? '👍' : '👎'} فكرة` }),
          h('code', { dir: 'ltr', text: shortRef(r.id) }), h('span', { class: 'admin-meta', text: when(r.at) })))),
        section('المحادثات', d.conversations.length, ...d.conversations.map((c) => h('button', {
          type: 'button', class: 'admin-line admin-line--btn', onclick: () => openConversation(c.id),
        }, chip(c.kind === 'direct' ? 'مباشرة' : 'مجهولة قديمة'), h('span', { text: c.started_by_user ? 'بدأها ←' : '← بدأها الطرف الآخر' }), authorLine(c.peer),
        h('span', { class: 'admin-meta', text: `${fmt(c.messages_stored)} رسالة · ${when(c.last_message_at)}` })))),
        section('بلاغات ضده', d.reports_against.length, ...d.reports_against.map(reportLine)),
        section('بلاغات قدّمها', d.reports_by.length, ...d.reports_by.map(reportLine)),
        section('الدخول والأحداث الأمنية', d.security_events.length, ...d.security_events.map((e) => h('div', { class: 'admin-line' },
          h('b', { text: e.type }), e.ip_ref ? h('code', { dir: 'ltr', text: e.ip_ref }) : null,
          h('span', { class: 'admin-meta', text: when(e.at) })))),
      );
    };
    paint();
  });
}

function reportLine(r) {
  return h('div', { class: 'admin-line' }, chip(r.reason, 'chip--hot'), chip(r.status), h('span', { class: 'admin-meta', text: when(r.created_at) }));
}

export function openConversation(cid) {
  detailSheet('محادثة', async (body, close) => {
    const d = await attempt(() => call('GET', `/api/admin/access/conversations/${encodeURIComponent(cid)}`));
    if (!d) { close(); return; }
    const c = d.conversation;
    const side = (m) => (m.sender_id === (c.initiator && c.initiator.id) ? 'user' : 'peer');
    body.replaceChildren(
      h('div', { class: 'admin-group glass' },
        h('div', { class: 'admin-line' }, h('b', { text: 'بدأها:' }), authorLine(c.initiator)),
        h('div', { class: 'admin-line' }, h('b', { text: 'الطرف الآخر:' }), authorLine(c.recipient)),
        h('p', { class: 'admin-meta', text: `تُحذف تلقائيًا ${when(c.expires_at)} وفق مدة الاحتفاظ.` }),
        h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
          if (!(await confirmDanger('حذف المحادثة؟', 'تُحذف كل رسائلها للطرفين.', 'حذف'))) return;
          if (await attempt(() => call('DELETE', `/api/admin/access/content/conversation/${encodeURIComponent(cid)}`))) { toast('حُذفت.'); close(); }
        } }, 'حذف المحادثة')),
      h('div', { class: 'admin-conv__msgs' }, ...(d.messages.length ? d.messages.map((m) => h('div', { class: `admin-msg admin-msg--${side(m)}${m.flagged ? ' admin-msg--flagged' : ''}` },
        h('span', { class: 'admin-msg__who', text: side(m) === 'user' ? 'من بدأ المحادثة' : 'الطرف الآخر' }),
        h('p', { dir: 'auto', text: m.content }),
        h('span', { class: 'admin-msg__meta', text: `${when(m.created_at)}${m.read_at ? ' · مقروءة' : ''}${m.flagged ? ' · مرصودة' : ''}` })))
        : [h('p', { class: 'admin-meta', text: 'لا رسائل محفوظة (انتهت مدتها).' })])));
  });
}
