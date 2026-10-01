// "I'm not a robot" widget backed by a server-issued proof-of-work challenge.
import { api } from './api.js';
import { icon } from './icons.js';
import { h } from './ui.js';

function solve(ch) {
  return new Promise((resolve, reject) => {
    const worker = new Worker('/js/pow-worker.js');
    worker.onmessage = (e) => {
      if (e.data.type === 'done') { worker.terminate(); resolve(e.data.number); }
      else if (e.data.type === 'fail') { worker.terminate(); reject(new Error('fail')); }
    };
    worker.onerror = () => { worker.terminate(); reject(new Error('worker')); };
    worker.postMessage({ salt: ch.salt, challenge: ch.challenge, maxnumber: ch.maxnumber });
  });
}

export function createAntibot(purpose, enabled) {
  let state = 'idle';
  let solution = null;
  let pending = null;

  const label = h('span', { class: 'antibot__label', text: 'أنا لست روبوتًا' });
  const hint = h('span', { class: 'antibot__hint', text: 'تحقق سريع يتم على جهازك' });
  const el = h('button', { type: 'button', class: 'antibot', 'data-state': 'idle', 'aria-live': 'polite' },
    h('span', { class: 'antibot__box' }, icon('check')),
    h('span', { class: 'antibot__text' }, label, hint),
  );

  const set = (s, text) => {
    state = s;
    el.dataset.state = s;
    el.setAttribute('aria-pressed', s === 'done' ? 'true' : 'false');
    if (text) label.textContent = text;
  };

  async function run() {
    if (!enabled) return null;
    if (state === 'done' && solution) return solution;
    if (pending) return pending;
    set('working', 'جارٍ التحقق…');
    pending = (async () => {
      try {
        const ch = await api.post('/api/auth/challenge', { purpose });
        const number = await solve(ch);
        solution = { ...ch, number };
        set('done', 'تم التحقق');
        return solution;
      } catch (err) {
        set('error', 'فشل التحقق، اضغط للمحاولة مجددًا');
        throw err;
      } finally {
        pending = null;
      }
    })();
    return pending;
  }

  el.addEventListener('click', () => { if (state !== 'working' && state !== 'done') run().catch(() => {}); });

  return {
    el: enabled ? el : null,
    get verified() { return !enabled || state === 'done'; },
    ensure: run,                // returns the solution (solving first if needed)
    reset() { solution = null; pending = null; set('idle', 'أنا لست روبوتًا'); },
  };
}
