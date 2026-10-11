// V6 phase 14: the access pages (Android browser, old app, computer) — server-rendered, this only helps them.
// 1. An iPad's Safari says «Macintosh»: a touch screen tells it apart from a Mac (cookie dz_touch, then reload).
// 2. A phone that used the web app before: its service worker and offline copy are removed.
// 3. Our app (3.2.0+) that could not prove itself: «إعادة المحاولة» asks it to try again.
(() => {
  const mac = /Macintosh/.test(navigator.userAgent);
  if (mac && navigator.maxTouchPoints > 1 && !/(^|;\s*)dz_touch=1/.test(document.cookie)) {
    const secure = location.protocol === 'https:' ? '; Secure' : '';
    document.cookie = `dz_touch=1; path=/; max-age=31536000; SameSite=Lax${secure}`;
    if (/(^|;\s*)dz_touch=1/.test(document.cookie)) { location.reload(); return; } // cookies off: no loop
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.getRegistrations().then((regs) => regs.forEach((r) => r.unregister())).catch(() => {});
  }
  if (window.caches) caches.keys().then((keys) => keys.forEach((k) => caches.delete(k))).catch(() => {});
  const retry = document.getElementById('gate-retry');
  const native = window.DZPLAYAndroid;
  if (retry && native && typeof native.refreshAccess === 'function') {
    retry.hidden = false;
    retry.addEventListener('click', () => { retry.disabled = true; native.refreshAccess(); });
  }
})();
