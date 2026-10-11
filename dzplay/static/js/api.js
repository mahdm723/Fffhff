// Thin fetch wrapper. Session lives in an HttpOnly cookie (never readable from JS).
import { lang, t } from './i18n.js';

export class ApiError extends Error {
  constructor(status, code, message, retryAfter = null) {
    super(message);
    this.status = status;
    this.code = code;
    this.retryAfter = retryAfter;
  }
  get isNetwork() { return this.code === 'network'; }
  get isRetryable() { return this.isNetwork || this.status >= 500; }
}

// Requests the user is waiting for (messages, ideas, sign-in…). Background media
// prefetch watches this and pauses while any are in flight.
let inflight = 0;
const idleWaiters = [];
export function apiBusy() { return inflight > 0; }
export function whenApiIdle() {
  return inflight === 0 ? Promise.resolve() : new Promise((resolve) => idleWaiters.push(resolve));
}

export async function request(method, path, body) {
  inflight += 1;
  try {
    return await doRequest(method, path, body);
  } finally {
    inflight -= 1;
    if (inflight === 0) idleWaiters.splice(0).forEach((fn) => fn());
  }
}

async function doRequest(method, path, body) {
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-DZ-Requested': '1', Accept: 'application/json', 'X-DZ-Lang': lang() },
    cache: 'no-store',
  };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError(0, 'network', t('لا يوجد اتصال بالإنترنت.'));
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    if (res.status === 403 && err.code === 'app_required') leaveForGate();
    throw new ApiError(res.status, err.code || 'error', err.message || t('حدث خطأ. حاول مرة أخرى.'), err.retry_after ?? null);
  }
  return data;
}

// V6 phase 14: this client may not use the app (an Android browser, an old or unproven app, a computer). Our app
// proves itself again (its access cookie may have expired); anything else reloads into the server's access page,
// without the offline copy that would otherwise keep showing the app.
let leaving = false;
function leaveForGate() {
  if (leaving) return;
  leaving = true;
  const native = window.DZPLAYAndroid;
  if (native && typeof native.refreshAccess === 'function') { native.refreshAccess(); return; }
  const unregister = 'serviceWorker' in navigator
    ? navigator.serviceWorker.getRegistrations().then((regs) => Promise.all(regs.map((r) => r.unregister())))
    : Promise.resolve();
  const clear = window.caches ? caches.keys().then((keys) => Promise.all(keys.map((k) => caches.delete(k)))) : Promise.resolve();
  Promise.all([unregister, clear]).catch(() => {}).finally(() => location.replace('/'));
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b = {}) => request('POST', p, b),
  put: (p, b = {}) => request('PUT', p, b),
  patch: (p, b = {}) => request('PATCH', p, b),
  del: (p) => request('DELETE', p),
};
