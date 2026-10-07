// DZPLAY client entry point: boot, routing and the app shell.
import { api } from './api.js';
import { initKeyboard } from './keyboard.js';
import { forgetNative, initNative } from './native.js';
import { icon } from './icons.js';
import { disableForLogout, refreshPushSubscription, showLocalNotification } from './notify.js';
import * as store from './store.js';
import { h, toast } from './ui.js';
import { runOnboarding } from './onboarding.js';
import { showPrivacyNotice } from './privacy.js';
import { renderAuth } from './views/auth.js';
import { renderChat } from './views/chat.js';
import { renderHome, resetFeedCache } from './views/home.js';
import { renderMessages } from './views/messages.js';
import { renderProfile } from './views/profile.js';
import { renderSupport, renderTicket } from './views/support.js';
import { renderMembership } from './views/membership.js';
import { renderEarnings, renderReferrals } from './views/earnings.js';
import { renderUser } from './views/user.js';
import { renderUsers } from './views/users.js';
import { renderNotifications } from './views/notifications.js';
import { renderPost } from './views/post.js';

const root = document.getElementById('app');
let cleanupView = null;
let shell = null;

const TABS = [
  { id: 'home', label: 'الرئيسية', icon: 'feather' },
  { id: 'users', label: 'المستخدمون', icon: 'users' },
  { id: 'messages', label: 'الرسائل', icon: 'chat' },
  { id: 'profile', label: 'حسابي', icon: 'user' },
];

function navigate(hash) {
  if (location.hash === hash) route(); else location.hash = hash;
}

function parseRoute() {
  const m = location.hash.match(/^#\/chat\/([A-Za-z0-9_-]{1,32})$/);
  if (m) return { name: 'chat', id: m[1] };
  const u = location.hash.match(/^#\/u\/([A-Za-z0-9_-]{1,32})$/);
  if (u) return { name: 'user', ref: u[1] };
  const p = location.hash.match(/^#\/id\/(DZ-[A-Z0-9]{6})$/i);
  if (p) return { name: 'user', publicId: p[1].toUpperCase() };
  const t = location.hash.match(/^#\/support\/(\d{1,9})$/);
  if (t) return { name: 'ticket', id: Number(t[1]) };
  const pp = location.hash.match(/^#\/post\/([A-Za-z0-9_-]{1,32})(?:\/([A-Za-z0-9_-]{1,32}))?$/);
  if (pp) return { name: 'post', id: pp[1], commentId: pp[2] || null };
  if (location.hash === '#/notifications') return { name: 'notifications' };
  if (location.hash === '#/earnings') return { name: 'earnings' };
  if (location.hash === '#/referrals') return { name: 'referrals' };
  if (location.hash === '#/support') return { name: 'support' };
  if (location.hash === '#/membership' || location.hash === '#/verify') return { name: 'membership' }; // V6: the star comes with the membership
  const name = location.hash.replace(/^#\//, '');
  return { name: TABS.some((t) => t.id === name) ? name : 'home' };
}

function setTabBadge(tab, n) {
  const btn = shell && shell.nav.querySelector(`[data-tab="${tab}"]`);
  if (!btn) return;
  const badge = btn.querySelector('.badge');
  if (n) {
    const text = n > 99 ? '99+' : String(n);
    if (badge) badge.textContent = text;
    else btn.append(h('span', { class: 'badge', text }));
  } else if (badge) badge.remove();
}

function updateBadges() {
  const n = store.unreadTotal();
  document.title = n ? `(${n}) DZPLAY` : 'DZPLAY';
  setTabBadge('messages', n || store.requestCount());
  setTabBadge('profile', (store.state.me && store.state.me.unseen_comments) || 0);
  const bell = (store.state.me && store.state.me.unread_notifications) || 0; // V6: the bell on Home
  document.querySelectorAll('.bell-btn').forEach((b) => {
    const badge = b.querySelector('.badge');
    if (bell) { if (badge) badge.textContent = bell > 99 ? '99+' : String(bell); else b.append(h('span', { class: 'badge', text: bell > 99 ? '99+' : String(bell) })); }
    else if (badge) badge.remove();
  });
}

function updateConnectionBanner() {
  if (!shell) return;
  const offline = !store.state.online;
  shell.banner.hidden = !offline;
}

function buildShell() {
  const page = h('main', { class: 'page', id: 'page' });
  const banner = h('div', { class: 'offline-banner', hidden: true, text: 'أنت غير متصل بالإنترنت. ستُرسل رسائلك عند عودة الاتصال.' });
  const nav = h('nav', { class: 'nav', 'aria-label': 'التنقل الرئيسي' },
    h('div', { class: 'nav__inner' },
      ...TABS.map((t) => h('button', { class: 'nav__btn', type: 'button', 'data-tab': t.id, onclick: () => navigate(`#/${t.id}`) }, icon(t.icon), t.label)),
    ),
  );
  const el = h('div', { class: 'shell' }, banner, page, nav);
  return { el, page, nav, banner };
}

function route() {
  if (!store.state.me) return;
  if (cleanupView) { cleanupView(); cleanupView = null; }
  const r = parseRoute();
  window.scrollTo(0, 0);

  if (r.name === 'chat') {
    shell = null;
    cleanupView = renderChat(root, { conversationId: r.id, navigate }) || null;
    updateBadges();
    return;
  }
  if (!shell || !shell.el.isConnected) {
    shell = buildShell();
    root.replaceChildren(shell.el);
  }
  const activeTab = ['post', 'notifications'].includes(r.name) ? 'home' : r.name === 'user' ? 'users' : ['support', 'ticket', 'membership', 'earnings', 'referrals'].includes(r.name) ? 'profile' : r.name;
  for (const btn of shell.nav.querySelectorAll('.nav__btn')) {
    if (btn.dataset.tab === activeTab) btn.setAttribute('aria-current', 'page'); else btn.removeAttribute('aria-current');
  }
  const ctx = {
    config: store.state.config, navigate, onLogout: logout,
    onMe,
  };
  if (r.name === 'home') cleanupView = renderHome(shell.page, ctx) || null;
  else if (r.name === 'messages') cleanupView = renderMessages(shell.page, ctx) || null;
  else if (r.name === 'users') cleanupView = renderUsers(shell.page, ctx) || null;
  else if (r.name === 'notifications') cleanupView = renderNotifications(shell.page, ctx) || null;
  else if (r.name === 'post') cleanupView = renderPost(shell.page, { ...ctx, postId: r.id, commentId: r.commentId }) || null;
  else if (r.name === 'user') cleanupView = renderUser(shell.page, { ...ctx, ref: r.ref, publicId: r.publicId }) || null;
  else if (r.name === 'support') cleanupView = renderSupport(shell.page, ctx) || null;
  else if (r.name === 'ticket') cleanupView = renderTicket(shell.page, { ...ctx, ticketId: r.id }) || null;
  else if (r.name === 'membership') cleanupView = renderMembership(shell.page, ctx) || null;
  else if (r.name === 'earnings') cleanupView = renderEarnings(shell.page, ctx) || null;
  else if (r.name === 'referrals') cleanupView = renderReferrals(shell.page, ctx) || null;
  else cleanupView = renderProfile(shell.page, ctx) || null;
  updateBadges();
  updateConnectionBanner();
}

function onMe(me) {
  store.state.me = { ...store.state.me, ...me };
  updateBadges();
}

async function logout() {
  await disableForLogout();
  await forgetNative();
  try { await api.post('/api/auth/logout'); } catch { /* clear locally anyway */ }
  store.stopRealtime();
  store.clearCache();
  resetFeedCache();
  try { localStorage.removeItem('dz:draft'); localStorage.removeItem('dz:session'); } catch { /* ignore */ }
  store.state.me = null;
  showAuth();
}

function showAuth() {
  shell = null;
  if (cleanupView) { cleanupView(); cleanupView = null; }
  renderAuth(root, {
    config: store.state.config,
    onAuthenticated: (me) => {
      store.clearCache(); // never show a previous account's cached conversations
      resetFeedCache();
      history.replaceState(null, '', '#/home');
      startSession(me);
    },
  });
}

function hadSession() { try { return localStorage.getItem('dz:session') === '1'; } catch { return false; } }

function startSession(me) {
  store.state.me = me;
  try { localStorage.setItem('dz:session', '1'); } catch { /* ignore */ }
  route();
  store.startRealtime();
  initNative();
  store.sync({ full: true }).catch(() => {});
  store.flushOutbox();
  refreshPushSubscription();
  showPrivacyNotice(me, () => runOnboarding(me, { onMe, onLogout: logout }));
}

store.subscribe((type, detail) => {
  if (type === 'sync') updateBadges();
  else if (type === 'connection') updateConnectionBanner();
  else if (type === 'unauthorized') {
    if (store.state.me) { toast('انتهت الجلسة. سجّل الدخول من جديد.', 'error'); logout(); }
  } else if (type === 'incoming') {
    const inChat = parseRoute().name === 'chat';
    if (document.visibilityState !== 'visible') showLocalNotification();
    else if (!inChat) toast(detail > 1 ? `وصلتك ${detail} رسائل جديدة` : 'وصلتك رسالة جديدة');
  } else if (type === 'comment') {
    if (store.state.me) store.state.me.unseen_comments = (store.state.me.unseen_comments || 0) + 1;
    updateBadges();
  } else if (type === 'notify') { // V6: a comment, a reply… (the list is in «الإشعارات»)
    if (store.state.me) store.state.me.unread_notifications = (store.state.me.unread_notifications || 0) + 1;
    updateBadges();
    if (document.visibilityState !== 'visible') showLocalNotification();
    else if (parseRoute().name !== 'notifications') toast('لديك إشعار جديد 🔔');
  } else if (type === 'account') { // V5: the blue star was granted / revoked, a request was decided
    api.get('/api/me').then((me) => onMe(me)).catch(() => {});
    document.dispatchEvent(new CustomEvent('dz:account'));
  } else if (type === 'support') {
    if (store.state.me) store.state.me.support_unread = (store.state.me.support_unread || 0) + 1;
    if (!['support', 'ticket'].includes(parseRoute().name)) toast('ردّ فريق الدعم على تذكرتك. افتح حسابي ← الدعم.');
    document.dispatchEvent(new CustomEvent('dz:support'));
  }
});

window.addEventListener('hashchange', route);
document.addEventListener('dz:badges', updateBadges);

async function boot() {
  initKeyboard();
  // V6: an opaque strip under the phone's status bar, on every screen (outside #app, never re-rendered)
  if (!document.querySelector('.status-scrim')) document.body.append(h('div', { class: 'status-scrim', 'aria-hidden': 'true' }));
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  store.loadCache();
  try {
    store.state.config = await api.get('/api/config');
    try { localStorage.setItem('dz:config', JSON.stringify(store.state.config)); } catch { /* ignore */ }
  } catch {
    try { store.state.config = JSON.parse(localStorage.getItem('dz:config') || 'null'); } catch { /* ignore */ }
  }
  if (!store.state.config) {
    root.replaceChildren(h('div', { class: 'auth' },
      h('p', { class: 'auth__tagline', text: 'تعذّر الاتصال بالخادم. تحقق من الإنترنت ثم أعد المحاولة.' }),
      h('button', { class: 'btn btn--primary', onclick: () => location.reload() }, 'إعادة المحاولة')));
    return;
  }
  try {
    const me = await api.get('/api/me');
    startSession(me);
  } catch (err) {
    if (err.status === 401) { store.clearCache(); showAuth(); }
    else if (err.isNetwork && hadSession()) {
      // Offline start with a previous session: show cached data, reconnect later.
      startSession({ display_name: 'dzplay' });
    } else showAuth();
  }
}

boot();
