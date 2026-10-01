// DZPLAY owner dashboard. Talks only to the token-protected /api/admin API.
// The admin token is kept in sessionStorage (cleared when the tab closes) and
// sent as a Bearer header. All server data is inserted with textContent.
import { h, toast, confirmSheet, wordmark, REPORT_REASONS } from './ui.js';
import { icon } from './icons.js';

const TOKEN_KEY = 'dz-admin-token';
const REFRESH_MS = 30_000;
const app = document.getElementById('app');

const nf = new Intl.NumberFormat('ar-DZ');
const fmt = (n) => nf.format(n ?? 0);
const shortDay = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'short' });
const longDay = new Intl.DateTimeFormat('ar-DZ', { weekday: 'long', day: 'numeric', month: 'long' });
const stamp = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
const clockFmt = new Intl.DateTimeFormat('ar-DZ', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
const when = (iso) => (iso ? stamp.format(new Date(iso)) : '');
const parseDay = (d) => { const [y, m, dd] = d.split('-').map(Number); return new Date(y, m - 1, dd); };
const shortRef = (ref) => (ref ? `${ref.slice(0, 8)}…` : '—');

// ------------------------------------------------------------------ token + API

let memToken = null; // fallback when sessionStorage is unavailable
function getToken() {
  try { return sessionStorage.getItem(TOKEN_KEY) || memToken; } catch { return memToken; }
}
function setToken(t) {
  memToken = t || null;
  try { if (t) sessionStorage.setItem(TOKEN_KEY, t); else sessionStorage.removeItem(TOKEN_KEY); } catch { /* private mode */ }
}

class AdminError extends Error {
  constructor(status, code, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

async function call(method, path, body, token = getToken()) {
  const headers = { Accept: 'application/json', Authorization: `Bearer ${token || ''}` };
  const init = { method, headers, cache: 'no-store', credentials: 'omit' };
  if (method !== 'GET') {
    headers['X-DZ-Requested'] = '1';
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body ?? {});
  }
  let res;
  try { res = await fetch(path, init); } catch { throw new AdminError(0, 'network', 'لا يوجد اتصال بالإنترنت.'); }
  let data = null;
  try { data = await res.json(); } catch { /* empty */ }
  if (!res.ok) {
    const e = (data && data.error) || {};
    throw new AdminError(res.status, e.code || 'error', e.message || 'حدث خطأ. حاول مرة أخرى.', e.retry_after ?? null);
  }
  return data;
}

function waitText(seconds) {
  const m = Math.max(1, Math.ceil((seconds || 60) / 60));
  return m === 1 ? 'دقيقة' : `${fmt(m)} دقائق`;
}

function handleError(err) {
  if (err.status === 401) return signOut('انتهت الجلسة أو تغيّر الرمز. أدخل الرمز من جديد.');
  if (err.status === 429) return toast(`محاولات كثيرة. حاول بعد ${waitText(err.retryAfter)}.`, 'error');
  return toast(err.message, 'error');
}

// ------------------------------------------------------------------ labels

const REASONS = Object.fromEntries(REPORT_REASONS);
const TARGETS = { message: 'رسالة', conversation: 'محادثة', post: 'فكرة منشورة', comment: 'تعليق' };
const RESOLUTIONS = { dismiss: 'تم التجاهل', warn: 'تحذير', remove: 'حُذف المحتوى', suspend: 'أُوقف الحساب', ban: 'حُظر الحساب' };
const ACTIONS = {
  dismiss: { label: 'تجاهل', title: 'تجاهل البلاغ؟', text: 'لن يتغير شيء في حساب المستخدم أو المحتوى.', cls: 'btn--ghost' },
  warn: { label: 'تحذير', title: 'تسجيل تحذير؟', text: 'يُغلق البلاغ ويُسجَّل كتحذير، دون إيقاف الحساب.', cls: 'btn--ghost' },
  remove: { label: 'حذف المحتوى', title: 'حذف المحتوى المُبلَّغ عنه؟', text: 'تختفي الفكرة من الصفحة الرئيسية، أو يُحذف التعليق نهائيًا.', cls: 'btn--danger', danger: true },
  suspend: { label: 'إيقاف الحساب', title: 'إيقاف الحساب؟', text: 'لن يستطيع صاحب الحساب الإرسال أو النشر حتى تعيد تفعيله من تبويب الصيانة.', cls: 'btn--danger', danger: true },
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
};
const RISKY = new Set(['login_blocked_ip', 'login_blocked_ip_account', 'account_locked', 'login_banned', 'register_limited',
  'honeypot', 'google_invalid_token', 'auto_suspended', 'admin_auth_failed']);
const CLEANUP_LABELS = {
  messages: 'رسائل منتهية', conversations: 'محادثات منتهية', sessions: 'جلسات منتهية', challenges: 'تحديات مكافحة الروبوت',
  auth_throttle: 'سجلات حظر الدخول', security_events: 'سجلات أمان قديمة', reports: 'بلاغات قديمة مغلقة',
};
const METRICS = [
  ['users', 'مستخدمون جدد'],
  ['posts', 'أفكار منشورة'],
  ['conversations', 'محادثات جديدة'],
  ['failed_logins', 'محاولات دخول فاشلة'],
];
const TABS = [
  ['overview', 'نظرة عامة'],
  ['reports', 'البلاغات'],
  ['security', 'الأمان'],
  ['tools', 'الصيانة'],
];

const state = {
  tab: 'overview', stats: null, activity: null, days: 14, active: null, showTable: false,
  reportStatus: 'open', eventType: '', prefillRef: '', timer: null, updatedAt: null,
};

// ------------------------------------------------------------------ small pieces

function segmented(options, current, onPick, label) {
  const box = h('div', { class: 'segmented admin-seg', role: 'tablist', 'aria-label': label });
  box.style.gridTemplateColumns = `repeat(${options.length}, 1fr)`;
  for (const [value, text] of options) {
    box.append(h('button', {
      type: 'button', role: 'tab', 'aria-selected': String(value === current),
      onclick: () => {
        box.querySelectorAll('button').forEach((b) => b.setAttribute('aria-selected', 'false'));
        box.querySelector(`[data-v="${value}"]`).setAttribute('aria-selected', 'true');
        onPick(value);
      },
      dataset: { v: value },
    }, text));
  }
  return box;
}

function iconButton(name, label, onclick) {
  return h('button', { type: 'button', class: 'icon-btn glass', 'aria-label': label, title: label, onclick }, icon(name));
}

function sectionHead(title, ...extra) {
  return h('div', { class: 'admin-section__head' }, h('h2', { class: 'admin-section__title', text: title }), ...extra);
}

function emptyState(text) {
  return h('div', { class: 'empty glass admin-empty' }, h('p', { text }));
}

function spinner() {
  return h('div', { class: 'admin-loading' }, h('span', { class: 'spinner' }));
}

function userRef(ref) {
  if (!ref) return h('span', { class: 'admin-ref', text: '—' });
  return h('button', {
    type: 'button', class: 'admin-ref', dir: 'ltr', title: 'نسخ مرجع المستخدم',
    onclick: async () => {
      try { await navigator.clipboard.writeText(ref); toast('تم نسخ مرجع المستخدم.'); } catch { toast(ref); }
    },
  }, shortRef(ref));
}

// ------------------------------------------------------------------ sign in / out

function renderLogin(message = '') {
  stopRefresh();
  const input = h('input', {
    class: 'input', id: 'admin-token', type: 'password', dir: 'ltr', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'off', required: true, placeholder: 'ADMIN_API_TOKEN',
  });
  const error = h('p', { class: 'form-error', role: 'alert', text: message, hidden: !message });
  const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, icon('lock'), 'دخول');
  const form = h('form', {
    class: 'admin-login__card glass',
    onsubmit: async (e) => {
      e.preventDefault();
      const token = input.value.trim();
      if (!token) return;
      submit.disabled = true;
      error.hidden = true;
      try {
        const stats = await call('GET', '/api/admin/stats', undefined, token);
        setToken(token);
        renderShell(stats);
      } catch (err) {
        error.textContent = err.status === 401 ? 'الرمز غير صحيح.'
          : err.status === 429 ? `محاولات خاطئة كثيرة من هذه الشبكة. حاول بعد ${waitText(err.retryAfter)}.`
            : err.message;
        error.hidden = false;
        submit.disabled = false;
        input.select();
      }
    },
  },
  h('div', { class: 'field' }, h('label', { for: 'admin-token', text: 'رمز المشرف' }), input),
  error,
  submit);

  app.replaceChildren(h('main', { class: 'admin-login' },
    wordmark(true),
    h('h1', { class: 'admin-login__title', text: 'لوحة التحكم' }),
    h('p', { class: 'admin-login__lead', text: 'للمالك فقط. أدخل رمز المشرف الموجود في ملف ‎.env على الخادم.' }),
    form,
    h('div', { class: 'admin-hint glass' },
      h('p', { text: 'لعرض الرمز، شغّل في الخادم:' }),
      h('code', { dir: 'ltr', text: 'grep ADMIN_API_TOKEN /opt/dzplay/dzplay/.env' }),
      h('p', { class: 'admin-hint__note', text: 'يُحفظ الرمز في هذه النافذة فقط ويُمسح عند إغلاقها أو عند الخروج.' }))));
  input.focus();
}

function signOut(message = '') {
  setToken(null);
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
  const open = state.stats.safety.reports_open;
  badge.textContent = fmt(open);
  badge.hidden = !open;
}

function showTab(id) {
  state.tab = id;
  document.querySelectorAll('.admin-tab').forEach((b) => b.setAttribute('aria-selected', String(b.id === `tab-${id}`)));
  const main = document.getElementById('admin-main');
  main.setAttribute('aria-labelledby', `tab-${id}`);
  ({ overview: renderOverview, reports: renderReports, security: renderSecurity, tools: renderTools })[id](main);
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
    tile('بلاغات مفتوحة', s.safety.reports_open, s.safety.reports_open ? 'تحتاج مراجعة — اضغط للعرض' : 'لا شيء ينتظر',
      { icon: 'flag', alert: s.safety.reports_open > 0, onclick: () => showTab('reports') }),
  );
  document.getElementById('groups').replaceChildren(
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
      ['بلاغات مفتوحة', s.safety.reports_open], ['دخول فاشل خلال 24 ساعة', s.safety.failed_logins_24h],
      ['حظر دخول نشط الآن', s.safety.active_login_blocks], ['تسجيلات مرفوضة خلال 24 ساعة', s.safety.registrations_limited_24h],
      ['حظر بين المستخدمين', s.safety.blocks],
    ]),
  );
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

// ------------------------------------------------------------------ reports

function renderReports(main) {
  const list = h('div', { class: 'admin-list', id: 'report-list' });
  main.replaceChildren(
    sectionHead('البلاغات'),
    segmented([['open', 'مفتوحة'], ['resolved', 'تم حلها'], ['dismissed', 'تم تجاهلها']], state.reportStatus,
      (v) => { state.reportStatus = v; loadReports(list); }, 'حالة البلاغات'),
    h('p', { class: 'admin-meta', text: 'يظهر محتوى الرسائل هنا فقط كدليل محفوظ عند تقديم بلاغ صريح. لا تظهر عناوين البريد أبدًا.' }),
    list);
  loadReports(list);
}

async function loadReports(list) {
  list.replaceChildren(spinner());
  try {
    const { reports } = await call('GET', `/api/admin/reports?status=${state.reportStatus}&limit=100`);
    paintReports(list, reports);
  } catch (err) {
    list.replaceChildren();
    handleError(err);
  }
}

function paintReports(list, reports) {
  if (!reports.length) {
    list.replaceChildren(emptyState(state.reportStatus === 'open' ? 'لا توجد بلاغات مفتوحة. كل شيء هادئ.' : 'لا شيء هنا بعد.'));
    return;
  }
  list.replaceChildren(...reports.map((r) => reportCard(r, list)));
}

function reportCard(r, list) {
  const card = h('article', { class: 'admin-card glass' });
  const evidence = (r.evidence || []).filter((e) => e && e.content);
  const actions = r.status === 'open'
    ? h('div', { class: 'admin-actions' },
      ...['dismiss', 'warn', ...(r.target === 'post' || r.target === 'comment' ? ['remove'] : []), 'suspend', 'ban']
        .map((a) => h('button', { type: 'button', class: `btn btn--sm ${ACTIONS[a].cls}`, onclick: () => resolveReport(r, a, card, list) }, ACTIONS[a].label)))
    : h('p', { class: 'admin-resolution' }, icon('check'), RESOLUTIONS[r.resolution] || r.resolution || '');
  card.append(...[
    h('div', { class: 'admin-card__head' },
      h('span', { class: 'chip chip--hot', text: REASONS[r.reason] || r.reason }),
      h('span', { class: 'admin-card__target', text: TARGETS[r.target] || r.target }),
      h('time', { class: 'admin-card__time', datetime: r.created_at, text: when(r.created_at) })),
    h('div', { class: 'admin-card__meta' },
      h('span', {}, 'المستخدم المُبلَّغ عنه: ', userRef(r.reported_user_ref)),
      h('span', {}, 'كل البلاغات ضده: ', h('b', { text: fmt(r.reported_user_reports_total) })),
      r.reported_user_ref ? h('button', {
        type: 'button', class: 'admin-link', onclick: () => { state.prefillRef = r.reported_user_ref; showTab('tools'); },
      }, 'إدارة الحساب') : null),
    r.details ? h('div', { class: 'admin-quote' }, h('span', { class: 'admin-quote__label', text: 'ملاحظة المُبلِّغ' }), h('p', { text: r.details })) : null,
    evidence.length ? h('div', { class: 'admin-evidence' },
      h('span', { class: 'admin-quote__label', text: 'الدليل (نسخة محفوظة وقت البلاغ)' }),
      ...evidence.map((e) => h('div', { class: 'admin-evidence__item' },
        h('p', { text: e.content }),
        e.created_at ? h('time', { datetime: e.created_at, text: when(e.created_at) }) : null)))
      : h('p', { class: 'admin-meta', text: 'لا يوجد نص محفوظ لهذا البلاغ.' }),
    actions,
  ].filter(Boolean)); // native append() would print "null"
  return card;
}

async function resolveReport(r, action, card, list) {
  const a = ACTIONS[action];
  if (!(await confirmSheet({ title: a.title, text: a.text, confirm: a.label, danger: !!a.danger }))) return;
  try {
    await call('POST', `/api/admin/reports/${encodeURIComponent(r.id)}/resolve`, { action });
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

function renderSecurity(main) {
  const select = h('select', { class: 'input admin-select', id: 'event-type', 'aria-label': 'نوع الحدث' },
    h('option', { value: '', text: 'كل الأحداث' }),
    ...Object.entries(EVENT_LABELS).map(([v, label]) => h('option', { value: v, text: label })));
  select.value = state.eventType;
  const list = h('div', { class: 'admin-events glass', id: 'event-list' });
  select.addEventListener('change', () => { state.eventType = select.value; loadEvents(list); });
  main.replaceChildren(
    sectionHead('سجل الأمان'),
    h('div', { class: 'admin-filter' }, select),
    h('p', { class: 'admin-meta', text: 'المستخدمون بمرجع داخلي فقط، والشبكات ببصمة مشفّرة مختصرة — لا عناوين IP ولا بريد. تُحذف السجلات تلقائيًا بعد 30 يومًا.' }),
    list);
  loadEvents(list);
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

// ------------------------------------------------------------------ maintenance

function renderTools(main) {
  const result = h('dl', { class: 'admin-group__rows', hidden: true });
  const runBtn = h('button', { type: 'button', class: 'btn btn--primary btn--block' }, icon('trash'), 'تشغيل التنظيف الآن');
  runBtn.addEventListener('click', async () => {
    runBtn.disabled = true;
    try {
      const { deleted } = await call('POST', '/api/admin/cleanup');
      const total = Object.values(deleted).reduce((a, b) => a + b, 0);
      result.replaceChildren(...Object.entries(deleted).map(([k, v]) => h('div', { class: 'admin-row' },
        h('dt', { text: CLEANUP_LABELS[k] || k }), h('dd', { text: fmt(v) }))));
      result.hidden = false;
      toast(total ? `تم حذف ${fmt(total)} عنصرًا منتهيًا.` : 'لا يوجد شيء منتهٍ للحذف.');
      refreshStatsQuietly();
    } catch (err) {
      handleError(err);
    } finally {
      runBtn.disabled = false;
    }
  });

  const refInput = h('input', {
    class: 'input', id: 'user-ref', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off',
    placeholder: 'مرجع المستخدم', value: state.prefillRef || '',
  });
  state.prefillRef = '';
  let status = 'suspended';
  const statusSeg = segmented([['active', 'تفعيل'], ['suspended', 'إيقاف'], ['banned', 'حظر']], status, (v) => { status = v; }, 'الحالة الجديدة');
  const applyBtn = h('button', { type: 'submit', class: 'btn btn--ghost btn--block' }, 'تطبيق');
  const statusForm = h('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      const ref = refInput.value.trim();
      if (!ref) { refInput.focus(); return; }
      const words = { active: ['تفعيل الحساب؟', 'يعود الحساب للعمل بشكل طبيعي.'], suspended: ['إيقاف الحساب؟', 'لن يستطيع الإرسال أو النشر حتى تعيد تفعيله.'], banned: ['حظر الحساب نهائيًا؟', 'يُحظر الحساب ويُسجَّل خروجه من كل الأجهزة.'] }[status];
      if (!(await confirmSheet({ title: words[0], text: words[1], confirm: 'تأكيد', danger: status !== 'active' }))) return;
      applyBtn.disabled = true;
      try {
        await call('POST', `/api/admin/users/${encodeURIComponent(ref)}/status`, { status });
        toast('تم تحديث حالة الحساب.');
        refreshStatsQuietly();
      } catch (err) {
        if (err.status === 404) toast('لا يوجد مستخدم بهذا المرجع.', 'error');
        else handleError(err);
      } finally {
        applyBtn.disabled = false;
      }
    },
  },
  h('div', { class: 'field' }, h('label', { for: 'user-ref', text: 'مرجع المستخدم (من البلاغات أو سجل الأمان)' }), refInput),
  statusSeg,
  applyBtn);

  main.replaceChildren(
    sectionHead('الصيانة'),
    h('article', { class: 'admin-group glass' },
      h('h3', { class: 'admin-group__title' }, icon('clock'), 'تنظيف البيانات المنتهية'),
      h('p', { class: 'admin-meta', text: 'يعمل تلقائيًا بشكل دوري: يحذف الرسائل والمحادثات التي انتهت مدتها، الجلسات القديمة، وسجلات الأمان والبلاغات المغلقة القديمة.' }),
      runBtn,
      result),
    h('article', { class: 'admin-group glass' },
      h('h3', { class: 'admin-group__title' }, icon('user'), 'حالة حساب'),
      statusForm),
    h('article', { class: 'admin-group glass' },
      h('h3', { class: 'admin-group__title' }, icon('lock'), 'تغيير رمز المشرف'),
      h('p', { class: 'admin-meta', text: 'إذا شاركت الرمز مع أحد أو ظهر في مكان عام، غيّره بهذه الأوامر في الخادم ثم ادخل بالرمز الجديد:' }),
      h('pre', { class: 'admin-code', dir: 'ltr' }, h('code', {
        text: 'cd /opt/dzplay/dzplay\nsed -i "s/^ADMIN_API_TOKEN=.*/ADMIN_API_TOKEN=$(openssl rand -hex 32)/" .env\ndocker compose up -d --force-recreate app\ngrep ADMIN_API_TOKEN .env',
      }))));
}

// ------------------------------------------------------------------ boot

async function boot() {
  const token = getToken();
  if (!token) { renderLogin(); return; }
  try {
    renderShell(await call('GET', '/api/admin/stats', undefined, token));
  } catch (err) {
    if (err.status === 401) signOut();
    else renderLogin(err.status === 429 ? `محاولات كثيرة. حاول بعد ${waitText(err.retryAfter)}.` : err.message);
  }
}

boot();
