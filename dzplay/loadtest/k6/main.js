// V6 phase 21: the load scenarios. One script; SCENARIO picks the shape, PACE=quick|full the durations.
//   A baseline   100 users                          (full: 10 min)
//   B load       500 → 1,000 → 2,500 users          (full: 15 min each)
//   C stress     +250 users per step until it breaks (errors > 5 % or p95 > 3 s: loadtest/monitor/k6sink.py stops k6)
//   D spike      200 → 3,000 users in 60 s, then back to 200 (recovery)
//   E soak       1,000 users                        (full: 1 h)
//   G auth       sign-up and login waves (per minute: 30 → 960) over 300 browsing users
//   S smoke      10 users for a minute (checks the tools)
// (F, the maximum number of WebSockets, is loadtest/ws/wsflood.py.)
// Run by loadtest/run.sh, never directly against anything but staging (it checks /healthz/env first).
import { memberSession } from './lib/session.js';
import { passwordLogin, signup } from './lib/auth.js';
import { setLevels } from './lib/client.js';
import http from 'k6/http';

export { memberSession, signup, passwordLogin };

const FULL = (__ENV.PACE || 'quick') === 'full';
const m = (full, quick) => `${FULL ? full : quick}m`;
const SCENARIO = (__ENV.SCENARIO || 'A').toUpperCase();

// a stage list → (elapsed seconds → level tag): "ramp" while the number of users changes, else the number itself
function levelsFrom(stages) {
  const marks = [];
  let t = 0;
  let from = 0;
  for (const st of stages) {
    const secs = parseFloat(st.duration) * (st.duration.endsWith('m') ? 60 : 1);
    marks.push([t, t + secs, from === st.target ? String(st.target) : 'ramp']);
    t += secs;
    from = st.target;
  }
  return (elapsed) => (marks.find(([a, b]) => elapsed >= a && elapsed < b) || [0, 0, 'after'])[2];
}

const members = (stages, extra = {}) => ({
  executor: 'ramping-vus', exec: 'memberSession', startVUs: 0, stages, gracefulRampDown: '20s', gracefulStop: '30s', ...extra,
});

const B_LEVELS = [500, 1000, 2500];
const G_WAVES = (__ENV.G_WAVES || '30,60,120,240,480,960').split(',').map(Number); // sign-ups (and logins) per minute
const G_BACKGROUND = Number(__ENV.G_BACKGROUND || 300);
const SHAPES = {
  S: () => [{ duration: '20s', target: 10 }, { duration: '70s', target: 10 }], // smoke: checks the tools, not the server
  A: () => [{ duration: '1m', target: 100 }, { duration: m(10, 4), target: 100 }],
  B: () => B_LEVELS.flatMap((n) => [{ duration: '2m', target: n }, { duration: m(15, 4), target: n }]),
  C: () => {
    const out = [];
    for (let n = 250; n <= Number(__ENV.STRESS_MAX || 8000); n += 250) {
      out.push({ duration: '45s', target: n }, { duration: FULL ? '2m15s' : '75s', target: n });
    }
    return out;
  },
  D: () => [{ duration: '1m', target: 200 }, { duration: m(3, 2), target: 200 }, { duration: '60s', target: 3000 },
    { duration: m(3, 2), target: 3000 }, { duration: '30s', target: 200 }, { duration: m(5, 3), target: 200 }],
  E: () => [{ duration: '2m', target: 1000 }, { duration: m(60, 15), target: 1000 }],
};

function scenarios() {
  if (SCENARIO === 'G') {
    const waves = G_WAVES;
    const hold = Number(__ENV.G_HOLD || (FULL ? 240 : 120)); // seconds per wave (after a 20 s ramp)
    const lead = Number(__ENV.G_LEAD || 120); // the browsing users alone first
    const wave = (exec) => ({
      executor: 'ramping-arrival-rate', exec, startRate: 0, timeUnit: '1m', preAllocatedVUs: 50, maxVUs: 800,
      startTime: `${lead}s`, stages: waves.flatMap((r) => [{ duration: '20s', target: r }, { duration: `${hold}s`, target: r }]),
    });
    const total = waves.length * (hold + 20) + lead + 30;
    setLevels((t) => (t < lead ? 'background' : String(waves[Math.min(waves.length - 1, Math.floor((t - lead) / (hold + 20)))])),
      { signup: lead, login: lead });
    return {
      background: members([{ duration: '1m', target: G_BACKGROUND }, { duration: `${total}s`, target: G_BACKGROUND }]),
      signup: wave('signup'),
      login: wave('passwordLogin'),
    };
  }
  const stages = (SHAPES[SCENARIO] || SHAPES.A)();
  setLevels(levelsFrom(stages));
  return { members: members(stages) };
}

const READ_P95 = Number(__ENV.SLO_READ_P95 || 300);
const READ_P99 = Number(__ENV.SLO_READ_P99 || 800);
const WRITE_P95 = Number(__ENV.SLO_WRITE_P95 || 600);

function thresholds() {
  const t = {
    'http_req_duration{kind:read}': [`p(95)<${READ_P95}`, `p(99)<${READ_P99}`],
    'http_req_duration{kind:write}': [`p(95)<${WRITE_P95}`],
    'http_req_duration{kind:static}': ['p(95)<1000'],
    'http_req_duration{kind:media}': ['p(95)<1500'],
    http_req_failed: ['rate<0.01'],
    ws_dropped: ['count<1'],
  };
  const levels = SCENARIO === 'B' ? B_LEVELS.map(String)
    : SCENARIO === 'D' ? ['200', '3000', 'ramp'] : SCENARIO === 'G' ? ['background', ...G_WAVES.map(String)] : [];
  for (const lv of levels) { // one sub-metric per level → its own line in the summary (per-level p95/p99)
    t[`http_req_duration{kind:read,level:${lv}}`] = [`p(95)<${READ_P95}`];
    t[`http_req_duration{kind:write,level:${lv}}`] = [`p(95)<${WRITE_P95}`];
    t[`http_req_failed{level:${lv}}`] = ['rate<0.01'];
    t[`http_reqs{level:${lv}}`] = ['count>=0'];
  }
  if (SCENARIO === 'G') { // the waves must never spill over the members who browse and chat meanwhile
    t.signup_total_ms = ['p(95)<5000'];
    t['http_req_duration{kind:auth}'] = ['p(95)<1500'];
    t['http_req_failed{kind:auth}'] = ['rate>=0']; // reported: refusals ("busy") belong to the wave itself
    t['http_req_failed{kind:read}'] = ['rate<0.01'];
    t['http_req_failed{kind:write}'] = ['rate<0.01'];
    t.busy_503 = ['count>=0'];
    for (const lv of G_WAVES.map(String)) {
      t[`http_req_failed{kind:read,level:${lv}}`] = ['rate<0.01'];
      t[`http_req_failed{kind:auth,level:${lv}}`] = ['rate>=0'];
      t[`http_req_duration{kind:auth,level:${lv}}`] = ['p(95)<1500'];
    }
  }
  if (SCENARIO === 'C') { // backstop (cumulative); the 30-second window is watched by k6sink.py
    t.http_req_failed = [{ threshold: 'rate<0.05', abortOnFail: true, delayAbortEval: '2m' }];
    t['http_req_duration{kind:read}'] = [{ threshold: 'p(95)<3000', abortOnFail: true, delayAbortEval: '2m' }];
  }
  return t;
}

export const options = {
  scenarios: scenarios(),
  thresholds: thresholds(),
  insecureSkipTLSVerify: true, // staging: Caddy's internal certificate
  discardResponseBodies: false,
  noCookiesReset: true,
  summaryTrendStats: ['avg', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  systemTags: ['status', 'method', 'name', 'scenario', 'expected_response', 'error_code'],
  // 429 (a limit hit) counts as a failure, like 5xx and timeouts; other 4xx are counted apart (refused_4xx)
  setupTimeout: '2m',
};

http.setResponseCallback(http.expectedStatuses({ min: 200, max: 428 }, { min: 430, max: 499 }));

export function handleSummary(data) {
  const out = __ENV.SUMMARY_OUT || '/results/summary.json';
  return { [out]: JSON.stringify(data, null, 1), stdout: `\nsummary → ${out}\n` };
}
