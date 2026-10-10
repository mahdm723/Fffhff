// V6 phase 8: editable texts from the content system (/api/content/<key>). The server renders them with its
// own Markdown converter (everything escaped, a few tags only); here they are checked again against the same
// short allowlist before entering the page — defence in depth, never trust markup blindly.
import { api } from './api.js';
import { h } from './ui.js';

const TAGS = new Set(['H2', 'H3', 'H4', 'P', 'UL', 'OL', 'LI', 'STRONG', 'EM', 'A', 'BR']);
const SAFE_HREF = /^(https:\/\/|\/(?![/\\]))/;
const cache = new Map();

function clean(node) {
  for (const el of [...node.children]) {
    if (!TAGS.has(el.tagName)) { el.replaceWith(document.createTextNode(el.textContent || '')); continue; }
    for (const a of [...el.attributes]) {
      const ok = el.tagName === 'A' && ((a.name === 'href' && SAFE_HREF.test(a.value)) || a.name === 'target' || a.name === 'rel');
      if (!ok) el.removeAttribute(a.name);
    }
    if (el.tagName === 'A' && el.getAttribute('target')) el.setAttribute('rel', 'noopener noreferrer nofollow');
    clean(el);
  }
}

/** A <div class="prose"> with the cleaned HTML (a <template> is inert: nothing in it loads or runs). */
export function safeHtml(html, cls = '') {
  const t = document.createElement('template');
  t.innerHTML = String(html || '');
  clean(t.content);
  const box = h('div', { class: `prose cms ${cls}`.trim() });
  box.append(t.content);
  return box;
}

/** {key, title, html, text, updated, version} or null; cached for this page load. */
export function loadContent(key) {
  if (!cache.has(key)) cache.set(key, api.get(`/api/content/${encodeURIComponent(key)}`).catch(() => null));
  return cache.get(key);
}

export function forgetContent() { cache.clear(); }

/** A block that fills itself with the text (or stays empty when the text is empty / unavailable). */
export function contentBlock(key, cls = '') {
  const box = h('div', { class: `cms-slot ${cls}`.trim() });
  loadContent(key).then((c) => { if (c && c.text) box.replaceChildren(safeHtml(c.html)); else box.remove(); });
  return box;
}
