// Notifications never reveal the sender or the message text.
import { api } from './api.js';
import { state } from './store.js';

const KEY = 'dz:notify';
const BODY = 'لديك رسالة جديدة على DZPLAY';

const supported = () => 'Notification' in window;
const flag = () => { try { return localStorage.getItem(KEY) === '1'; } catch { return false; } };

export async function notificationsEnabled() {
  return supported() && Notification.permission === 'granted' && flag();
}

function urlB64ToUint8Array(base64) {
  const padding = '='.repeat((4 - (base64.length % 4)) % 4);
  const raw = atob((base64 + padding).replace(/-/g, '+').replace(/_/g, '/'));
  return Uint8Array.from(raw, (c) => c.charCodeAt(0));
}

async function subscribePush() {
  const key = state.config?.push_public_key;
  if (!key || !('serviceWorker' in navigator) || !('PushManager' in window)) return;
  const reg = await navigator.serviceWorker.ready;
  let sub = await reg.pushManager.getSubscription();
  if (!sub) sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlB64ToUint8Array(key) });
  const json = sub.toJSON();
  await api.post('/api/push/subscribe', { endpoint: json.endpoint, keys: json.keys });
}

async function unsubscribePush() {
  if (!('serviceWorker' in navigator)) return;
  const reg = await navigator.serviceWorker.getRegistration();
  const sub = reg && (await reg.pushManager?.getSubscription());
  if (!sub) return;
  try { await api.post('/api/push/unsubscribe', { endpoint: sub.endpoint }); } catch { /* ignore */ }
  try { await sub.unsubscribe(); } catch { /* ignore */ }
}

export async function setNotifications(on) {
  if (!supported()) throw new Error('المتصفح لا يدعم الإشعارات.');
  if (!on) {
    try { localStorage.removeItem(KEY); } catch { /* ignore */ }
    await unsubscribePush();
    return false;
  }
  const permission = await Notification.requestPermission();
  if (permission !== 'granted') return false;
  try { localStorage.setItem(KEY, '1'); } catch { /* ignore */ }
  await subscribePush().catch(() => {});
  return true;
}

// Re-register the push subscription for the current account after login.
export async function refreshPushSubscription() {
  if (await notificationsEnabled()) await subscribePush().catch(() => {});
}

export async function showLocalNotification() {
  if (!(await notificationsEnabled())) return;
  const options = { body: BODY, tag: 'dz-message', renotify: true, icon: '/icons/icon-192.png', data: { url: '/#/messages' } };
  const reg = 'serviceWorker' in navigator ? await navigator.serviceWorker.getRegistration() : null;
  if (reg) reg.showNotification('DZPLAY', options);
  else new Notification('DZPLAY', options);
}

export async function disableForLogout() {
  await unsubscribePush();
  try { localStorage.removeItem(KEY); } catch { /* ignore */ }
}
