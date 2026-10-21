// V6 phase 21: one virtual user = one phone of a seeded member (session token from out/vu_users.csv).
// Every request carries the load-test token and the user's own synthetic address (X-Loadtest-IP): the server applies
// its per-address limits per user, as with real phones, although all of them come from one generator.
import http from 'k6/http';
import exec from 'k6/execution';
import { SharedArray } from 'k6/data';
import { Counter, Trend } from 'k6/metrics';
import { setTimeout } from 'k6/timers';

export const BASE = (__ENV.BASE_URL || 'https://staging.test:8443').replace(/\/$/, '');
export const WS_URL = BASE.replace(/^http/, 'ws') + '/api/ws';
export const MOCKS = (__ENV.MOCKS_URL || 'http://mocks:9000').replace(/\/$/, '');
const TOKEN = __ENV.LOADTEST_TOKEN || '';
const VERSION_CODE = Number(__ENV.VERSION_CODE || 8);
export const PROFILE = JSON.parse(open(__ENV.PROFILE || '../profile.json'));

export const USERS = new SharedArray('vu_users', () => {
  const lines = open(__ENV.VU_USERS || '../../out/vu_users.csv').trim().split('\n');
  const head = lines.shift().split(',');
  return lines.map((l) => Object.fromEntries(l.split(',').map((v, i) => [head[i], v])));
});
export const META = JSON.parse(open(__ENV.SEED_META || '../../out/meta.json'));

const ANDROID_UA = 'Mozilla/5.0 (Linux; Android 14; SM-A546B; wv) AppleWebKit/537.36 (KHTML, like Gecko) '
  + 'Version/4.0 Chrome/130.0.0.0 Mobile Safari/537.36 DZPLAYApp/3.2.0';
const IPHONE_UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) '
  + 'Version/18.1 Mobile/15E148 Safari/604.1';

// what the report counts besides k6's own metrics
export const refused = new Counter('refused_4xx'); // 4xx other than 429: the model asked for something not allowed
export const limited = new Counter('rate_limited_429');
export const failures = new Counter('server_errors'); // 5xx, timeouts, resets
export const busy = new Counter('busy_503'); // "busy, try again in a moment" (the password-hash gate)

// ------------------------------------------------------------------------------------------- small helpers
export const rand = (a, b) => a + Math.random() * (b - a);
export const randInt = (a, b) => Math.floor(rand(a, b + 1));
export const pick = (list) => list[Math.floor(Math.random() * list.length)];
export const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
export const think = () => delay(rand(...PROFILE.think_seconds) * 1000);
export function weighted(weights) {
  const entries = Object.entries(weights);
  let r = Math.random() * entries.reduce((s, [, w]) => s + w, 0);
  for (const [key, w] of entries) { if ((r -= w) <= 0) return key; }
  return entries[0][0];
}
export function hash32(n) { // stable per VU: who is on Android, who on iPhone
  let x = (n ^ 0x9e3779b9) >>> 0;
  x = Math.imul(x ^ (x >>> 16), 0x85ebca6b) >>> 0;
  return (x ^ (x >>> 13)) >>> 0;
}
export const ipFor = (n, first = 10) => `${first}.${(n >> 16) & 255}.${(n >> 8) & 255}.${n & 255}`;

// "level" tag: which part of the scenario a request belongs to (per-level percentiles in the summary)
let levelOf = () => 'all';
let offsets = {}; // scenarios that start later than the first one (seconds)
export function setLevels(fn, startOffsets = {}) { levelOf = fn; offsets = startOffsets; }
export function level() {
  const startedAt = exec.scenario.startTime;
  return levelOf(startedAt ? (Date.now() - startedAt) / 1000 + (offsets[exec.scenario.name] || 0) : 0);
}

// ------------------------------------------------------------------------------------------- identity
export function identity(vu = exec.vu.idInTest) {
  const row = USERS[(vu - 1) % USERS.length];
  return {
    vu, row, lang: row.lang || 'ar',
    android: hash32(vu) % 100 < PROFILE.android_pct,
    ip: ipFor(vu),
    device: `vu-${vu}`,
  };
}

export function headers(id, extra = {}) {
  return Object.assign({
    'User-Agent': id.android ? ANDROID_UA : IPHONE_UA,
    'X-Loadtest-Token': TOKEN,
    'X-Loadtest-IP': id.ip,
    'X-DZ-Requested': '1',
    'X-DZ-Lang': id.lang,
    Accept: 'application/json',
  }, extra);
}

export function useSession(id, token) {
  http.cookieJar().set(BASE, 'dz_session', token, { path: '/', secure: true });
}

export function cookieHeader() {
  const jar = http.cookieJar().cookiesForURL(BASE);
  return Object.entries(jar).map(([k, v]) => `${k}=${v[0]}`).join('; ');
}

let debugLeft = Number(__ENV.LT_DEBUG || 0); // LT_DEBUG=N: print the first N refusals of each user
function account(res, name) {
  const s = res.status;
  if (s === 503 && String(res.body || '').includes('"busy"')) busy.add(1, { name });
  if (s === 0 || s >= 500) failures.add(1, { name, status: String(s) });
  else if (s === 429) limited.add(1, { name });
  else if (s >= 400) refused.add(1, { name, status: String(s) });
  if (s >= 400 && debugLeft > 0) {
    debugLeft -= 1;
    console.warn(`${s} ${name}: ${String(res.body || '').slice(0, 160)}`);
  }
}

/** One API call, timed and tagged like the server labels it (name = route template, kind = read | write | …). */
export async function call(id, method, path, body, name, kind) {
  const params = {
    headers: headers(id, body === undefined ? {} : { 'Content-Type': 'application/json', Origin: BASE }),
    tags: { name: name || `${method} ${path.split('?')[0]}`, kind: kind || (method === 'GET' ? 'read' : 'write'), level: level() },
    timeout: '30s',
  };
  const res = await http.asyncRequest(method, BASE + path, body === undefined ? null : JSON.stringify(body), params);
  account(res, params.tags.name);
  return res;
}

export function json(res) {
  if (!res || res.status < 200 || res.status >= 300) return null;
  try { return res.json(); } catch { return null; }
}

/** The Android app proves itself once per phone (here: the staging-only synthetic attestation → dz_app cookie). */
export async function attest(id) {
  if (!id.android) return true;
  const res = await call(id, 'POST', '/api/app/loadtest-attest', { device: id.device, version_code: VERSION_CODE },
    'POST /api/app/loadtest-attest', 'setup');
  return res.status === 200;
}

// ------------------------------------------------------------------------------------------- pictures
// The phone's HTTP cache: a signed picture URL stays the same for about an hour, so it is downloaded once.
const seenPictures = new Set();
export function picturesIn(value, out = []) {
  if (!value || typeof value !== 'object') return out;
  if (Array.isArray(value)) { for (const v of value) picturesIn(v, out); return out; }
  for (const [k, v] of Object.entries(value)) {
    if (typeof v === 'string' && (k === 'avatar_url' || k === 'url') && v.startsWith('/media/')) out.push(v);
    else if (v && typeof v === 'object') picturesIn(v, out);
  }
  return out;
}

export async function loadPictures(id, payload) {
  const fresh = [...new Set(picturesIn(payload))].filter((u) => !seenPictures.has(u)).slice(0, PROFILE.images_per_page);
  if (seenPictures.size > 3000) seenPictures.clear();
  for (const u of fresh) seenPictures.add(u);
  await Promise.all(fresh.map((u) => http.asyncRequest('GET', BASE + u, null, {
    headers: headers(id, { Accept: 'image/webp,image/*' }), tags: { name: 'GET /media/{asset_id}/{variant}', kind: 'media', level: level() },
    timeout: '30s',
  }).then((res) => account(res, 'GET /media/{asset_id}/{variant}'))));
}

export const wsConnectMs = new Trend('ws_connect_ms', true);
