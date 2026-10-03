// Inside the DZPLAY Android app: register its Firebase token (calls ring while the app is closed)
// and tell the user once when a newer version is on /download.
import { api } from './api.js';
import { h, isAndroidApp, sheet } from './ui.js';

const native = () => window.DZPLAYAndroid || null;

function appVersion() {
  const m = navigator.userAgent.match(/\bDZPLAYApp\/([\d.]+)/);
  return m ? m[1] : null;
}

export function newer(a, b) { // "2.0.0" > "1.2.0"
  const pa = String(a).split('.').map(Number);
  const pb = String(b).split('.').map(Number);
  for (let i = 0; i < Math.max(pa.length, pb.length); i += 1) {
    const d = (pa[i] || 0) - (pb[i] || 0);
    if (d) return d > 0;
  }
  return false;
}

function fcmToken() {
  try { return (native() && native().fcmToken && native().fcmToken()) || ''; } catch { return ''; }
}

export function initNative() {
  if (!isAndroidApp()) return;
  const token = fcmToken();
  if (token) api.post('/api/push/fcm', { token }).catch(() => {});
  checkUpdate();
}

export async function forgetNative() {
  const token = fcmToken();
  if (token) { try { await api.post('/api/push/fcm/remove', { token }); } catch { /* logout anyway */ } }
}

async function checkUpdate() {
  const mine = appVersion();
  if (!mine) return;
  let rel;
  try { rel = await api.get('/api/app/version'); } catch { return; }
  if (!rel.available || !rel.version_name || !newer(rel.version_name, mine)) return;
  const key = `dz:update-seen:${rel.version_name}`;
  try { if (localStorage.getItem(key)) return; localStorage.setItem(key, '1'); } catch { /* ignore */ }
  sheet((panel, close) => {
    panel.append(
      h('h2', { text: 'تحديث متوفر' }),
      h('p', { text: `الإصدار ${rel.version_name} من تطبيق DZPLAY متاح (لديك ${mine}). يضيف المكالمات الصوتية والمرئية وتحسينات أخرى.` }),
      h('div', { class: 'actions' },
        h('a', { class: 'btn btn--primary btn--block', href: '/download' }, 'تحميل التحديث'),
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'لاحقًا')),
    );
  });
}
