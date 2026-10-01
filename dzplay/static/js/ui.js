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

export function avatar(size = '') {
  return h('div', { class: `avatar ${size ? 'avatar--' + size : ''}`, 'aria-hidden': 'true' }, icon('mask'));
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

export function sheet(build, onClose = null) {
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
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  backdrop.addEventListener('click', (e) => { if (e.target === backdrop) close(); });
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
