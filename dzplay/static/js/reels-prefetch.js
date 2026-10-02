// Reels prefetch: make the first reels start instantly without slowing the rest of the app.
//
// * The server picks a random order per session; we load the first page as soon as the app
//   opens (whatever page the user is on) and keep it here for the Reels view.
// * Downloads run one at a time, with fetch priority "low", and pause between chunks while
//   any API request the user is waiting for (messages, ideas, sign-in) is in flight.
// * Save-Data or a slow connection (2G/3G) → posters/first images only, no videos.
// * Files go to an on-device cache (Cache Storage, capped at device_cache_mb, oldest out
//   first). The service worker answers <video>/<img> requests — including Range requests —
//   from that cache. It is cleared on logout.
// Limit (PWA / WebView): nothing can download while the app is fully closed; we prefetch
// while it is open, on any page.
import { api, apiBusy, whenApiIdle } from './api.js';

export const MEDIA_CACHE = 'dz-media-v1';
const INDEX_KEY = 'dz:media-index';
const DEFAULTS = { count: 3, ahead: 3, device_cache_mb: 200 };

const feed = { reels: [], cursor: null, done: false, loadedAt: 0, loading: null, settings: { ...DEFAULTS } };
const queue = [];
const queued = new Set();
let running = false;
let stopped = false;

export function feedState() { return feed; }

export function configure(cfg) {
  if (cfg && cfg.reels_prefetch) feed.settings = { ...DEFAULTS, ...cfg.reels_prefetch };
}

export function resetFeed() {
  Object.assign(feed, { reels: [], cursor: null, done: false, loadedAt: 0, loading: null });
}

/** Next page of the server-ordered feed (deduplicated); shared by the prefetcher and the view. */
export function loadMore() {
  if (feed.loading) return feed.loading;
  if (feed.done) return Promise.resolve([]);
  const qs = feed.cursor ? `?cursor=${encodeURIComponent(feed.cursor)}` : '';
  feed.loading = api.get(`/api/reels/feed${qs}`).then((data) => {
    const known = new Set(feed.reels.map((r) => r.id));
    const fresh = data.reels.filter((r) => !known.has(r.id));
    feed.reels.push(...fresh);
    feed.cursor = data.next_cursor;
    feed.done = !data.next_cursor;
    feed.loadedAt = Date.now();
    if (data.prefetch) feed.settings = { ...feed.settings, ...data.prefetch };
    return fresh;
  }).finally(() => { feed.loading = null; });
  return feed.loading;
}

// ------------------------------------------------------------------ network policy

export function liteMode() {
  const c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
  if (!c) return false;
  return !!c.saveData || ['slow-2g', '2g', '3g'].includes(c.effectiveType);
}

// ------------------------------------------------------------------ device cache index

function readIndex() {
  try { return JSON.parse(localStorage.getItem(INDEX_KEY) || '{}'); } catch { return {}; }
}
function writeIndex(idx) {
  try { localStorage.setItem(INDEX_KEY, JSON.stringify(idx)); } catch { /* storage full or blocked */ }
}
const keyOf = (url) => new URL(url, location.origin).pathname; // signature/expiry are not part of the key

export async function isCached(url) {
  if (!('caches' in window)) return false;
  try { return !!(await (await caches.open(MEDIA_CACHE)).match(keyOf(url))); } catch { return false; }
}

async function evict(cache, idx, capBytes) {
  let total = Object.values(idx).reduce((n, e) => n + (e.size || 0), 0);
  const oldest = Object.entries(idx).sort((a, b) => a[1].t - b[1].t);
  for (const [key, entry] of oldest) {
    if (total <= capBytes) break;
    await cache.delete(key);
    delete idx[key];
    total -= entry.size || 0;
  }
}

export async function clearMediaCache() {
  queue.length = 0;
  queued.clear();
  try { localStorage.removeItem(INDEX_KEY); } catch { /* ignore */ }
  if ('caches' in window) { try { await caches.delete(MEDIA_CACHE); } catch { /* ignore */ } }
}

/** Mark a cached file as just used (so eviction removes the least recently used first). */
export function touch(url) {
  const idx = readIndex();
  const key = keyOf(url);
  if (idx[key]) { idx[key].t = Date.now(); writeIndex(idx); }
}

// ------------------------------------------------------------------ downloader

async function download(url, type) {
  const key = keyOf(url);
  if (!('caches' in window)) return;
  const cache = await caches.open(MEDIA_CACHE);
  if (await cache.match(key)) return;
  await whenApiIdle();
  const res = await fetch(url, { credentials: 'same-origin', priority: 'low', cache: 'no-store' });
  if (!res.ok || !res.body) return;
  const reader = res.body.getReader();
  const chunks = [];
  let size = 0;
  for (;;) {
    if (apiBusy()) await whenApiIdle(); // back-pressure: let the user's requests have the bandwidth
    if (stopped) { reader.cancel().catch(() => {}); return; }
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    size += value.byteLength;
  }
  const blob = new Blob(chunks, { type: res.headers.get('content-type') || type });
  const idx = readIndex();
  await evict(cache, idx, feed.settings.device_cache_mb * 1024 * 1024 - size);
  await cache.put(key, new Response(blob, { headers: { 'Content-Type': blob.type, 'Content-Length': String(size) } }));
  idx[key] = { size, t: Date.now() };
  writeIndex(idx);
}

async function pump() {
  if (running) return;
  running = true;
  try {
    while (queue.length && !stopped) {
      const job = queue.shift();
      try { await download(job.url, job.type); } catch { /* network hiccup: skip, the player will fetch it */ }
      queued.delete(keyOf(job.url));
    }
  } finally {
    running = false;
  }
}

function enqueue(url, type, urgent = false) {
  if (!url) return;
  const key = keyOf(url);
  if (queued.has(key)) return;
  queued.add(key);
  (urgent ? queue.unshift.bind(queue) : queue.push.bind(queue))({ url, type });
}

/** Queue what reel #i needs: poster/first image always, the video only on good networks. */
function plan(reel, { urgent = false } = {}) {
  const lite = liteMode();
  const first = reel.media[0];
  if (!first) return;
  if (first.type === 'video') {
    enqueue(first.poster, 'image/jpeg', urgent);
    if (!lite) enqueue(first.src, 'video/mp4', urgent);
  } else {
    reel.media.slice(0, lite ? 1 : reel.media.length).forEach((m) => enqueue(m.src, 'image/webp', urgent));
  }
}

/** Called once the app is open (any page): first page + the first N reels in the background. */
export async function startPrefetch(cfg) {
  stopped = false;
  configure(cfg);
  try {
    if (!feed.reels.length) await loadMore();
  } catch { return; }
  feed.reels.slice(0, feed.settings.count).forEach((r) => plan(r));
  pump();
}

/** While watching: keep the current reel and the next `ahead` ones ready; fetch more pages early. */
export function prefetchAround(index) {
  const cur = feed.reels[index];
  if (cur) plan(cur, { urgent: true });
  feed.reels.slice(index + 1, index + 1 + feed.settings.ahead).forEach((r) => plan(r));
  pump();
  if (!feed.done && index >= feed.reels.length - 4) loadMore().then(() => prefetchAround(index)).catch(() => {});
}

export function stopPrefetch() {
  stopped = true;
  queue.length = 0;
  queued.clear();
}
