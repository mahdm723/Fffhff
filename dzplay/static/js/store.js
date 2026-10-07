// Client state: local history cache, server sync, offline outbox, realtime.
//
// The server keeps messages only temporarily (TTL). The device keeps its own
// copy of each conversation so the history stays visible after the server
// copy expired, for as long as the conversation itself exists.

import { api } from './api.js';
import { newClientId } from './ui.js';

const CACHE_KEY = 'dz:cache:v1';
const MAX_PER_CONV = 300;
const SYNC_OVERLAP_MS = 5000;
const POLL_MS = 15000;

export const state = {
  me: null,
  config: null,
  conversations: [],      // summaries from the server (already anonymised)
  messages: {},           // conversationId -> [message]
  serverTime: null,
  outbox: [],             // messages waiting to be sent (offline / retry)
  online: navigator.onLine,
  connected: false,
};

const listeners = new Set();
export function subscribe(fn) { listeners.add(fn); return () => listeners.delete(fn); }
function emit(type, detail) { for (const fn of [...listeners]) { try { fn(type, detail); } catch (e) { console.error(e); } } }

// ---------------------------------------------------------------- cache
let saveTimer = null;
function writeCache() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!state.me) return;
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({
      conversations: state.conversations, messages: state.messages, serverTime: state.serverTime, outbox: state.outbox,
    }));
  } catch { /* storage full or disabled: the app still works online */ }
}
function save() {
  clearTimeout(saveTimer);
  saveTimer = setTimeout(writeCache, 250);
}
// Never lose the latest messages / outbox when the app is closed or backgrounded.
window.addEventListener('pagehide', () => { if (saveTimer) writeCache(); });
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'hidden' && saveTimer) writeCache(); });

export function loadCache() {
  try {
    const raw = JSON.parse(localStorage.getItem(CACHE_KEY) || 'null');
    if (raw) {
      state.conversations = raw.conversations || [];
      state.messages = raw.messages || {};
      state.serverTime = raw.serverTime || null;
      // V6: queued random messages ("new") cannot be sent any more; only replies stay in the outbox
      state.outbox = (raw.outbox || []).filter((i) => i.kind === 'reply')
        .map((i) => ({ ...i, state: i.state === 'failed' ? 'failed' : 'pending' }));
    }
  } catch { /* corrupted cache */ }
}

export function clearCache() {
  clearTimeout(saveTimer);
  saveTimer = null;
  state.conversations = []; state.messages = {}; state.serverTime = null; state.outbox = [];
  try { localStorage.removeItem(CACHE_KEY); } catch { /* ignore */ }
}

// ---------------------------------------------------------------- selectors
export const getConversation = (id) => state.conversations.find((c) => c.id === id) || null;
export const getMessages = (id) => state.messages[id] || [];
export const isRequest = (c) => !!(c && c.request && c.request.pending_for_me);
export const unreadTotal = () => state.conversations.reduce((n, c) => n + (c.unread && !c.muted ? c.unread : 0), 0);
export const requestCount = () => state.conversations.filter(isRequest).length;

function upsertConversation(conv) {
  const i = state.conversations.findIndex((c) => c.id === conv.id);
  if (i >= 0) state.conversations[i] = conv; else state.conversations.unshift(conv);
  state.conversations.sort((a, b) => (b.last_message_at || '').localeCompare(a.last_message_at || ''));
}

function mergeMessages(cid, incoming) {
  if (!incoming.length) return;
  const list = (state.messages[cid] || []).slice();
  for (const m of incoming) {
    const i = list.findIndex((x) => x.id === m.id || (m.client_id && x.client_id === m.client_id));
    if (i >= 0) list[i] = { ...list[i], ...m }; else list.push(m);
  }
  list.sort((a, b) => a.created_at.localeCompare(b.created_at) || a.id.localeCompare(b.id));
  state.messages[cid] = list.slice(-MAX_PER_CONV);
}

function applyPeerRead(conv) {
  if (!conv.peer_read_at) return;
  for (const m of state.messages[conv.id] || []) {
    if (m.mine && m.status !== 'read' && !m.id.startsWith('local:') && m.created_at <= conv.peer_read_at) m.status = 'read';
  }
}

// ---------------------------------------------------------------- sync
let syncing = null;
let syncAgain = false;

export async function sync({ full = false } = {}) {
  if (syncing) { syncAgain = true; return syncing; }
  syncing = (async () => {
    try {
      const since = !full && state.serverTime ? new Date(Date.parse(state.serverTime) - SYNC_OVERLAP_MS).toISOString() : null;
      const data = await api.get('/api/sync' + (since ? `?since=${encodeURIComponent(since)}` : ''));
      const known = new Set(Object.values(state.messages).flat().map((m) => m.id));
      state.conversations = data.conversations;
      const alive = new Set(data.conversations.map((c) => c.id));
      for (const cid of Object.keys(state.messages)) if (!alive.has(cid)) delete state.messages[cid];

      const byConv = {};
      for (const m of data.messages) (byConv[m.conversation_id] ||= []).push(m);
      for (const [cid, list] of Object.entries(byConv)) mergeMessages(cid, list);
      for (const c of state.conversations) applyPeerRead(c);
      state.serverTime = data.server_time;
      const muted = new Set(state.conversations.filter((c) => c.muted).map((c) => c.id));
      const incoming = since
        ? data.messages.filter((m) => !m.mine && !known.has(m.id) && m.kind !== 'system' && !muted.has(m.conversation_id)).length : 0;
      save();
      emit('sync');
      if (incoming) emit('incoming', incoming);
    } catch (err) {
      if (err.status === 401) emit('unauthorized');
      else throw err;
    } finally {
      syncing = null;
      if (syncAgain) { syncAgain = false; sync().catch(() => {}); }
    }
  })();
  return syncing;
}

let syncTimer = null;
export function scheduleSync(delay = 120) {
  clearTimeout(syncTimer);
  syncTimer = setTimeout(() => sync().catch(() => {}), delay);
}

export async function loadConversation(cid) {
  const data = await api.get(`/api/conversations/${encodeURIComponent(cid)}?limit=50`);
  upsertConversation(data.conversation);
  mergeMessages(cid, data.messages);
  applyPeerRead(data.conversation);
  save();
  emit('sync');
  return data;
}

const readInFlight = new Set();
export async function markRead(cid) {
  const conv = getConversation(cid);
  if (!conv || !conv.unread || readInFlight.has(cid)) return;
  readInFlight.add(cid);
  conv.unread = 0;
  emit('sync');
  try { await api.post(`/api/conversations/${encodeURIComponent(cid)}/read`); } catch { /* next sync fixes it */ }
  finally { readInFlight.delete(cid); }
  save();
}

// ---------------------------------------------------------------- sending
/** First message to someone found by their public ID (starts a message request). */
export async function sendDirect(publicId, content) {
  const res = await api.post(`/api/people/${encodeURIComponent(publicId)}/messages`, { content, client_id: newClientId() });
  applySent(res);
  return res.conversation;
}

export async function answerRequest(cid, action) {
  await api.post(`/api/conversations/${encodeURIComponent(cid)}/request`, { action });
  if (action === 'ignore') removeConversationLocally(cid);
  else await loadConversation(cid);
}

/** Apply a local change to a conversation summary (mute) without waiting for a sync. */
export function patchConversation(cid, patch) {
  const c = getConversation(cid);
  if (!c) return;
  Object.assign(c, patch);
  save();
  emit('sync');
}

function applySent(res) {
  if (res.conversation) upsertConversation(res.conversation);
  if (res.message) mergeMessages(res.message.conversation_id, [res.message]);
  save();
  emit('sync');
}

export function sendReply(cid, content) {
  const client_id = newClientId();
  const now = new Date().toISOString();
  mergeMessages(cid, [{ id: `local:${client_id}`, conversation_id: cid, mine: true, author: 'me', content, created_at: now, status: 'pending', client_id }]);
  state.outbox.push({ kind: 'reply', conversation_id: cid, client_id, content, created_at: now, state: 'pending' });
  save();
  emit('sync');
  flushOutbox();
}

let flushing = false;
export async function flushOutbox() {
  if (flushing) return;
  flushing = true;
  try {
    for (const item of [...state.outbox]) {
      if (item.state !== 'pending') continue;
      try {
        const res = await api.post(`/api/conversations/${encodeURIComponent(item.conversation_id)}/messages`,
          { content: item.content, client_id: item.client_id });
        state.outbox = state.outbox.filter((i) => i !== item);
        applySent(res);
      } catch (err) {
        if (err.isRetryable) break; // still offline / server down: keep order, retry later
        item.state = 'failed';
        item.error = err.message;
        mergeMessages(item.conversation_id, [{ id: `local:${item.client_id}`, client_id: item.client_id, status: 'failed', error: err.message, created_at: item.created_at }]);
        save();
        emit('sync');
      }
    }
  } finally {
    flushing = false;
  }
}

export function retryMessage(cid, clientId) {
  const item = state.outbox.find((i) => i.client_id === clientId);
  if (!item) return;
  item.state = 'pending';
  mergeMessages(cid, [{ id: `local:${clientId}`, client_id: clientId, status: 'pending', error: null, created_at: item.created_at }]);
  emit('sync');
  flushOutbox();
}

export function discardMessage(cid, clientId) {
  state.outbox = state.outbox.filter((i) => i.client_id !== clientId);
  state.messages[cid] = (state.messages[cid] || []).filter((m) => m.client_id !== clientId || !m.id.startsWith('local:'));
  save();
  emit('sync');
}

export function removeConversationLocally(cid) {
  state.conversations = state.conversations.filter((c) => c.id !== cid);
  delete state.messages[cid];
  state.outbox = state.outbox.filter((i) => i.conversation_id !== cid);
  save();
  emit('sync');
}

// ---------------------------------------------------------------- realtime
let ws = null;
let backoff = 1000;
let reconnectTimer = null;
let pollTimer = null;
let started = false;

function connect() {
  clearTimeout(reconnectTimer);
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/api/ws`);
  ws.onopen = () => {
    backoff = 1000;
    state.connected = true;
    emit('connection');
    sync().catch(() => {});
    flushOutbox();
  };
  ws.onmessage = (e) => {
    let msg;
    try { msg = JSON.parse(e.data); } catch { return; }
    if (msg.type === 'typing') { emit('typing', msg); return; }
    if (msg.type !== 'sync') { if (msg.type && msg.type !== 'ping' && msg.type !== 'hello') emit('ws', msg); return; }
    if (msg.reason === 'comment') emit('comment'); // ideas: no message sync needed
    else if (msg.reason === 'media') { emit('media'); scheduleSync(150); } // a chat picture was opened / expired
    else if (['account', 'support', 'notify'].includes(msg.reason)) emit(msg.reason); // V5 + V6 notifications
    else scheduleSync(msg.reason === 'message' ? 0 : 150);
  };
  ws.onclose = () => {
    ws = null;
    if (state.connected) { state.connected = false; emit('connection'); }
    if (!started) return;
    reconnectTimer = setTimeout(connect, backoff + Math.random() * 500);
    backoff = Math.min(backoff * 2, 30000);
  };
  ws.onerror = () => { try { ws.close(); } catch { /* ignore */ } };
}

/** Small JSON message to the server over the socket (typing). */
export function sendWS(obj) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return false;
  try { ws.send(JSON.stringify(obj)); return true; } catch { return false; }
}

export function sendTyping(cid, on) { return sendWS({ type: 'typing', conversation_id: cid, on: !!on }); }

function onOnline() { state.online = true; emit('connection'); backoff = 1000; connect(); flushOutbox(); scheduleSync(0); }
function onOffline() { state.online = false; emit('connection'); }
function onVisible() { if (document.visibilityState === 'visible') { connect(); scheduleSync(0); } }

export function startRealtime() {
  if (started) return;
  started = true;
  connect();
  window.addEventListener('online', onOnline);
  window.addEventListener('offline', onOffline);
  document.addEventListener('visibilitychange', onVisible);
  // Fallback when WebSockets are blocked (some proxies): poll while disconnected.
  pollTimer = setInterval(() => { if (!state.connected && navigator.onLine) { sync().catch(() => {}); flushOutbox(); } }, POLL_MS);
}

export function stopRealtime() {
  started = false;
  clearTimeout(reconnectTimer);
  clearInterval(pollTimer);
  window.removeEventListener('online', onOnline);
  window.removeEventListener('offline', onOffline);
  document.removeEventListener('visibilitychange', onVisible);
  if (ws) { try { ws.close(); } catch { /* ignore */ } ws = null; }
  state.connected = false;
}
