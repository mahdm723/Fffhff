// On-screen keyboard: keeps the field you type in (and its send button) visible in every form.
//
// - Android app / Chrome: the page itself shrinks when the keyboard opens (native insets in the app,
//   interactive-widget=resizes-content in the browser), so --kb stays 0.
// - iOS Safari / browsers that only shrink the *visual* viewport: --kb = the keyboard height, used by
//   fixed bottom bars and sheets to sit above it.
// In both cases body.kb-open hides the bottom navigation and the focused field is scrolled into view.
const FIELD = 'input:not([type=checkbox]):not([type=radio]):not([type=button]):not([type=submit]), textarea, [contenteditable="true"]';

let tallest = 0;

function focusedField() {
  const el = document.activeElement;
  return el && el.matches && el.matches(FIELD) ? el : null;
}

function update() {
  const vv = window.visualViewport;
  const kb = vv ? Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop)) : 0;
  if (!focusedField()) tallest = Math.max(tallest, window.innerHeight);
  const shrunk = tallest && window.innerHeight < tallest * 0.78; // the page itself was resized by the keyboard
  const open = !!focusedField() && (kb > 80 || shrunk);
  document.documentElement.style.setProperty('--kb', `${kb > 80 ? kb : 0}px`);
  document.body.classList.toggle('kb-open', open);
}

function reveal(el) {
  // after the keyboard animation: keep the field (and the button next to it) on screen
  setTimeout(() => {
    if (document.activeElement !== el) return;
    update();
    // the whole composer (field + its send button), not only the field
    const block = el.closest('[data-kb-block], .idea-composer, .msg-dock, .chat__composer-wrap, form') || el;
    try { block.scrollIntoView({ block: 'nearest', inline: 'nearest' }); } catch { block.scrollIntoView(); }
  }, 280);
}

export function initKeyboard() {
  tallest = window.innerHeight;
  const vv = window.visualViewport;
  if (vv) { vv.addEventListener('resize', update); vv.addEventListener('scroll', update); }
  window.addEventListener('resize', update);
  document.addEventListener('focusin', (e) => { if (e.target.matches && e.target.matches(FIELD)) { update(); reveal(e.target); } });
  document.addEventListener('focusout', () => setTimeout(update, 60));
  update();
}
