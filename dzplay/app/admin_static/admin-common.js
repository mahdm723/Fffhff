// Shared helpers of the admin panel modules (API client, formatting, small UI pieces).
// All server data is inserted with textContent (h() / text:), never innerHTML.
import { confirmSheet, h, sheet, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';

export const BASE = document.documentElement.dataset.base || '';
const nf = new Intl.NumberFormat('ar-DZ');
export const fmt = (n) => nf.format(n ?? 0);
const stamp = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
export const when = (iso) => (iso ? stamp.format(new Date(iso)) : '');
export const shortRef = (ref) => (ref ? `${ref.slice(0, 8)}…` : '—');
export const bytes = (n) => (n >= 1024 ** 3 ? `${(n / 1024 ** 3).toFixed(2)} GB` : `${(n / 1024 ** 2).toFixed(1)} MB`);

// Hooks the shell installs (avoids import cycles between modules).
export const hooks = { onUnauthorized: () => {}, openUser: null, refreshStats: () => {} };

export class AdminError extends Error {
  constructor(status, code, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
}

export async function call(method, path, body) {
  const headers = { Accept: 'application/json' };
  const init = { method, headers, cache: 'no-store', credentials: 'same-origin' };
  if (path.startsWith('/api/admin')) path = BASE + path;
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

export const qs = (params) => {
  const u = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) if (v !== undefined && v !== null && v !== '' && v !== false) u.set(k, String(v));
  const s = u.toString();
  return s ? `?${s}` : '';
};

export function waitText(seconds) {
  const m = Math.max(1, Math.ceil((seconds || 60) / 60));
  return m === 1 ? 'دقيقة' : `${fmt(m)} دقائق`;
}

export function handleError(err) {
  if (err.status === 401) return hooks.onUnauthorized('انتهت الجلسة. سجّل الدخول من جديد.');
  if (err.status === 429) return toast(`محاولات كثيرة. حاول بعد ${waitText(err.retryAfter)}.`, 'error');
  return toast(err.message, 'error');
}

/** Run an API call with error handling; returns undefined on failure. */
export async function attempt(fn) {
  try { return await fn(); } catch (err) { handleError(err); return undefined; }
}

export async function confirmDanger(title, text, confirm = 'تأكيد') {
  return confirmSheet({ title, text, confirm, danger: true });
}

export function segmented(options, current, onPick, label) {
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

export function iconButton(name, label, onclick) {
  return h('button', { type: 'button', class: 'icon-btn glass', 'aria-label': label, title: label, onclick }, icon(name));
}

export function sectionHead(title, ...extra) {
  return h('div', { class: 'admin-section__head' }, h('h2', { class: 'admin-section__title', text: title }), ...extra);
}

export function emptyState(text) {
  return h('div', { class: 'empty glass admin-empty' }, h('p', { text }));
}

export function spinner() {
  return h('div', { class: 'admin-loading' }, h('span', { class: 'spinner' }));
}

/** A user reference: opens the user's page in the panel (full access), long-press/copy fallback. */
export function userRef(ref, label = null) {
  if (!ref) return h('span', { class: 'admin-ref', text: '—' });
  return h('button', {
    type: 'button', class: 'admin-ref', dir: 'ltr', title: 'فتح صفحة المستخدم',
    onclick: async (e) => {
      e.stopPropagation(); // inside a clickable row (e.g. a conversation): open the user only
      if (hooks.openUser) { hooks.openUser(ref); return; }
      try { await navigator.clipboard.writeText(ref); toast('تم نسخ المرجع.'); } catch { toast(ref); }
    },
  }, label || shortRef(ref));
}

export function chip(text, cls = '') {
  return h('span', { class: `chip ${cls}`, text });
}

export function field(label, control) {
  const id = control.id || `f-${Math.random().toString(36).slice(2, 9)}`;
  control.id = id;
  return h('div', { class: 'field' }, h('label', { for: id, text: label }), control);
}

/** A tall glass sheet for detail pages (user, conversation, idea…). */
export function detailSheet(title, build) {
  return sheet((panel, close) => {
    panel.classList.add('sheet--tall', 'admin-detail');
    const body = h('div', { class: 'admin-detail__body' }, spinner());
    panel.append(h('div', { class: 'admin-detail__head' }, h('h2', { text: title }),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: close }, 'إغلاق')), body);
    build(body, close);
  });
}

export function teamBadge(author) {
  if (!author || !author.team) return null;
  return chip(author.team === 'official' ? 'رسمي' : 'تعليق الفريق', 'chip--team');
}

export function authorLine(author) {
  if (!author) return h('span', { class: 'admin-meta', text: 'حساب محذوف' });
  return h('span', { class: 'admin-author' },
    author.team ? h('b', { text: author.team === 'official' ? 'DZPLAY الرسمي' : 'حساب نظام (dzplay)' })
      : userRef(author.id, author.email || shortRef(author.id)),
    author.display_name && !author.team ? h('bdi', { class: 'admin-meta', text: author.display_name }) : null,
    teamBadge(author));
}
