// V6 phase 21: scenario G — waves of sign-ups (with the e-mailed code, read from the mock mailbox) and password logins.
// Each sign-up is a new phone: its own synthetic address (172.16.0.0/12) and, on Android, its own attested device —
// so the per-address and per-device limits are those of real separate people.
import http from 'k6/http';
import exec from 'k6/execution';
import { Counter, Trend } from 'k6/metrics';
import { MOCKS, USERS, attest, call, delay, hash32, ipFor, json, pick } from './client.js';
import { login, solvePow } from './session.js';

const signupMs = new Trend('signup_total_ms', true);
const codeMs = new Trend('signup_code_delivery_ms', true);
const signups = new Counter('signups_done');
const signupFailed = new Counter('signups_failed');
const logins = new Counter('logins_done');
const loginFailed = new Counter('logins_failed');

const FIRST = ['Sami', 'Rania', 'Walid', 'Nadia', 'Hakim', 'Lyna', 'Anis', 'Imane', 'سليم', 'رانيا', 'هشام', 'إيناس'];
const LAST = ['Trader', 'Saidi', 'Kaci', 'Larbi', 'Amrani', 'بوزيد', 'منصور', 'قاسمي'];

function newPhone() {
  const n = exec.scenario.iterationInTest + 1 + 1000 * exec.vu.idInTest;
  return { vu: exec.vu.idInTest, lang: 'ar', android: hash32(n) % 100 < 70, ip: ipFor(n % (1 << 20), 172), device: `sg-${n}`, row: {} };
}

async function code(email) {
  const t0 = Date.now();
  for (let i = 0; i < 60; i++) {
    const res = await http.asyncRequest('GET', `${MOCKS}/mail/code?to=${encodeURIComponent(email)}`, null, { tags: { name: 'mock mailbox', kind: 'mock' } });
    if (res.status === 200) { codeMs.add(Date.now() - t0); return res.json().code; }
    await delay(500);
  }
  return null;
}

export async function signup() {
  http.cookieJar().clear(__ENV.BASE_URL || 'https://staging.test:8443');
  const id = newPhone();
  const t0 = Date.now();
  const ok = await (async () => {
    if (!(await attest(id))) return false;
    await call(id, 'GET', '/api/config', undefined, 'GET /api/config', 'auth');
    const ch = json(await call(id, 'POST', '/api/auth/challenge', { purpose: 'register' }, 'POST /api/auth/challenge', 'auth'));
    if (!ch) return false;
    const email = `sg-${id.device}-${Math.floor(Math.random() * 1e9)}@staging.test`;
    const password = 'Signup-Pass-2026!';
    const res = await call(id, 'POST', '/api/auth/register', {
      email, password, password_confirm: password, antibot: solvePow(ch), website: null,
      gender: pick(['male', 'female', 'unspecified']), age_confirmed: true, display_name: `${pick(FIRST)} ${pick(LAST)}`,
    }, 'POST /api/auth/register', 'auth');
    const pending = json(res);
    if (!pending) return false;
    if (!pending.verify) return true; // e-mail confirmation switched off in the panel
    const c = await code(email);
    if (!c) return false;
    const done = await call(id, 'POST', '/api/auth/register/verify', { signup_token: pending.signup_token, code: c },
      'POST /api/auth/register/verify', 'auth');
    return done.status === 201;
  })();
  if (ok) { signups.add(1); signupMs.add(Date.now() - t0); } else signupFailed.add(1);
}

export async function passwordLogin() {
  http.cookieJar().clear(__ENV.BASE_URL || 'https://staging.test:8443');
  const n = exec.scenario.iterationInTest + 1;
  const row = USERS[(n * 7919) % USERS.length];
  const id = { vu: exec.vu.idInTest, lang: row.lang, android: hash32(n) % 100 < 70, ip: ipFor(n % (1 << 20), 100), device: `lg-${n % 5000}`, row };
  if (!(await attest(id))) { loginFailed.add(1); return; }
  if (await login(id, row.email)) {
    logins.add(1);
    await call(id, 'GET', '/api/me', undefined, 'GET /api/me', 'auth');
  } else loginFailed.add(1);
}
