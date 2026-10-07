// V6 phase 2: the market pane of Home — top gainers / losers of the last 24 h (Bybit spot, USDT pairs).
// The phone only calls /api/market (the server polls Bybit). For information only: no advice, no trading.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { h, sheet } from '../ui.js';

const SVG = 'http://www.w3.org/2000/svg';
const compact = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 1 });

function svg(tag, attrs) {
  const el = document.createElementNS(SVG, tag);
  for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v);
  return el;
}

function formatPrice(p) {
  const v = Number(p);
  if (!Number.isFinite(v)) return '—';
  const digits = v >= 1000 ? 2 : v >= 1 ? 4 : v >= 0.01 ? 5 : 8;
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: digits }).format(v);
}

function pct(v) { return `${v > 0 ? '+' : ''}${v.toFixed(2)}%`; }

function ago(iso) {
  if (!iso) return '';
  const s = Math.max(0, Math.round((Date.now() - Date.parse(iso)) / 1000));
  if (s < 60) return 'الآن';
  const m = Math.round(s / 60);
  if (m < 60) return `منذ ${m} د`;
  const hh = Math.round(m / 60);
  return hh < 48 ? `منذ ${hh} س` : `منذ ${Math.round(hh / 24)} يوم`;
}

// 24 h hourly closes as a thin line (no axes: a shape hint, the numbers are in the row).
function sparkline(points, up, w = 76, hgt = 28) {
  const box = svg('svg', { viewBox: `0 0 ${w} ${hgt}`, width: String(w), height: String(hgt), class: 'spark', 'aria-hidden': 'true' });
  if (!points || points.length < 2) return box;
  const min = Math.min(...points);
  const max = Math.max(...points);
  const span = max - min || 1;
  const pad = 2;
  const xy = points.map((v, i) => [(i / (points.length - 1)) * (w - pad * 2) + pad, hgt - pad - ((v - min) / span) * (hgt - pad * 2)]);
  const d = xy.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join(' ');
  box.append(svg('path', { d, class: up ? 'spark__line spark__line--up' : 'spark__line spark__line--down' }));
  const [lx, ly] = xy[xy.length - 1];
  box.append(svg('circle', { cx: lx.toFixed(1), cy: ly.toFixed(1), r: '2.2', class: up ? 'spark__dot spark__dot--up' : 'spark__dot spark__dot--down' }));
  return box;
}

function change(v) {
  const up = v > 0;
  return h('span', { class: `mk-change ${up ? 'mk-change--up' : 'mk-change--down'}`, dir: 'ltr' },
    icon(up ? 'trendUp' : 'trendDown'), pct(v));
}

function details(c) {
  sheet((panel, close) => {
    const up = c.change_pct > 0;
    panel.append(
      h('div', { class: 'mk-sheet__head' },
        h('h2', { class: 'mk-sheet__title', dir: 'ltr' }, h('b', { text: c.base }), h('span', { text: ' / USDT' })),
        change(c.change_pct)),
      h('div', { class: 'mk-sheet__price', dir: 'ltr', text: formatPrice(c.price) }),
      h('div', { class: 'mk-sheet__spark' }, sparkline(c.spark, up, 320, 90)),
      h('p', { class: 'mk-sheet__note', text: 'آخر 24 ساعة (سعر الإغلاق كل ساعة).' }),
      h('dl', { class: 'mk-stats' },
        h('div', {}, h('dt', { text: 'أعلى سعر 24 س' }), h('dd', { dir: 'ltr', text: formatPrice(c.high24) })),
        h('div', {}, h('dt', { text: 'أدنى سعر 24 س' }), h('dd', { dir: 'ltr', text: formatPrice(c.low24) })),
        h('div', {}, h('dt', { text: 'حجم التداول 24 س' }), h('dd', { dir: 'ltr', text: `${compact.format(c.turnover24)} USDT` })),
        h('div', {}, h('dt', { text: 'الزوج' }), h('dd', { dir: 'ltr', text: c.symbol }))),
      h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إغلاق'));
  });
}

function row(c) {
  const up = c.change_pct > 0;
  const b = h('button', { class: 'mk-row', type: 'button', dataset: { symbol: c.symbol },
    'aria-label': `${c.base}: ${formatPrice(c.price)} دولار، ${pct(c.change_pct)} خلال 24 ساعة` },
  h('span', { class: 'mk-row__name', dir: 'ltr' }, h('b', { text: c.base }), h('small', { text: '/USDT' })),
  sparkline(c.spark, up),
  h('span', { class: 'mk-row__nums', dir: 'ltr' }, h('span', { class: 'mk-row__price', text: formatPrice(c.price) }), change(c.change_pct)));
  b.addEventListener('click', () => details(c));
  return b;
}

// Re-render a list, sliding rows that changed rank from their old place (FLIP).
function paintList(list, coins) {
  const before = new Map([...list.children].map((el) => [el.dataset.symbol, el.getBoundingClientRect().top]));
  list.replaceChildren(...coins.map(row));
  if (!before.size || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
  for (const el of list.children) {
    const old = before.get(el.dataset.symbol);
    if (old === undefined) continue;
    const dy = old - el.getBoundingClientRect().top;
    if (Math.abs(dy) < 1) continue;
    el.animate([{ transform: `translateY(${dy}px)` }, { transform: 'translateY(0)' }], { duration: 380, easing: 'cubic-bezier(.2,.8,.2,1)' });
  }
}

export function renderMarket(pane) {
  const gainers = h('div', { class: 'mk-list', role: 'list', 'aria-label': 'الأعلى ربحًا' });
  const losers = h('div', { class: 'mk-list', role: 'list', 'aria-label': 'الأعلى خسارة' });
  const updated = h('span', { class: 'mk-updated' });
  const banner = h('div', { class: 'mk-banner', role: 'status', hidden: true });
  const disclaimer = h('p', { class: 'mk-disclaimer' });
  const body = h('div', { class: 'mk-body' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  let data = null;
  let timer = null;
  let tick = null;
  let visible = false;
  let loading = false;

  const refreshBtn = h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'تحديث الأسعار' }, icon('refresh'));
  refreshBtn.addEventListener('click', () => load());

  pane.replaceChildren(
    h('header', { class: 'mk-head' },
      h('div', {}, h('h1', { class: 'mk-title', text: 'السوق' }), h('p', { class: 'mk-sub' }, 'أكبر تحرّك خلال 24 ساعة · ', updated)),
      refreshBtn),
    banner, body, disclaimer);

  function paint() {
    disclaimer.textContent = data.disclaimer || '';
    updated.textContent = data.updated_at ? `آخر تحديث ${ago(data.updated_at)}` : '';
    if (!data.enabled) {
      banner.hidden = true;
      body.replaceChildren(h('div', { class: 'empty glass' }, icon('chart'), h('h2', { text: 'السوق متوقف حاليًا' }),
        h('p', { text: 'سيعود قريبًا.' })));
      return;
    }
    banner.hidden = !(data.stale && data.updated_at);
    banner.replaceChildren(icon('clock'), h('span', { text: `الأسعار لم تُحدَّث منذ مدة (${ago(data.updated_at)}). قد لا تكون حالية.` }));
    if (!data.gainers.length && !data.losers.length) {
      body.replaceChildren(h('div', { class: 'empty glass' }, icon('chart'), h('h2', { text: 'لا توجد بيانات بعد' }),
        h('p', { text: 'تُجلب الأسعار خلال لحظات، حاول بعد قليل.' })));
      return;
    }
    if (!body.contains(gainers)) {
      body.replaceChildren(
        h('section', { class: 'mk-section glass' }, h('h2', { class: 'mk-section__title' }, icon('trendUp'), 'الأعلى ربحًا'), gainers),
        h('section', { class: 'mk-section glass' }, h('h2', { class: 'mk-section__title' }, icon('trendDown'), 'الأعلى خسارة'), losers));
    }
    paintList(gainers, data.gainers);
    paintList(losers, data.losers);
  }

  async function load() {
    if (loading) return;
    loading = true;
    refreshBtn.disabled = true;
    try {
      data = await api.get('/api/market');
      paint();
    } catch (err) {
      if (!data) body.replaceChildren(h('div', { class: 'feed-status' },
        h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => load() }, 'إعادة المحاولة')));
    } finally {
      loading = false;
      refreshBtn.disabled = false;
    }
  }

  function schedule() {
    clearInterval(timer);
    clearInterval(tick);
    if (!visible || document.hidden) return;
    const every = Math.max(30, (data && data.refresh_seconds) || 60) * 1000;
    timer = setInterval(load, every);
    tick = setInterval(() => { if (data && data.updated_at) updated.textContent = `آخر تحديث ${ago(data.updated_at)}`; }, 15000);
  }
  const onVisibility = () => { if (!document.hidden && visible) load(); schedule(); };
  document.addEventListener('visibilitychange', onVisibility);

  return {
    setVisible(on) {
      if (on === visible) return;
      visible = on;
      if (on) load();
      schedule();
    },
    destroy() {
      clearInterval(timer);
      clearInterval(tick);
      document.removeEventListener('visibilitychange', onVisibility);
    },
  };
}
