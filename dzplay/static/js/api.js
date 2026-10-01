// Thin fetch wrapper. Session lives in an HttpOnly cookie (never readable from JS).

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

export async function request(method, path, body) {
  const init = {
    method,
    credentials: 'same-origin',
    headers: { 'X-DZ-Requested': '1', Accept: 'application/json' },
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
    throw new ApiError(0, 'network', 'لا يوجد اتصال بالإنترنت.');
  }
  let data = null;
  try { data = await res.json(); } catch { /* empty body */ }
  if (!res.ok) {
    const err = (data && data.error) || {};
    throw new ApiError(res.status, err.code || 'error', err.message || 'حدث خطأ. حاول مرة أخرى.', err.retry_after ?? null);
  }
  return data;
}

export const api = {
  get: (p) => request('GET', p),
  post: (p, b = {}) => request('POST', p, b),
  del: (p) => request('DELETE', p),
};
