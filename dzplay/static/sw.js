// DZPLAY service worker: offline app shell + privacy-preserving push notifications.
const VERSION = 'dz-v5';
const MEDIA_CACHE = 'dz-media-v1'; // filled by js/reels-prefetch.js; survives app updates
const SHELL = [
  '/', '/css/app.css', '/manifest.webmanifest',
  '/js/app.js', '/js/api.js', '/js/ui.js', '/js/icons.js', '/js/store.js', '/js/notify.js',
  '/js/antibot.js', '/js/pow-worker.js', '/js/ideas.js', '/js/privacy.js', '/js/reels-prefetch.js',
  '/js/views/auth.js', '/js/views/home.js', '/js/views/messages.js', '/js/views/chat.js', '/js/views/profile.js', '/js/views/user.js', '/js/views/reels.js',
  '/fonts/plex-arabic-arabic-400.woff2', '/fonts/plex-arabic-arabic-500.woff2', '/fonts/plex-arabic-arabic-700.woff2',
  '/icons/icon.svg', '/icons/icon-192.png',
];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(VERSION).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== VERSION && k !== MEDIA_CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener('fetch', (event) => {
  const req = event.request;
  const url = new URL(req.url);
  // Never cache the API or anything cross-origin (Google sign-in etc.).
  if (req.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  if (url.pathname.startsWith('/media/')) {
    event.respondWith(serveMedia(req, url));
    return;
  }

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

// Reels media prefetched on this device: answer from the cache, including the Range
// requests <video> makes, so a prefetched reel starts without touching the network.
async function serveMedia(req, url) {
  let hit = null;
  try { hit = await (await caches.open(MEDIA_CACHE)).match(url.pathname); } catch { /* cache unavailable */ }
  if (!hit) return fetch(req);
  const range = req.headers.get('range');
  if (!range) return hit;
  const blob = await hit.blob();
  const m = /^bytes=(\d*)-(\d*)$/.exec(range.trim());
  if (!m || (!m[1] && !m[2])) return new Response(blob, { headers: { 'Content-Type': blob.type, 'Accept-Ranges': 'bytes' } });
  let start; let end;
  if (m[1]) { start = Number(m[1]); end = m[2] ? Math.min(Number(m[2]), blob.size - 1) : blob.size - 1; }
  else { start = Math.max(0, blob.size - Number(m[2])); end = blob.size - 1; }
  if (start >= blob.size || start > end) {
    return new Response(null, { status: 416, headers: { 'Content-Range': `bytes */${blob.size}` } });
  }
  return new Response(blob.slice(start, end + 1, blob.type), {
    status: 206,
    headers: {
      'Content-Type': blob.type, 'Accept-Ranges': 'bytes', 'Content-Length': String(end - start + 1),
      'Content-Range': `bytes ${start}-${end}/${blob.size}`,
    },
  });
}

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
