// DOM helpers. User content is only ever inserted with textContent — never innerHTML.
import { appName, wordmarkParts } from './brand.js';
import { STAR_CHECK, STAR_SEAL, icon } from './icons.js';
import { LOGO } from './logo-shapes.js';

export function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === 'class') el.className = v;
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
    else if (k === 'dataset') Object.assign(el.dataset, v);
    else if (v === true) el.setAttribute(k, '');
    else el.setAttribute(k, String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

export const REPORT_REASONS = [
  ['spam', 'رسائل مزعجة (سبام)'],
  ['harassment', 'تحرش أو مضايقة'],
  ['threat', 'تهديد'],
  ['inappropriate', 'محتوى غير لائق'],
  ['minor', 'محتوى يخص قاصرًا'],
  ['other', 'سبب آخر'],
];

export function avatar(size = '') {
  return h('div', { class: `avatar ${size ? 'avatar--' + size : ''}`, 'aria-hidden': 'true' }, icon('mask'));
}

const AVATAR_TONES = 6;

/** Round avatar: the profile picture (V6, signed URL) when there is one, else the person's initial;
 *  the dzplay mask for anonymous / default-named people. */
export function personAvatar(name, { size = '', anonymous = false, active = false, url = null } = {}) {
  const cls = `avatar ${size ? 'avatar--' + size : ''}`;
  let el;
  if (anonymous || !name || name === 'dzplay') el = h('div', { class: cls, 'aria-hidden': 'true' }, icon('mask'));
  else if (url) {
    const img = h('img', { src: url, alt: '', loading: 'lazy', decoding: 'async', draggable: 'false' });
    el = h('div', { class: `${cls} avatar--photo`, 'aria-hidden': 'true' }, img);
    img.addEventListener('error', () => { el.replaceWith(personAvatar(name, { size, active })); }, { once: true });
  } else {
    const letter = (Array.from(name.trim())[0] || '?').toLocaleUpperCase('ar');
    let n = 0;
    for (const ch of name) n = (n * 31 + ch.codePointAt(0)) >>> 0;
    el = h('div', { class: `${cls} avatar--letter avatar--t${n % AVATAR_TONES}`, 'aria-hidden': 'true' }, h('span', { text: letter }));
  }
  if (active) el.append(h('span', { class: 'avatar__online' }));
  return el;
}

const GENDER = { male: ['male', 'رجل'], female: ['female', 'أنثى'] };

/** Small gender icon next to a name (nothing for "prefer not to say"). */
export function genderMark(gender) {
  const g = GENDER[gender];
  if (!g) return null;
  return h('span', { class: `gender gender--${gender}`, title: g[1] }, icon(g[0]), h('span', { class: 'sr-only', text: g[1] }));
}

/** A display name as plain text (dir="auto": Arabic or Latin) + gender icon. */
export function nameLine(name, gender, cls = '', verified = false) {
  return h('span', { class: `name-line ${cls}` }, h('bdi', { class: 'name-line__text', text: name || 'dzplay' }),
    verified ? starMark() : null, genderMark(gender));
}

/** The blue star (the membership badge): one size next to every name (18px, centred on the text line);
 *  `large` for the decorative badge on the membership pages. Colours: --star (per theme) + a white check. */
export function starMark({ large = false } = {}) {
  const NS = 'http://www.w3.org/2000/svg';
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', '0 0 24 24');
  svg.setAttribute('aria-hidden', 'true');
  for (const [cls, d] of [['star-mark__seal', STAR_SEAL], ['star-mark__check', STAR_CHECK]]) {
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('class', cls);
    path.setAttribute('d', d);
    svg.append(path);
  }
  return h('span', { class: `star-mark${large ? ' star-mark--lg' : ''}`, title: 'حساب موثّق' }, svg, h('span', { class: 'sr-only', text: 'حساب موثّق' }));
}

export async function copyText(text, done = 'نُسخ.') {
  try {
    await navigator.clipboard.writeText(text);
    toast(done);
  } catch {
    toast(text, 'info', 6000);
  }
}

/** "DZ-XXXXXX" chip with a copy button. */
export function idChip(publicId) {
  if (!publicId) return null;
  return h('button', { class: 'id-chip', type: 'button', 'aria-label': `نسخ المعرّف ${publicId}`, onclick: () => copyText(publicId, 'نُسخ المعرّف.') },
    h('span', { dir: 'ltr', text: publicId }), icon('copy'));
}

let logoCopies = 0;

/** The logo (brand/, generated into logo-shapes.js): three candles, the red dot glowing on the tallest one's wick.
 *  Never mirrored (fixed coordinates, LTR). The small drawing at 24px and below, the large one above. `pulse`: the
 *  slow glow of the header, boot and sign-in screens (off with «تقليل الحركة»); `mono`: one colour, no glow.
 *  The candles take the text colour (currentColor). */
export function logoMark({ size = 24, pulse = false, mono = false } = {}) {
  const NS = 'http://www.w3.org/2000/svg';
  const el = (tag, attrs) => {
    const e = document.createElementNS(NS, tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, String(v));
    return e;
  };
  const L = size <= 24 ? LOGO.small : LOGO.large;
  const id = `dz-logo-${(logoCopies += 1)}`; // gradient ids: unique per copy
  const gradient = (gid, attrs, stops) => {
    const g = el('radialGradient', { id: gid, ...attrs });
    for (const [offset, color, opacity] of stops) {
      g.append(el('stop', { offset, 'stop-color': color, ...(opacity === undefined ? {} : { 'stop-opacity': opacity }) }));
    }
    return g;
  };
  const { stops, ...dotAttrs } = L.dotGradient;
  const [cx, cy, r] = L.dot;
  const svg = el('svg', { viewBox: '0 0 100 100', width: size, height: size, 'aria-hidden': 'true',
    class: `logo-mark${pulse ? ' logo-mark--pulse' : ''}${mono ? ' logo-mark--mono' : ''}` });
  const defs = el('defs', {});
  defs.append(gradient(`${id}-dot`, dotAttrs, stops), gradient(`${id}-glow`, {}, L.glowStops));
  const ink = el('g', { class: 'ink', fill: 'currentColor' });
  for (const [x, y, width, height, rx] of L.ink) ink.append(el('rect', { x, y, width, height, rx }));
  svg.append(defs, el('circle', { class: 'glow', cx, cy, r: L.glow, fill: `url(#${id}-glow)` }), ink,
    el('circle', { class: 'dot', cx, cy, r, fill: `url(#${id}-dot)` }));
  return svg;
}

/** The logo + the name side by side (left to right, like the name itself). */
export function logoLockup({ size = 24, pulse = false, large = false } = {}) {
  return h('span', { class: `logo-lockup${large ? ' logo-lockup--lg' : ''}` }, logoMark({ size, pulse }), wordmark(large));
}

export function wordmark(large = false) {
  const [first, rest] = wordmarkParts();
  return h('div', { class: `wordmark ${large ? 'wordmark--lg' : ''}`, 'aria-label': appName() },
    first, h('span', { class: 'wordmark__dot' }), rest || null);
}

export function toast(message, type = 'info', ms = 3200) {
  const box = document.getElementById('toasts');
  const t = h('div', { class: `toast ${type === 'error' ? 'toast--error' : ''}`, text: message });
  box.append(t);
  setTimeout(() => t.remove(), ms);
}

export function sheet(build, onClose = null, { dismissible = true } = {}) {
  const prev = document.activeElement;
  const backdrop = h('div', { class: 'sheet-backdrop' });
  const panel = h('div', { class: 'sheet', role: 'dialog', 'aria-modal': 'true' }, h('div', { class: 'sheet__grip' }));
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    backdrop.remove();
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('hashchange', onRoute);
    if (onClose) onClose();
    if (prev && prev.focus && prev.isConnected) prev.focus();
  };
  const onKey = (e) => { if (e.key === 'Escape' && dismissible) close(); };
  // leaving the page (a link inside the sheet, or the phone's back button) closes it: it never stays over
  // the next page. Mandatory sheets (privacy notice, onboarding) are not dismissible and stay.
  const onRoute = () => { if (dismissible) close(); };
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop && dismissible) close(); });
  document.addEventListener('keydown', onKey);
  window.addEventListener('hashchange', onRoute);
  build(panel, close);
  backdrop.append(panel);
  document.body.append(backdrop);
  const focusable = panel.querySelector('button, input, textarea');
  if (focusable) focusable.focus();
  return close;
}

export function confirmSheet({ title, text, confirm, danger = false }) {
  return new Promise((resolve) => {
    let answer = false;
    sheet((panel, close) => {
      const finish = (v) => { answer = v; close(); };
      panel.append(
        h('h2', { text: title }),
        h('p', { text }),
        h('div', { class: 'actions' },
          h('button', { class: `btn btn--block ${danger ? 'btn--danger' : 'btn--primary'}`, onclick: () => finish(true) }, confirm),
          h('button', { class: 'btn btn--block btn--ghost', onclick: () => finish(false) }, 'إلغاء'),
        ),
      );
    }, () => resolve(answer)); // backdrop / Escape resolve to false
  });
}

const timeFmt = new Intl.DateTimeFormat('ar-DZ', { hour: '2-digit', minute: '2-digit' });
const dayFmt = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'long' });

export function formatTime(iso) { return iso ? timeFmt.format(new Date(iso)) : ''; }

function sameDay(a, b) { return a.toDateString() === b.toDateString(); }

export function formatDay(iso) {
  const d = new Date(iso);
  const now = new Date();
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (sameDay(d, now)) return 'اليوم';
  if (sameDay(d, y)) return 'أمس';
  return dayFmt.format(d);
}

export function formatListTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  if (sameDay(d, now)) return formatTime(iso);
  return formatDay(iso);
}

export function autoGrow(textarea, max = 140) {
  const fit = () => {
    textarea.style.height = 'auto';
    // box-sizing: border-box — the height includes the borders, scrollHeight does not (without them the
    // line and its placeholder were cut by 2px)
    const borders = textarea.offsetHeight - textarea.clientHeight;
    textarea.style.height = Math.min(textarea.scrollHeight + borders, max) + 'px';
  };
  textarea.addEventListener('input', fit);
  requestAnimationFrame(fit);
  return fit;
}

export function newClientId() {
  const a = new Uint8Array(12);
  crypto.getRandomValues(a);
  return Array.from(a, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** True inside the DZPLAY Android app (its WebView adds "DZPLAYApp/<version>" to the user agent). */
export function isAndroidApp() {
  return /\bDZPLAYApp\//.test(navigator.userAgent);
}

/** True in a regular Android browser (not already inside the installed app). */
export function canOfferAndroidApp(config) {
  return !!(config && config.android_apk_url) && /Android/i.test(navigator.userAgent) && !isAndroidApp()
    && !matchMedia('(display-mode: standalone)').matches && !document.referrer.startsWith('android-app://');
}
