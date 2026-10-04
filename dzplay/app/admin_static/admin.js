// DZPLAY admin panel. Lives under the secret ADMIN_PATH; talks only to its own
// API there. Sign-in = admin account + password + TOTP code; the session is an
// HttpOnly cookie scoped to the panel path (never readable from JS).
// All server data is inserted with textContent.
import { h, toast, confirmSheet, wordmark, REPORT_REASONS } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  bytes, call, emptyState, fmt, handleError, hooks, iconButton, sectionHead, segmented, shortRef, spinner, userRef, waitText, when,
} from './admin-common.js';
import { openUser, renderUsers } from './admin-users.js';
import { openIdea, renderContent } from './admin-content.js';
import { openEngage, renderEngage } from './admin-engage.js';
import { renderSystem } from './admin-system.js';
import { renderMedia, renderMoney, renderSettings, renderSupport, renderVerify } from './admin-v5.js';

const REFRESH_MS = 30_000;
const app = document.getElementById('app');
const shortDay = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'short' });
const longDay = new Intl.DateTimeFormat('ar-DZ', { weekday: 'long', day: 'numeric', month: 'long' });
const clockFmt = new Intl.DateTimeFormat('ar-DZ', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const parseDay = (d) => { const [y, m, dd] = d.split('-').map(Number); return new Date(y, m - 1, dd); };

// ------------------------------------------------------------------ labels

const REASONS = Object.fromEntries(REPORT_REASONS);
const TARGETS = { message: 'رسالة', conversation: 'محادثة', post: 'فكرة منشورة', comment: 'تعليق' };
const RESOLUTIONS = { dismiss: 'تم التجاهل', warn: 'تحذير', remove: 'حُذف المحتوى', suspend: 'أُوقف الحساب', ban: 'حُظر الحساب' };
const ACTIONS = {
  dismiss: { label: 'تجاهل', title: 'تجاهل البلاغ؟', text: 'لن يتغير شيء في حساب المستخدم أو المحتوى.', cls: 'btn--ghost' },
  warn: { label: 'تحذير', title: 'تسجيل تحذير؟', text: 'يُغلق البلاغ ويُسجَّل كتحذير، دون إيقاف الحساب.', cls: 'btn--ghost' },
  remove: { label: 'حذف المحتوى', title: 'حذف المحتوى المُبلَّغ عنه؟', text: 'تختفي الفكرة من الصفحة الرئيسية، أو يُحذف التعليق نهائيًا.', cls: 'btn--danger', danger: true },
  suspend: { label: 'إيقاف الحساب', title: 'إيقاف الحساب؟', text: 'لن يستطيع صاحب الحساب الإرسال أو النشر حتى تعيد تفعيله من صفحته.', cls: 'btn--danger', danger: true },
  ban: { label: 'حظر نهائي', title: 'حظر الحساب نهائيًا؟', text: 'يُحظر الحساب ويُسجَّل خروجه من كل الأجهزة.', cls: 'btn--danger', danger: true },
};
const EVENT_LABELS = {
  login_failed: 'دخول فاشل',
  login_success: 'دخول ناجح',
  login_success_google: 'دخول عبر Google',
  login_blocked_ip: 'حظر دخول (الشبكة)',
  login_blocked_ip_account: 'حظر دخول (الشبكة + الحساب)',
  account_locked: 'قفل مؤقت للحساب',
  login_banned: 'محاولة دخول لحساب محظور',
  register: 'حساب جديد',
  register_limited: 'تسجيل مرفوض (حد الشبكة)',
  honeypot: 'روبوت (فخ التسجيل)',
  google_invalid_token: 'رمز Google غير صالح',
  report: 'بلاغ',
  auto_suspended: 'إيقاف تلقائي',
  block: 'حظر بين مستخدمين',
  block_commenter: 'حظر صاحب تعليق',
  admin_auth_failed: 'رمز مشرف خاطئ',
  admin_set_active: 'المشرف: تفعيل حساب',
  admin_set_suspended: 'المشرف: إيقاف حساب',
  admin_set_banned: 'المشرف: حظر حساب',
  admin_view_messages: 'المشرف: اطّلع على محادثات',
  admin_login: 'دخول مشرف',
  admin_login_failed: 'دخول مشرف فاشل',
};
const RISKY = new Set(['admin_login_failed', 'login_blocked_ip', 'login_blocked_ip_account', 'account_locked', 'login_banned', 'register_limited',
  'honeypot', 'google_invalid_token', 'auto_suspended', 'admin_auth_failed']);
const METRICS = [
  ['users', 'مستخدمون جدد'],
  ['posts', 'أفكار منشورة'],
  ['conversations', 'محادثات جديدة'],
  ['failed_logins', 'محاولات دخول فاشلة'],
];
const TABS = [
  ['overview', 'نظرة عامة'],
  ['users', 'المستخدمون'],
  ['content', 'المحتوى'],
  ['engage', 'التفاعل'],
  ['reports', 'البلاغات'],
  ['media', 'الإشراف على الوسائط'],
  ['verify', 'التوثيق والدفع'],
  ['money', 'الأرباح'],
  ['support', 'الدعم'],
  ['settings', 'الإعدادات'],
  ['system', 'الأمان والنظام'],
  ['logs', 'السجل'],
];

const state = {
  tab: 'overview', stats: null, activity: null, days: 14, active: null, showTable: false,
  reportKind: 'reports', logKind: 'events', reportStatus: 'open', eventType: '', timer: null, updatedAt: null,
};

// ------------------------------------------------------------------ sign in / out

function renderLogin(message = '') {
  stopRefresh();
  const field = (id, label, attrs) => h('div', { class: 'field' }, h('label', { for: id, text: label }),
    h('input', { class: 'input', id, dir: 'ltr', spellcheck: 'false', autocapitalize: 'off', required: true, ...attrs }));
  const user = field('admin-user', 'اسم المشرف', { autocomplete: 'username', maxlength: '64' });
  const pass = field('admin-pass', 'كلمة المرور', { type: 'password', autocomplete: 'current-password', maxlength: '256' });
  const code = field('admin-code', 'رمز التحقق (تطبيق المصادقة)', {
    inputmode: 'numeric', autocomplete: 'one-time-code', pattern: '[0-9]{6}', maxlength: '6', placeholder: '123456',
  });
  const error = h('p', { class: 'form-error', role: 'alert', text: message, hidden: !message });
  const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, icon('lock'), 'دخول');
  const val = (f) => f.querySelector('input').value.trim();
  const form = h('form', {
    class: 'admin-login__card glass',
    onsubmit: async (e) => {
      e.preventDefault();
      submit.disabled = true;
      error.hidden = true;
      try {
        await call('POST', '/api/admin/login', { username: val(user), password: pass.querySelector('input').value, code: val(code) });
        renderShell(await call('GET', '/api/admin/stats'));
      } catch (err) {
        error.textContent = err.status === 401 ? 'بيانات الدخول أو رمز التحقق غير صحيحة.'
          : err.status === 429 ? `محاولات خاطئة كثيرة. حاول بعد ${waitText(err.retryAfter)}.`
            : err.message;
        error.hidden = false;
        submit.disabled = false;
        code.querySelector('input').value = '';
        code.querySelector('input').focus();
      }
    },
  }, user, pass, code, error, submit);

  app.replaceChildren(h('main', { class: 'admin-login' },
    wordmark(true),
    h('h1', { class: 'admin-login__title', text: 'لوحة الإدارة' }),
    h('p', { class: 'admin-login__lead', text: 'للمشرفين فقط. الدخول بحساب مشرف ورمز من تطبيق المصادقة.' }),
    form,
    h('div', { class: 'admin-hint glass' },
      h('p', { text: 'لإنشاء حساب مشرف (مرة واحدة)، شغّل في الخادم:' }),
      h('code', { dir: 'ltr', text: 'cd /opt/dzplay/dzplay && docker compose exec app python -m app.admin_cli create-admin owner' }),
      h('p', { class: 'admin-hint__note', text: 'يطلب كلمة مرور ثم يعرض رمز QR تمسحه بتطبيق Google Authenticator أو Microsoft Authenticator.' }))));
  user.querySelector('input').focus();
}

async function signOut(message = '') {
  try { await call('POST', '/api/admin/logout'); } catch { /* already signed out */ }
  state.stats = null;
  state.activity = null;
  renderLogin(message);
}

// ------------------------------------------------------------------ shell + tabs

function renderShell(stats) {
  state.stats = stats;
  state.updatedAt = new Date();
  const tabs = h('nav', { class: 'admin-tabs glass glass--blur', role: 'tablist', 'aria-label': 'أقسام لوحة التحكم' },
    ...TABS.map(([id, label]) => h('button', {
      type: 'button', role: 'tab', class: 'admin-tab', id: `tab-${id}`, 'aria-selected': String(state.tab === id),
      onclick: () => showTab(id),
    }, label, id === 'reports' ? h('span', { class: 'badge', id: 'reports-badge', hidden: true }) : null)));

  app.replaceChildren(h('div', { class: 'admin-shell' },
    h('header', { class: 'admin-top' },
      h('div', { class: 'admin-top__brand' }, wordmark(), h('span', { class: 'admin-top__title', text: 'لوحة التحكم' })),
      h('div', { class: 'admin-top__actions' },
        iconButton('refresh', 'تحديث', () => refresh(true)),
        iconButton('logout', 'خروج', () => signOut()))),
    tabs,
    h('main', { class: 'admin-main', id: 'admin-main', role: 'tabpanel' })));
  paintBadge();
  showTab(state.tab);
  startRefresh();
}

function paintBadge() {
  const badge = document.getElementById('reports-badge');
  if (!badge || !state.stats) return;
  const open = state.stats.safety.reports_open + (state.stats.safety.flags_open || 0);
  badge.textContent = fmt(open);
  badge.hidden = !open;
}

function showTab(id) {
  state.tab = id;
  document.querySelectorAll('.admin-tab').forEach((b) => b.setAttribute('aria-selected', String(b.id === `tab-${id}`)));
  const main = document.getElementById('admin-main');
  main.setAttribute('aria-labelledby', `tab-${id}`);
  const tab = document.getElementById(`tab-${id}`);
  if (tab) tab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  ({
    overview: renderOverview, users: renderUsers, content: renderContent, engage: renderEngage, reports: renderReports,
    system: renderSystem, logs: renderSecurity,
    media: renderMedia, verify: renderVerify, money: renderMoney, support: renderSupport, settings: renderSettings,
  })[id](main);
  window.scrollTo({ top: 0 });
}

function startRefresh() {
  stopRefresh();
  state.timer = setInterval(() => { if (document.visibilityState === 'visible') refresh(false); }, REFRESH_MS);
}
function stopRefresh() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
}
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state.timer && state.updatedAt && Date.now() - state.updatedAt > REFRESH_MS) refresh(false);
});

async function refresh(manual) {
  try {
    state.stats = await call('GET', '/api/admin/stats');
    state.updatedAt = new Date();
    paintBadge();
    if (state.tab === 'overview') {
      paintStats();
      await loadActivity();
    } else if (manual) {
      showTab(state.tab);
    }
    if (manual) toast('تم التحديث.');
  } catch (err) {
    if (manual || err.status === 401) handleError(err);
  }
}

// ------------------------------------------------------------------ overview

function renderOverview(main) {
  main.replaceChildren(
    h('p', { class: 'admin-meta', id: 'updated' }),
    h('section', { class: 'admin-hero', id: 'hero', 'aria-label': 'أهم الأرقام' }),
    h('section', { class: 'admin-section', 'aria-labelledby': 'activity-title' },
      h('div', { class: 'admin-section__head' },
        h('h2', { class: 'admin-section__title', id: 'activity-title', text: 'النشاط اليومي' }),
        segmented([[7, '7 أيام'], [14, '14 يومًا'], [30, '30 يومًا']], state.days, (d) => { state.days = d; state.active = null; loadActivity(); }, 'المدة')),
      h('div', { id: 'activity' }, spinner())),
    h('section', { class: 'admin-groups', id: 'groups', 'aria-label': 'كل الإحصائيات' }));
  paintStats();
  loadActivity();
}

function tile(label, value, sub, opts = {}) {
  const el = h(opts.onclick ? 'button' : 'div', {
    class: `admin-tile glass ${opts.alert ? 'admin-tile--alert' : ''}`, type: opts.onclick ? 'button' : null, onclick: opts.onclick,
  },
  h('span', { class: 'admin-tile__label' }, opts.icon ? icon(opts.icon) : null, label),
  h('strong', { class: 'admin-tile__num', text: fmt(value) }),
  sub ? h('span', { class: 'admin-tile__sub', text: sub }) : null);
  return el;
}

function group(title, iconName, rows) {
  return h('article', { class: 'admin-group glass' },
    h('h3', { class: 'admin-group__title' }, icon(iconName), title),
    h('dl', { class: 'admin-group__rows' },
      ...rows.map(([label, value]) => h('div', { class: 'admin-row' }, h('dt', { text: label }), h('dd', { text: fmt(value) })))));
}

function paintStats() {
  const s = state.stats;
  const hero = document.getElementById('hero');
  if (!s || !hero) return;
  const updated = document.getElementById('updated');
  updated.textContent = `آخر تحديث ${clockFmt.format(state.updatedAt)} · يتحدث تلقائيًا كل 30 ثانية`;
  hero.replaceChildren(
    tile('المستخدمون', s.users.total, `${fmt(s.users.new_24h)} جدد خلال 24 ساعة`, { icon: 'user' }),
    tile('نشطون الآن', s.users.active_24h, 'خلال آخر 24 ساعة', { icon: 'spark' }),
    tile('رسائل', s.messages.sent_24h, 'أُرسلت خلال 24 ساعة', { icon: 'chat' }),
    tile('تحتاج مراجعة', s.safety.reports_open + s.safety.flags_open,
      s.safety.reports_open + s.safety.flags_open
        ? `${fmt(s.safety.reports_open)} بلاغ · ${fmt(s.safety.flags_open)} رصد تلقائي`
        : 'لا شيء ينتظر',
      { icon: 'flag', alert: s.safety.reports_open + s.safety.flags_open > 0,
        onclick: () => { state.reportKind = s.safety.flags_open && !s.safety.reports_open ? 'flags' : 'reports'; showTab('reports'); } }),
  );
  document.getElementById('groups').replaceChildren(...[
    group('المستخدمون', 'user', [
      ['إجمالي الحسابات', s.users.total], ['نشطون خلال 24 ساعة', s.users.active_24h], ['جدد خلال 24 ساعة', s.users.new_24h],
      ['سجّلوا عبر Google', s.users.google], ['موقوفون', s.users.suspended], ['محظورون', s.users.banned],
      ['جلسات دخول نشطة', s.sessions_active],
    ]),
    group('الرسائل والمحادثات', 'bubbles', [
      ['رسائل خلال 24 ساعة', s.messages.sent_24h], ['رسائل مخزنة الآن', s.messages.stored_now],
      ['كل الرسائل منذ البداية', s.messages.total_sent_all_time], ['محادثات نشطة', s.conversations.active],
      ['محادثات مغلقة', s.conversations.closed], ['محادثات جديدة خلال 24 ساعة', s.conversations.created_24h],
    ]),
    group('الأفكار', 'bulb', [
      ['أفكار ظاهرة', s.ideas.posts], ['أفكار جديدة خلال 24 ساعة', s.ideas.posts_24h], ['أفكار محذوفة', s.ideas.removed],
      ['تعليقات', s.ideas.comments], ['إعجابات وعدم إعجاب', s.ideas.reactions],
    ]),
    group('الأمان', 'shield', [
      ['بلاغات مفتوحة', s.safety.reports_open], ['رسائل مرصودة تلقائيًا (مفتوحة)', s.safety.flags_open], ['دخول فاشل خلال 24 ساعة', s.safety.failed_logins_24h],
      ['حظر دخول نشط الآن', s.safety.active_login_blocks], ['تسجيلات مرفوضة خلال 24 ساعة', s.safety.registrations_limited_24h],
      ['حظر بين المستخدمين', s.safety.blocks], ['حظر شبكات نشط', s.ip_blocks_active],
    ]),
    s.reels ? group('Reels', 'reels', [
      ['ظاهر', s.reels.visible], ['مخفي', s.reels.hidden], ['قيد التجهيز', s.reels.processing], ['فشل التجهيز', s.reels.failed],
      ['مشاهدات خلال 24 ساعة', s.reels.views_24h], ['تفاعلات خلال 24 ساعة', s.reels.reactions_24h],
      ['تعليقات خلال 24 ساعة', s.reels.comments_24h], ['كل التعليقات', s.reel_comments],
    ]) : null,
    s.password_resets ? group('استعادة كلمة المرور', 'lock', [
      ['طلبات خلال 24 ساعة', s.password_resets.requests_24h], ['بانتظار المشرف', s.password_resets.waiting_admin],
      ['اكتملت خلال 24 ساعة', s.password_resets.completed_24h],
    ]) : null,
  ].filter(Boolean)); // native replaceChildren() would print "null"
  if (s.media_cache_bytes != null) {
    document.getElementById('groups').append(h('article', { class: 'admin-group glass' },
      h('h3', { class: 'admin-group__title' }, icon('reels'), 'ذاكرة الوسائط'),
      h('dl', { class: 'admin-group__rows' }, h('div', { class: 'admin-row' }, h('dt', { text: 'الحجم الحالي' }), h('dd', { text: bytes(s.media_cache_bytes) })))));
  }
}

async function loadActivity() {
  if (!document.getElementById('activity')) return;
  try {
    state.activity = await call('GET', `/api/admin/activity?days=${state.days}&tz=${new Date().getTimezoneOffset()}`);
    paintActivity();
  } catch (err) {
    handleError(err);
  }
}

function paintActivity() {
  const box = document.getElementById('activity');
  if (!box || !state.activity) return;
  const { days, series } = state.activity;
  const readout = h('div', { class: 'viz-readout', 'aria-live': 'polite' });
  const charts = METRICS.map(([key, label]) => miniChart(label, days, series[key]));
  const table = activityTable(days, series);
  table.hidden = !state.showTable;
  const toggle = h('button', {
    type: 'button', class: 'btn btn--ghost btn--sm', 'aria-pressed': String(state.showTable),
    onclick: () => {
      state.showTable = !state.showTable;
      table.hidden = !state.showTable;
      toggle.setAttribute('aria-pressed', String(state.showTable));
      toggle.textContent = state.showTable ? 'إخفاء الجدول' : 'عرض كجدول';
    },
  }, state.showTable ? 'إخفاء الجدول' : 'عرض كجدول');

  const setActive = (i) => {
    state.active = i;
    charts.forEach((c) => c.mark(i));
    paintReadout(readout, i);
  };
  charts.forEach((c) => { c.onActive = setActive; });
  box.replaceChildren(
    readout,
    h('div', { class: 'viz-grid' }, ...charts.map((c) => c.el)),
    h('div', { class: 'viz-foot' },
      h('p', { class: 'viz-note', text: 'الأيام حسب توقيت جهازك. المحادثات التي حُذفت تلقائيًا بعد انتهاء مدتها لا تُحتسب.' }),
      toggle),
    table);
  setActive(state.active != null && state.active < days.length ? state.active : null);
}

function paintReadout(box, i) {
  const { days, series } = state.activity;
  if (i == null) {
    box.replaceChildren(h('span', { class: 'viz-readout__hint', text: 'المس أي رسم أو مرّر عليه لعرض أرقام يوم محدد.' }));
    return;
  }
  box.replaceChildren(
    h('strong', { class: 'viz-readout__day', text: longDay.format(parseDay(days[i])) }),
    h('span', { class: 'viz-readout__items' },
      ...METRICS.map(([key, label]) => h('span', { class: 'viz-readout__item' },
        h('b', { text: fmt(series[key][i]) }), h('span', { text: label })))));
}

function activityTable(days, series) {
  return h('div', { class: 'viz-table glass' },
    h('table', {},
      h('caption', { class: 'sr-only', text: 'النشاط اليومي' }),
      h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'اليوم' }), ...METRICS.map(([, label]) => h('th', { scope: 'col', text: label })))),
      h('tbody', {}, ...days.map((d, i) => i).reverse().map((i) => h('tr', {},
        h('th', { scope: 'row', text: shortDay.format(parseDay(days[i])) }),
        ...METRICS.map(([key]) => h('td', { text: fmt(series[key][i]) })))))));
}

// --- one small-multiple line chart (inline SVG, single series, shared X) ---

const SVG_NS = 'http://www.w3.org/2000/svg';
function svg(tag, attrs = {}) {
  const el = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, String(v));
  return el;
}
function svgText(content, x, y, anchor, cls) {
  const t = svg('text', { x, y, 'text-anchor': anchor, class: cls });
  t.textContent = content;
  return t;
}
function niceMax(v) {
  if (v <= 4) return 4;
  const pow = 10 ** Math.floor(Math.log10(v));
  for (const m of [1, 2, 4, 6, 8, 10]) if (m * pow >= v && (m * pow) % 2 === 0) return m * pow;
  return 10 * pow;
}

function miniChart(label, days, values) {
  const n = values.length;
  const total = values.reduce((a, b) => a + b, 0);
  const plot = h('div', {
    class: 'viz-plot', dir: 'ltr', tabindex: '0', role: 'img',
    'aria-label': `${label}: ${fmt(total)} خلال ${fmt(n)} يومًا، آخر يوم ${fmt(values[n - 1])}. استخدم الأسهم لتصفح الأيام.`,
  });
  const el = h('figure', { class: 'viz-card glass' },
    h('figcaption', { class: 'viz-card__head' },
      h('span', { class: 'viz-card__title', text: label }),
      h('span', { class: 'viz-card__total' }, h('b', { text: fmt(total) }), ` خلال ${fmt(n)} يومًا`)),
    plot);
  const chart = { el, onActive: null, mark: () => {} };
  let geom = null;
  let cross = null;
  let dot = null;

  const draw = () => {
    const W = Math.round(plot.clientWidth);
    if (!W) return;
    const H = 116;
    const pad = { l: 28, r: 30, t: 14, b: 24 };
    const max = niceMax(Math.max(...values));
    const step = n > 1 ? (W - pad.l - pad.r) / (n - 1) : 0;
    const x = (i) => pad.l + i * step;
    const y = (v) => pad.t + (H - pad.t - pad.b) * (1 - v / max);
    const root = svg('svg', { width: W, height: H, viewBox: `0 0 ${W} ${H}`, 'aria-hidden': 'true', focusable: 'false' });
    for (const t of [0, max / 2, max]) {
      root.append(svg('line', { class: t === 0 ? 'viz-axis' : 'viz-gridline', x1: pad.l, x2: W - pad.r, y1: y(t), y2: y(t) }));
      root.append(svgText(fmt(t), pad.l - 7, y(t) + 4, 'end', 'viz-tick'));
    }
    const ticks = n > 2 ? [0, Math.floor((n - 1) / 2), n - 1] : [0, n - 1];
    ticks.forEach((i, k) => root.append(svgText(shortDay.format(parseDay(days[i])), x(i), H - 6,
      k === 0 ? 'start' : k === ticks.length - 1 ? 'end' : 'middle', 'viz-tick')));
    const pts = values.map((v, i) => [x(i), y(v)]);
    const line = pts.map((p, i) => `${i ? 'L' : 'M'}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(' ');
    root.append(svg('path', { class: 'viz-area', d: `${line} L${x(n - 1).toFixed(1)} ${y(0)} L${x(0).toFixed(1)} ${y(0)} Z` }));
    cross = svg('line', { class: 'viz-cross', y1: pad.t - 6, y2: H - pad.b, visibility: 'hidden' });
    root.append(cross);
    root.append(svg('path', { class: 'viz-line', d: line }));
    const [lx, ly] = pts[n - 1];
    root.append(svg('circle', { class: 'viz-dot', cx: lx, cy: ly, r: 4 }));
    root.append(svgText(fmt(values[n - 1]), lx + 8, ly + 4, 'start', 'viz-end'));
    dot = svg('circle', { class: 'viz-dot', r: 4.5, visibility: 'hidden' });
    root.append(dot);
    geom = { x, y, pad, step, W };
    plot.replaceChildren(root);
    chart.mark(state.active);
  };

  chart.mark = (i) => {
    if (!geom || !cross) return;
    if (i == null) {
      cross.setAttribute('visibility', 'hidden');
      dot.setAttribute('visibility', 'hidden');
      return;
    }
    const cx = geom.x(i);
    cross.setAttribute('x1', cx);
    cross.setAttribute('x2', cx);
    dot.setAttribute('cx', cx);
    dot.setAttribute('cy', geom.y(values[i]));
    cross.setAttribute('visibility', 'visible');
    dot.setAttribute('visibility', 'visible');
  };

  const indexAt = (clientX) => {
    const rect = plot.getBoundingClientRect();
    if (!geom || !geom.step) return n - 1;
    return Math.max(0, Math.min(n - 1, Math.round((clientX - rect.left - geom.pad.l) / geom.step)));
  };
  const activate = (i) => chart.onActive && chart.onActive(i);
  plot.addEventListener('pointermove', (e) => activate(indexAt(e.clientX)));
  plot.addEventListener('pointerdown', (e) => activate(indexAt(e.clientX)));
  plot.addEventListener('pointerleave', (e) => { if (e.pointerType === 'mouse') activate(null); });
  plot.addEventListener('focus', () => { if (state.active == null) activate(n - 1); });
  plot.addEventListener('keydown', (e) => {
    const cur = state.active ?? n - 1;
    const next = { ArrowLeft: cur - 1, ArrowRight: cur + 1, Home: 0, End: n - 1 }[e.key];
    if (e.key === 'Escape') { activate(null); return; }
    if (next === undefined) return;
    e.preventDefault();
    activate(Math.max(0, Math.min(n - 1, next)));
  });
  new ResizeObserver(draw).observe(plot);
  return chart;
}

// ------------------------------------------------------------------ reports + automatic flags

const CATEGORY_LABELS = {
  threat: 'تهديد', blackmail: 'ابتزاز', sexual: 'تحرش جنسي', insult: 'سب وشتم', contact: 'أرقام أو حسابات', custom: 'كلمة مضافة',
};
const LIST_STATUS = [['open', 'مفتوحة'], ['resolved', 'تم حلها'], ['dismissed', 'تم تجاهلها']];

function renderReports(main) {
  const list = h('div', { class: 'admin-list', id: 'report-list' });
  const note = h('p', { class: 'admin-meta' });
  const paintNote = () => {
    note.textContent = state.reportKind === 'flags'
      ? 'رسائل وتعليقات رصدها النظام تلقائيًا (تهديد، ابتزاز، تحرش، سب، مشاركة أرقام). الرسالة وصلت لصاحبها والمرسل لا يعلم. يُحفظ نصها هنا للمراجعة.'
      : 'بلاغات قدّمها المستخدمون بأنفسهم، مع نسخة محفوظة من المحتوى المُبلَّغ عنه. لا تظهر عناوين البريد أبدًا.';
  };
  paintNote();
  main.replaceChildren(
    sectionHead('البلاغات والرصد'),
    segmented([['reports', 'بلاغات المستخدمين'], ['flags', 'رصد تلقائي']], state.reportKind,
      (v) => { state.reportKind = v; paintNote(); loadReports(list); }, 'نوع المراجعة'),
    segmented(LIST_STATUS, state.reportStatus, (v) => { state.reportStatus = v; loadReports(list); }, 'الحالة'),
    note,
    list);
  loadReports(list);
}

async function loadReports(list) {
  list.replaceChildren(spinner());
  const kind = state.reportKind;
  try {
    const data = await call('GET', `/api/admin/${kind}?status=${state.reportStatus}&limit=100`);
    if (kind !== state.reportKind) return; // switched while loading
    paintReports(list, kind === 'flags' ? data.flags : data.reports);
  } catch (err) {
    list.replaceChildren();
    handleError(err);
  }
}

function paintReports(list, items) {
  if (!items.length) {
    const open = state.reportStatus === 'open';
    list.replaceChildren(emptyState(!open ? 'لا شيء هنا بعد.'
      : state.reportKind === 'flags' ? 'لم يُرصد أي محتوى مقلق. كل شيء هادئ.' : 'لا توجد بلاغات مفتوحة. كل شيء هادئ.'));
    return;
  }
  list.replaceChildren(...items.map((it) => (state.reportKind === 'flags' ? flagCard(it, list) : reportCard(it, list))));
}

function actionButtons(item, allowed, card, list) {
  if (item.status !== 'open') return h('p', { class: 'admin-resolution' }, icon('check'), RESOLUTIONS[item.resolution] || item.resolution || '');
  return h('div', { class: 'admin-actions' }, ...allowed.map((a) => h('button', {
    type: 'button', class: `btn btn--sm ${ACTIONS[a].cls}`, onclick: () => resolveItem(item, a, card, list),
  }, ACTIONS[a].label)));
}

function userLinks(ref) {
  if (!ref) return null;
  return h('span', { class: 'admin-card__links' },
    h('button', { type: 'button', class: 'admin-link', onclick: () => openUser(ref) }, icon('user'), 'صفحة المستخدم والمحادثات'));
}

function reportCard(r, list) {
  const card = h('article', { class: 'admin-card glass' });
  const evidence = (r.evidence || []).filter((e) => e && e.content);
  const allowed = ['dismiss', 'warn', ...(r.target === 'post' || r.target === 'comment' ? ['remove'] : []), 'suspend', 'ban'];
  card.append(...[
    h('div', { class: 'admin-card__head' },
      h('span', { class: 'chip chip--hot', text: REASONS[r.reason] || r.reason }),
      h('span', { class: 'admin-card__target', text: TARGETS[r.target] || r.target }),
      h('time', { class: 'admin-card__time', datetime: r.created_at, text: when(r.created_at) })),
    h('div', { class: 'admin-card__meta' },
      h('span', {}, 'المستخدم المُبلَّغ عنه: ', userRef(r.reported_user_ref)),
      h('span', {}, 'بلاغات ضده: ', h('b', { text: fmt(r.reported_user_reports_total) })),
      h('span', {}, 'رصد تلقائي: ', h('b', { text: fmt(r.reported_user_flags_total) })),
      userLinks(r.reported_user_ref)),
    r.details ? h('div', { class: 'admin-quote' }, h('span', { class: 'admin-quote__label', text: 'ملاحظة المُبلِّغ' }), h('p', { text: r.details })) : null,
    evidence.length ? h('div', { class: 'admin-evidence' },
      h('span', { class: 'admin-quote__label', text: 'الدليل (نسخة محفوظة وقت البلاغ)' }),
      ...evidence.map((e) => h('div', { class: 'admin-evidence__item' },
        h('p', { text: e.content }),
        e.created_at ? h('time', { datetime: e.created_at, text: when(e.created_at) }) : null)))
      : h('p', { class: 'admin-meta', text: 'لا يوجد نص محفوظ لهذا البلاغ.' }),
    actionButtons(r, allowed, card, list),
  ].filter(Boolean)); // native append() would print "null"
  return card;
}

function flagCard(f, list) {
  const card = h('article', { class: 'admin-card glass' });
  card.append(...[
    h('div', { class: 'admin-card__head' },
      ...f.categories.map((c) => h('span', { class: 'chip chip--hot', text: CATEGORY_LABELS[c] || c })),
      h('span', { class: 'admin-card__target', text: f.target === 'comment' ? 'تعليق خاص' : 'رسالة' }),
      h('time', { class: 'admin-card__time', datetime: f.created_at, text: when(f.created_at) })),
    h('div', { class: 'admin-card__meta' },
      h('span', {}, 'المرسل: ', userRef(f.offender_ref)),
      h('span', {}, 'رصد تلقائي: ', h('b', { text: fmt(f.offender_flags_total) })),
      h('span', {}, 'بلاغات: ', h('b', { text: fmt(f.offender_reports_total) })),
      userLinks(f.offender_ref)),
    h('div', { class: 'admin-evidence' },
      h('span', { class: 'admin-quote__label', text: 'النص (نسخة محفوظة)' }),
      h('div', { class: 'admin-evidence__item' }, h('p', { text: f.content }))),
    f.terms && f.terms.length ? h('p', { class: 'admin-terms' }, 'الكلمات المرصودة: ',
      ...f.terms.map((t) => h('mark', { text: t }))) : null,
    actionButtons(f, ['dismiss', 'warn', 'remove', 'suspend', 'ban'], card, list),
  ].filter(Boolean));
  card.dataset.kind = 'flag';
  return card;
}

async function resolveItem(item, action, card, list) {
  const a = ACTIONS[action];
  const isFlag = card.dataset.kind === 'flag';
  const text = isFlag && action === 'remove' ? 'تُحذف الرسالة أو التعليق نهائيًا من الخادم.' : a.text;
  if (!(await confirmSheet({ title: a.title, text, confirm: a.label, danger: !!a.danger }))) return;
  try {
    await call('POST', `/api/admin/${isFlag ? 'flags' : 'reports'}/${encodeURIComponent(item.id)}/resolve`, { action });
    toast(`تم: ${RESOLUTIONS[action]}.`);
    card.remove();
    if (!list.children.length) paintReports(list, []);
    refreshStatsQuietly();
  } catch (err) {
    handleError(err);
  }
}

async function refreshStatsQuietly() {
  try {
    state.stats = await call('GET', '/api/admin/stats');
    state.updatedAt = new Date();
    paintBadge();
  } catch { /* the next refresh will retry */ }
}

// ------------------------------------------------------------------ security log

const AUDIT_LABELS = {
  login: 'دخول مشرف', logout: 'خروج مشرف', view_reports: 'اطّلع على البلاغات', view_flags: 'اطّلع على الرصد',
  view_conversations: 'اطّلع على محادثات', cleanup: 'تنظيف البيانات', user_active: 'تفعيل حساب',
  user_suspended: 'إيقاف حساب', user_banned: 'حظر حساب', create_admin: 'إنشاء مشرف (CLI)',
  reset_admin_2fa: 'إعادة 2FA (CLI)', set_admin_password: 'تغيير كلمة مرور مشرف (CLI)',
  view_users: 'بحث في المستخدمين', view_user: 'فتح صفحة مستخدم', user_revoke_sessions: 'إنهاء جلسات مستخدم', user_delete: 'حذف حساب',
  view_ideas: 'تصفح الأفكار', view_idea_comments: 'اطّلع على تعليقات فكرة', view_conversation_list: 'تصفح المحادثات',
  view_conversation: 'اطّلع على محادثة', search_content: 'بحث في المحتوى', delete_idea: 'حذف فكرة', delete_idea_comment: 'حذف تعليق فكرة',
  delete_reel_comment: 'حذف تعليق Reel', delete_message: 'حذف رسالة', delete_conversation: 'حذف محادثة',
  engagement_boost: 'تعزيز تفاعل', engagement_comments: 'تعليقات الفريق', engagement_cancel: 'إلغاء عملية تفاعل',
  library_add: 'مكتبة: إضافة', library_edit: 'مكتبة: تعديل', library_delete: 'مكتبة: حذف', library_import: 'مكتبة: استيراد',
  category_add: 'تصنيف: إضافة', category_edit: 'تصنيف: تعديل', category_delete: 'تصنيف: حذف', official_comment: 'تعليق رسمي',
  ip_unblock: 'رفع حظر شبكة', reel_show: 'إظهار Reel', reel_hide: 'إخفاء Reel', reel_pin: 'تثبيت Reel', reel_unpin: 'إلغاء تثبيت Reel',
  reel_caption: 'تعديل وصف Reel', reel_delete: 'حذف Reel', login_failed: 'دخول مشرف فاشل',
  // V5
  media_ok: 'وسائط: قبول', media_no: 'وسائط: رفض', media_del: 'وسائط: حذف', media_ban: 'وسائط: حذف + حظر',
  media_keep: 'وسائط: إبقاء', media_minor: 'وسائط: قاصر (حذف + حظر + دليل)',
  verify_accept: 'توثيق: قبول', verify_reject: 'توثيق: رفض', verify_fix: 'توثيق: طلب تصحيح',
  verify_grant: 'منح النجمة', verify_revoke: 'سحب النجمة', payment_settings: 'إعدادات الدفع',
  support_reply: 'رد على تذكرة', support_open: 'فتح تذكرة', support_closed: 'إغلاق تذكرة', support_answered: 'تذكرة: تم الرد',
  settings_change: 'تغيير الإعدادات', monetize_accept: 'تحقيق الدخل: قبول', monetize_reject: 'تحقيق الدخل: رفض',
  monetize_fix: 'تحقيق الدخل: تصحيح', ledger_earning: 'أرباح: إضافة', ledger_payout: 'أرباح: دفعة الظرف الأحمر',
  ledger_adjustment: 'أرباح: تسوية', ledger_reversal: 'أرباح: إلغاء قيد', account_self_delete: 'حذف حساب بطلب صاحبه',
};
const auditLabel = (a) => AUDIT_LABELS[a] || ({ report_: 'بلاغ: ', flag_: 'رصد: ' }[a.replace(/[a-z]+$/, '')] || '') + (RESOLUTIONS[a.split('_').pop()] || a);

function renderSecurity(main) {
  const body = h('div', { class: 'admin-section' });
  const showEvents = () => {
    const select = h('select', { class: 'input admin-select', id: 'event-type', 'aria-label': 'نوع الحدث' },
      h('option', { value: '', text: 'كل الأحداث' }),
      ...Object.entries(EVENT_LABELS).map(([v, label]) => h('option', { value: v, text: label })));
    select.value = state.eventType;
    const list = h('div', { class: 'admin-events glass', id: 'event-list' });
    select.addEventListener('change', () => { state.eventType = select.value; loadEvents(list); });
    body.replaceChildren(
      h('div', { class: 'admin-filter' }, select),
      h('p', { class: 'admin-meta', text: 'المستخدمون بمرجع داخلي فقط، والشبكات ببصمة مشفّرة مختصرة — لا عناوين IP ولا بريد. تُحذف السجلات تلقائيًا بعد 30 يومًا.' }),
      list);
    loadEvents(list);
  };
  const showAudit = async () => {
    const list = h('div', { class: 'admin-events glass', id: 'audit-list' }, spinner());
    const chain = h('p', { class: 'admin-meta' });
    body.replaceChildren(
      h('p', { class: 'admin-meta', text: 'كل ما يفعله المشرفون، وكل اطلاع على محتوى خاص، مع السبب. السجل لا يمكن تعديله أو حذفه من اللوحة، وكل سطر مربوط بالسطر السابق لكشف أي تلاعب.' }),
      chain, list);
    try {
      const data = await call('GET', '/api/admin/audit?limit=200');
      chain.replaceChildren(icon(data.chain.ok ? 'check' : 'flag'),
        data.chain.ok ? ` السجل سليم (${fmt(data.chain.checked)} سطرًا).` : ` تحذير: السجل عُدّل خارج اللوحة عند السطر ${data.chain.broken_at}.`);
      chain.className = `admin-meta admin-chain ${data.chain.ok ? '' : 'admin-chain--bad'}`;
      list.replaceChildren(...(data.entries.length ? data.entries.map((e) => h('div', { class: 'admin-event' },
        h('div', { class: 'admin-event__top' },
          h('span', { class: 'admin-event__type', text: auditLabel(e.action) }),
          h('time', { class: 'admin-event__time', datetime: e.at, text: when(e.at) })),
        h('div', { class: 'admin-event__meta' },
          h('span', {}, 'المشرف: ', h('b', { text: e.actor })),
          e.target_id ? h('span', {}, 'الهدف: ', h('code', { dir: 'ltr', text: `${e.target_type || ''} ${shortRef(e.target_id)}` })) : null,
          e.reason ? h('span', {}, 'السبب: ', h('b', { text: e.reason })) : null,
          e.detail ? h('span', { class: 'admin-event__detail', text: e.detail }) : null)))
        : [h('p', { class: 'admin-events__empty', text: 'لا شيء بعد.' })]));
    } catch (err) {
      list.replaceChildren();
      handleError(err);
    }
  };
  main.replaceChildren(
    sectionHead('السجلات'),
    segmented([['events', 'أحداث الأمان'], ['audit', 'سجل الإدارة']], state.logKind,
      (v) => { state.logKind = v; (v === 'audit' ? showAudit : showEvents)(); }, 'نوع السجل'),
    body);
  (state.logKind === 'audit' ? showAudit : showEvents)();
}

async function loadEvents(list) {
  list.replaceChildren(spinner());
  try {
    const q = state.eventType ? `&type=${encodeURIComponent(state.eventType)}` : '';
    const { events } = await call('GET', `/api/admin/security-events?limit=200${q}`);
    if (!events.length) {
      list.replaceChildren(h('p', { class: 'admin-events__empty', text: 'لا توجد أحداث.' }));
      return;
    }
    list.replaceChildren(...events.map((e) => h('div', { class: `admin-event ${RISKY.has(e.type) ? 'admin-event--risk' : ''}` },
      h('div', { class: 'admin-event__top' },
        h('span', { class: 'admin-event__type' }, RISKY.has(e.type) ? icon('shield') : null, EVENT_LABELS[e.type] || e.type),
        h('time', { class: 'admin-event__time', datetime: e.at, text: when(e.at) })),
      h('div', { class: 'admin-event__meta' },
        e.user_ref ? h('span', {}, 'مستخدم: ', userRef(e.user_ref)) : null,
        e.ip_ref ? h('span', {}, 'شبكة: ', h('code', { dir: 'ltr', text: e.ip_ref })) : null,
        e.detail ? h('span', { class: 'admin-event__detail', text: e.detail }) : null))));
  } catch (err) {
    list.replaceChildren();
    handleError(err);
  }
}

// ------------------------------------------------------------------ boot

hooks.onUnauthorized = (message) => signOut(message);
hooks.openUser = openUser;
hooks.openIdea = openIdea;
hooks.openEngage = openEngage;
hooks.showTab = showTab;
hooks.refreshStats = () => refreshStatsQuietly();

async function boot() {
  try {
    renderShell(await call('GET', '/api/admin/stats'));
  } catch (err) {
    renderLogin(err.status === 401 || err.status === 0 ? '' : err.message);
  }
}

boot();
