// DZPLAY service worker: offline app shell + privacy-preserving push notifications.
const VERSION = 'dz-v23';
const SHELL = [
  '/', '/css/app.css', '/manifest.webmanifest',
  '/js/app.js', '/js/api.js', '/js/ui.js', '/js/icons.js', '/js/store.js', '/js/notify.js',
  '/js/antibot.js', '/js/pow-worker.js', '/js/ideas.js', '/js/privacy.js', '/js/people.js', '/js/onboarding.js', '/js/native.js', '/js/keyboard.js', '/js/media-pick.js', '/js/views/support.js', '/js/views/membership.js', '/js/views/earnings.js',
  '/js/views/auth.js', '/js/views/home.js', '/js/views/messages.js', '/js/views/chat.js', '/js/views/profile.js', '/js/views/user.js', '/js/views/market.js', '/js/views/users.js', '/js/views/notifications.js', '/js/views/post.js',
  '/fonts/plex-arabic-arabic-400.woff2', '/fonts/plex-arabic-arabic-500.woff2', '/fonts/plex-arabic-arabic-700.woff2',
  '/icons/icon.svg', '/icons/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      // every older cache goes, including the V5 Reels prefetch cache ("dz-media-v1")
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Never cache the API, user pictures (signed, session-bound, some must vanish) or anything cross-origin.
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')
      || url.pathname.startsWith('/media/')) return;

  if (req.mode === 'navigate') {
    event.respondWith(fetch(req).catch(() => caches.match('/')));
    return;
  }
  // Network first (fresh code after deploys), cache as offline fallback.
  event.respondWith(
    fetch(req)
      .then((res) => {
        if (res.ok) { const copy = res.clone(); caches.open(VERSION).then((c) => c.put(req, copy)); }
        return res;
      })
      .catch(() => caches.match(req)),
  );
});

self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch { /* ignore */ }
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    if (windows.some((w) => w.visibilityState === 'visible')) return; // the open app already shows it
    await self.registration.showNotification(data.title || 'DZPLAY', {
      body: data.body || 'لديك رسالة جديدة على DZPLAY',
      tag: 'dz-message',
      renotify: true,
      icon: '/icons/icon-192.png',
      data: { url: data.url || '/#/messages' },
    });
  })());
});

self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || '/#/messages';
  event.waitUntil((async () => {
    const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    for (const w of windows) {
      if ('focus' in w) { await w.focus(); if ('navigate' in w) w.navigate(target).catch(() => {}); return; }
    }
    await self.clients.openWindow(target);
  })());
});
