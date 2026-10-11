// V6 phase 12: the interface in three languages — Arabic (the original, and the fallback), French, English.
// The Arabic text written in the code is the key: t('حساب جديد'). The dictionary of the chosen language
// ({msgid: text}) comes from /api/i18n/<lang> (texts edited in the panel included) and is kept in localStorage,
// so the app starts in the right language at once. A missing translation shows the Arabic text — never a key.
// Variables are written {name}: t('أعجب {n} أشخاص', { n: 3 }); a plural is a text with forms chosen by `n`.

export const LANGS = ['ar', 'fr', 'en'];
const DIRS = { ar: 'rtl', fr: 'ltr', en: 'ltr' };
const LOCALES = { ar: 'ar-DZ', fr: 'fr-DZ', en: 'en-GB' }; // ar-DZ keeps the Latin digits used until now
export const NAMES = { ar: 'العربية', fr: 'Français', en: 'English' };
const KEY = 'dz.lang';
const DICT = (code) => `dz:i18n:${code}`;
const VAR = /\{(\w+)\}/g;

function stored(key) { try { return localStorage.getItem(key); } catch { return null; } }
function keep(key, value) { try { localStorage.setItem(key, value); } catch { /* storage blocked */ } }

/** The phone's language the first time (Arabic, French, otherwise English), then the saved choice. */
export function initialLang() {
  const saved = stored(KEY);
  if (LANGS.includes(saved)) return saved;
  for (const l of navigator.languages || [navigator.language || '']) {
    const code = String(l).toLowerCase().split('-')[0];
    if (LANGS.includes(code)) return code;
  }
  return 'en';
}

// The admin panel (admin.html carries data-base) stays in Arabic: it shares ui.js and this storage with the app.
const ADMIN = typeof document !== 'undefined' && document.documentElement.hasAttribute('data-base');
let current = ADMIN ? 'ar' : initialLang();
let messages = ADMIN ? {} : readDict(current);
const listeners = new Set();

function readDict(code) {
  try { return (JSON.parse(stored(DICT(code)) || 'null') || {}).messages || {}; } catch { return {}; }
}

export function lang() { return current; }
export function dir() { return DIRS[current]; }
export function locale() { return LOCALES[current]; }
export function isRtl() { return DIRS[current] === 'rtl'; }

function applyToDocument() {
  if (ADMIN) return;
  const r = document.documentElement;
  r.lang = current;
  r.dir = DIRS[current];
}
applyToDocument();

/** Change the language now (the dictionary is fetched if not cached); listeners redraw the page. */
export async function setLang(code, { fetchDict = true } = {}) {
  if (ADMIN || !LANGS.includes(code)) return;
  const changed = code !== current;
  current = code;
  keep(KEY, code);
  messages = readDict(code);
  applyToDocument();
  if (fetchDict) await refreshDictionary();
  if (changed) listeners.forEach((fn) => fn(code));
}

/** Fetch the dictionary of the current language (after boot, and when a text changed in the panel). */
export async function refreshDictionary() {
  if (ADMIN) return false;
  const code = current;
  try {
    const res = await fetch(`/api/i18n/${code}`, { credentials: 'same-origin', cache: 'no-store', headers: { Accept: 'application/json' } });
    if (!res.ok) return false;
    const data = await res.json();
    const old = stored(DICT(code));
    keep(DICT(code), JSON.stringify({ version: data.version, messages: data.messages }));
    if (code === current) messages = data.messages || {};
    return !old || JSON.parse(old).version !== data.version;
  } catch { return false; }
}

export function onLangChange(fn) { listeners.add(fn); return () => listeners.delete(fn); }

function pluralForm(forms, n) {
  let cat = 'other';
  try { cat = new Intl.PluralRules(LOCALES[current]).select(Number(n)); } catch { /* old browser */ }
  return forms[cat] || forms.other || Object.values(forms)[0] || '';
}

function resolve(msgid, params) {
  let text = Object.prototype.hasOwnProperty.call(messages, msgid) ? messages[msgid] : msgid;
  if (text && typeof text === 'object') text = pluralForm(text, params && params.n != null ? params.n : 1);
  return typeof text === 'string' && text ? text : msgid;
}

/** Marks a text kept in a constant (tabs, choices…): translated where it is shown, with t(value). */
export function N_(msgid) { return msgid; }

/** The text of msgid in the current language, {variables} filled. */
export function t(msgid, params) {
  const text = resolve(msgid, params);
  if (!params) return text;
  return text.replace(VAR, (m, name) => (Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : m));
}

/** Like t(), for a sentence holding elements: returns strings and nodes in the translated order (h() children). */
export function tNodes(msgid, params = {}) {
  const text = resolve(msgid, params);
  const out = [];
  let last = 0;
  text.replace(VAR, (m, name, at) => {
    if (at > last) out.push(text.slice(last, at));
    const v = Object.prototype.hasOwnProperty.call(params, name) ? params[name] : m;
    out.push(v instanceof Node ? v : String(v));
    last = at + m.length;
    return m;
  });
  if (last < text.length) out.push(text.slice(last));
  return out;
}

// --- numbers, money and dates in the language's format ----------------------------------------------
export function fmtNumber(n, options = {}) {
  try { return new Intl.NumberFormat(LOCALES[current], options).format(n); } catch { return String(n); }
}

/** 50 → «50 USDT», 12.5 → «12.50 USDT» (fr: «12,50 USDT»). */
export function fmtMoney(value, currency = 'USDT') {
  const n = Number(value);
  if (!Number.isFinite(n)) return `${value} ${currency}`;
  const whole = Number.isInteger(n);
  return `${fmtNumber(n, { minimumFractionDigits: whole ? 0 : 2, maximumFractionDigits: 2 })} ${currency}`;
}

export function fmtDate(d, options = { day: 'numeric', month: 'long', year: 'numeric' }) {
  const date = d instanceof Date ? d : new Date(d);
  try { return new Intl.DateTimeFormat(LOCALES[current], options).format(date); } catch { return date.toISOString().slice(0, 10); }
}

export function fmtTime(d) {
  return fmtDate(d, { hour: '2-digit', minute: '2-digit' });
}

/** «منذ 5 دقائق» / «il y a 5 minutes» / «5 minutes ago». */
export function fmtRelative(d) {
  const date = d instanceof Date ? d : new Date(d);
  const s = Math.round((date.getTime() - Date.now()) / 1000);
  const abs = Math.abs(s);
  const [value, unit] = abs < 60 ? [s, 'second'] : abs < 3600 ? [Math.round(s / 60), 'minute']
    : abs < 86400 ? [Math.round(s / 3600), 'hour'] : [Math.round(s / 86400), 'day'];
  try { return new Intl.RelativeTimeFormat(LOCALES[current], { numeric: 'auto' }).format(value, unit); } catch { return fmtDate(date); }
}
