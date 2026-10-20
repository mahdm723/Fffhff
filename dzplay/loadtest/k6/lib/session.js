// V6 phase 21: what a member does with the app, in the order the real app makes its requests (static/js).
// A session = open the app (+ its WebSocket for the whole session) → 4–14 actions with 3–10 s of reading between
// them → close. The socket's "sync" signals trigger the same /api/sync calls as in the app.
import { WebSocket } from 'k6/experimental/websockets';
import { Counter } from 'k6/metrics';
import { setTimeout } from 'k6/timers';
import crypto from 'k6/crypto';
import {
  BASE, PROFILE, WS_URL, attest, call, cookieHeader, delay, headers, identity, json, loadPictures, pick, rand, randInt,
  think, useSession, weighted, wsConnectMs,
} from './client.js';

const wsOpened = new Counter('ws_opened');
const wsDropped = new Counter('ws_dropped'); // closed by the server (or the network) during a session
const wsFailed = new Counter('ws_failed'); // could not connect
const wsMessages = new Counter('ws_messages');
const sessions = new Counter('sessions');

const SYNC_OVERLAP_MS = 5000;
const COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'TON', 'DOGE', 'SUI'];
const SEARCH = ['أحمد', 'محمد', 'سارة', 'نور', 'كريم', 'Yacine', 'Sarah', 'Trader', 'بن', 'Amine', 'ليلى', 'عمر'];
const TEXTS = ['تحليل جيد', 'أتفق معك', 'ما رأيكم في {c}؟', 'شكرًا على المشاركة', 'انتبهوا لوقف الخسارة', 'Good point',
  'Merci', 'متابع'];
const ACTIONS_LIST = Object.keys(PROFILE.actions);
let attested = false;
let shell = null; // the app's files, from /sw.js (cold opens)

const text = () => `${pick(TEXTS).replace('{c}', pick(COINS))} ${randInt(1, 9999)}`;
const clientId = () => `lt-${Date.now().toString(36)}-${Math.floor(Math.random() * 1e9).toString(36)}`;

// ------------------------------------------------------------------------------------------- sync + socket
async function sync(s, full = false) {
  if (s.syncing) { s.syncAgain = true; return; }
  s.syncing = true;
  try {
    const since = !full && s.serverTime ? new Date(Date.parse(s.serverTime) - SYNC_OVERLAP_MS).toISOString() : null;
    const d = json(await call(s.id, 'GET', '/api/sync' + (since ? `?since=${encodeURIComponent(since)}` : ''), undefined, 'GET /api/sync'));
    if (d) { s.serverTime = d.server_time; s.conversations = d.conversations || s.conversations; }
  } finally {
    s.syncing = false;
    if (s.syncAgain && !s.closed) { s.syncAgain = false; await sync(s); }
  }
}

function scheduleSync(s, ms) {
  if (s.syncTimer || s.closed) return;
  s.syncTimer = true;
  setTimeout(() => { s.syncTimer = false; if (!s.closed) sync(s).catch(() => {}); }, ms);
}

function openSocket(s) {
  const t0 = Date.now();
  let ws;
  try {
    ws = new WebSocket(WS_URL, null, { headers: headers(s.id, { Origin: BASE, Cookie: cookieHeader() }), tags: { name: 'WS /api/ws' } });
  } catch (e) { wsFailed.add(1); return null; }
  ws.onopen = () => { s.wsOpen = true; wsOpened.add(1); wsConnectMs.add(Date.now() - t0); sync(s).catch(() => {}); };
  ws.onmessage = (e) => {
    wsMessages.add(1);
    let m;
    try { m = JSON.parse(e.data); } catch { return; }
    if (m.type !== 'sync') return; // hello, ping, typing
    if (['comment', 'account', 'support', 'notify'].includes(m.reason)) return; // a badge only
    scheduleSync(s, m.reason === 'message' ? 0 : 150);
  };
  ws.onerror = () => { if (!s.wsOpen) wsFailed.add(1); };
  ws.onclose = () => { if (s.wsOpen && !s.closed) wsDropped.add(1); s.wsOpen = false; };
  return ws;
}

// ------------------------------------------------------------------------------------------- opening the app
async function coldOpen(s) {
  const page = await call(s.id, 'GET', '/', undefined, 'GET /', 'static');
  if (!shell) {
    const sw = await call(s.id, 'GET', '/sw.js', undefined, 'GET /sw.js', 'static');
    const list = (sw.body || '').match(/const SHELL = \[([\s\S]*?)\];/);
    shell = list ? [...list[1].matchAll(/'([^']+)'/g)].map((m) => m[1]).filter((p) => p !== '/') : [];
  }
  for (let i = 0; i < shell.length; i += 6) { // a browser loads ~6 files at a time per host
    await Promise.all(shell.slice(i, i + 6).map((p) => call(s.id, 'GET', p, undefined, 'GET static', 'static')));
  }
  await call(s.id, 'GET', `/api/i18n/${s.id.lang}`, undefined, 'GET /api/i18n/{lang}');
  return page.status;
}

async function openApp(s, cold) {
  if (cold) await coldOpen(s);
  await call(s.id, 'GET', '/api/config', undefined, 'GET /api/config');
  const me = json(await call(s.id, 'GET', '/api/me', undefined, 'GET /api/me'));
  s.me = me;
  s.ws = openSocket(s);
  await Promise.all([feedPage(s, true), sync(s, true)]);
}

// ------------------------------------------------------------------------------------------- actions
async function feedPage(s, first) {
  const qs = !first && s.cursor ? `?cursor=${encodeURIComponent(s.cursor)}` : '';
  const d = json(await call(s.id, 'GET', `/api/posts/feed${qs}`, undefined, 'GET /api/posts/feed'));
  if (!d) return;
  s.cursor = d.next_cursor;
  s.posts = (d.posts || []).filter((p) => p && p.id);
  await loadPictures(s.id, d.posts);
}

async function openPost(s) {
  const p = pick(s.posts || []);
  if (!p) return feedPage(s, true);
  const [post, comments] = await Promise.all([
    call(s.id, 'GET', `/api/posts/${p.id}`, undefined, 'GET /api/posts/{post_id}'),
    call(s.id, 'GET', `/api/posts/${p.id}/comments`, undefined, 'GET /api/posts/{post_id}/comments'),
  ]);
  const c = json(comments);
  s.openPost = p.id;
  s.comments = (c && c.comments) || [];
  await loadPictures(s.id, [json(post), c]);
}

const ACTIONS = {
  feed_more: (s) => feedPage(s, !s.cursor),
  open_post: openPost,
  react: async (s) => {
    const p = pick(s.posts || []);
    if (!p) return feedPage(s, true);
    const r = Math.random();
    await call(s.id, 'PUT', `/api/posts/${p.id}/reaction`, { reaction: r < 0.85 ? 'like' : r < 0.95 ? 'dislike' : null },
      'PUT /api/posts/{post_id}/reaction');
  },
  comment: async (s) => {
    if (!s.openPost) await openPost(s);
    if (!s.openPost) return;
    await think();
    await call(s.id, 'POST', `/api/posts/${s.openPost}/comments`, { content: text(), client_id: clientId() },
      'POST /api/posts/{post_id}/comments');
  },
  reply: async (s) => {
    if (!s.comments || !s.comments.length) await openPost(s);
    const parent = pick(s.comments || []);
    if (!parent) return;
    await think();
    await call(s.id, 'POST', `/api/posts/${s.openPost}/comments`, { content: text(), client_id: clientId(), parent_id: parent.id },
      'POST /api/posts/{post_id}/comments');
  },
  new_idea: async (s) => {
    await delay(rand(10, 30) * 1000); // writing an idea takes a while
    const r = await call(s.id, 'POST', '/api/posts', { content: `${text()} — ${pick(COINS)} ${randInt(100, 90000)}`, client_id: clientId() },
      'POST /api/posts');
    if (r.status === 201) await feedPage(s, true);
  },
  profile: async (s) => {
    const p = pick((s.posts || []).filter((x) => x.author && x.author.public_id));
    if (!p) return;
    const person = json(await call(s.id, 'GET', `/api/people/${p.author.public_id}`, undefined, 'GET /api/people/{public_id}'));
    if (!person || !person.profile_ref) return;
    const ref = person.profile_ref;
    const [prof, posts] = await Promise.all([
      call(s.id, 'GET', `/api/profiles/${ref}`, undefined, 'GET /api/profiles/{ref}'),
      call(s.id, 'GET', `/api/profiles/${ref}/posts`, undefined, 'GET /api/profiles/{ref}/posts'),
    ]);
    await loadPictures(s.id, [json(prof), json(posts)]);
  },
  users_page: async (s) => {
    const d = json(await call(s.id, 'GET', '/api/people', undefined, 'GET /api/people'));
    await loadPictures(s.id, d);
    if (d && d.next_cursor && Math.random() < 0.3) {
      await think();
      await loadPictures(s.id, json(await call(s.id, 'GET', `/api/people?cursor=${encodeURIComponent(d.next_cursor)}`, undefined, 'GET /api/people')));
    }
  },
  search: async (s) => {
    const q = pick(SEARCH);
    await loadPictures(s.id, json(await call(s.id, 'GET', `/api/people/search?q=${encodeURIComponent(q)}&page=1`, undefined, 'GET /api/people/search')));
  },
  notifications: async (s) => {
    const d = json(await call(s.id, 'GET', '/api/notifications', undefined, 'GET /api/notifications'));
    await loadPictures(s.id, d);
    await call(s.id, 'POST', '/api/notifications/read', {}, 'POST /api/notifications/read');
  },
  market: async (s) => { await call(s.id, 'GET', '/api/market', undefined, 'GET /api/market'); },
  money_pages: async (s) => {
    if (Math.random() < 0.5) await call(s.id, 'GET', '/api/membership', undefined, 'GET /api/membership');
    else await call(s.id, 'GET', '/api/rewards', undefined, 'GET /api/rewards');
  },
  chat: async (s) => {
    const list = json(await call(s.id, 'GET', '/api/conversations', undefined, 'GET /api/conversations'));
    const usable = ((list && list.conversations) || []).filter((c) => !(c.request && c.request.state === 'pending' && !c.request.incoming));
    const c = pick(usable);
    if (!c) return;
    await call(s.id, 'GET', `/api/conversations/${c.id}?limit=50`, undefined, 'GET /api/conversations/{conversation_id}');
    await call(s.id, 'POST', `/api/conversations/${c.id}/read`, {}, 'POST /api/conversations/{conversation_id}/read');
    for (let i = randInt(1, 3); i > 0; i--) {
      if (s.ws && s.wsOpen) s.ws.send(JSON.stringify({ type: 'typing', conversation_id: c.id, on: true }));
      await delay(rand(2, 6) * 1000); // typing
      await call(s.id, 'POST', `/api/conversations/${c.id}/messages`, { content: text(), client_id: clientId() },
        'POST /api/conversations/{conversation_id}/messages');
    }
  },
  rare: async (s) => {
    const what = weighted(PROFILE.rare);
    if (what === 'login') return login(s.id, s.id.row.email);
    if (what === 'membership_request') {
      const m = json(await call(s.id, 'GET', '/api/membership', undefined, 'GET /api/membership'));
      if (m && !m.member && !(m.request && m.request.status === 'pending')) {
        await call(s.id, 'POST', '/api/membership/requests', { txid: crypto.sha256(clientId(), 'hex') }, 'POST /api/membership/requests');
      }
      return;
    }
    const r = json(await call(s.id, 'GET', '/api/rewards', undefined, 'GET /api/rewards'));
    const w = r && r.withdraw;
    if (r && w && r.balances && r.balances.available >= (w.min || 10)) {
      await call(s.id, 'POST', '/api/rewards/withdraw/code', {}, 'POST /api/rewards/withdraw/code');
    }
  },
};
for (const a of ACTIONS_LIST) { if (!ACTIONS[a]) throw new Error(`profile.json: unknown action ${a}`); }

// ------------------------------------------------------------------------------------------- auth (also used by scenario G)
export function solvePow(ch) {
  for (let n = 0; n <= ch.maxnumber; n++) {
    if (crypto.sha256(`${ch.salt}${n}`, 'hex') === ch.challenge) return { ...ch, number: n };
  }
  return null;
}

/** Password login with the proof of work (the server checks it and the Argon2 hash, like for a real phone). */
export async function login(id, email) {
  const ch = json(await call(id, 'POST', '/api/auth/challenge', { purpose: 'login' }, 'POST /api/auth/challenge', 'auth'));
  if (!ch) return false;
  const res = await call(id, 'POST', '/api/auth/login', { email, password: __ENV.LT_PASSWORD || 'LoadTest-Pass-2026!', antibot: solvePow(ch) },
    'POST /api/auth/login', 'auth');
  return res.status === 200;
}

// ------------------------------------------------------------------------------------------- the session
export async function memberSession() {
  const id = identity();
  if (!attested) attested = await attest(id);
  useSession(id, id.row.token);
  const s = { id, posts: [], conversations: [], closed: false, wsOpen: false };
  sessions.add(1);
  await openApp(s, Math.random() * 100 < PROFILE.cold_open_pct);
  const n = randInt(...PROFILE.actions_per_session);
  for (let i = 0; i < n; i++) {
    await think();
    await ACTIONS[weighted(PROFILE.actions)](s);
  }
  s.closed = true;
  if (s.ws) { try { s.ws.close(); } catch { /* already closed */ } }
  await delay(rand(...PROFILE.pause_between_sessions_seconds) * 1000); // the app is closed for a moment
}
