// DOM helpers. User content is only ever inserted with textContent — never innerHTML.
import { icon } from './icons.js';

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

/** V5: the blue star (an official, trusted account — not an identity check). */
export function starMark() {
  return h('span', { class: 'star-mark', title: 'حساب موثّق' }, icon('verified'), h('span', { class: 'sr-only', text: 'حساب موثّق' }));
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

export function wordmark(large = false) {
  return h('div', { class: `wordmark ${large ? 'wordmark--lg' : ''}`, 'aria-label': 'dzplay' }, 'dzplay', h('span', { class: 'wordmark__dot' }));
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
    if (onClose) onClose();
    if (prev && prev.focus) prev.focus();
  };
  const onKey = (e) => { if (e.key === 'Escape' && dismissible) close(); };
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop && dismissible) close(); });
  document.addEventListener('keydown', onKey);
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
    textarea.style.height = Math.min(textarea.scrollHeight, max) + 'px';
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
