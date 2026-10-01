import { api } from '../api.js';
import { icon } from '../icons.js';
import { profilePosts } from '../ideas.js';
import { notificationsEnabled, setNotifications } from '../notify.js';
import { avatar, confirmSheet, formatDay, h, sheet, toast } from '../ui.js';

export function renderProfile(page, { onLogout, navigate, onMe }) {
  const statsBox = h('div', { class: 'stats' });
  const msgStats = h('p', { class: 'msg-stats' });
  const postsSlot = h('div');
  const notifSwitch = h('span', { class: 'switch', role: 'switch', 'aria-checked': 'false' });

  function stat(num, label) {
    return h('div', { class: 'stat glass' }, h('div', { class: 'stat__num', text: String(num) }), h('div', { class: 'stat__label', text: label }));
  }

  async function load() {
    try {
      const me = await api.get('/api/profile');
      if (onMe) onMe(me);
      statsBox.replaceChildren(
        stat(me.ideas.posts, 'منشورات'),
        stat(me.ideas.likes, 'إعجاب'),
        stat(me.ideas.dislikes, 'عدم إعجاب'),
      );
      // Private messaging stats: only you see these (visitors see idea stats only).
      msgStats.replaceChildren(icon('lock'),
        `رسائلك الخاصة: أرسلت ${me.stats.messages_sent} · استقبلت ${me.stats.messages_received} · ${me.stats.conversations} محادثة`);
      postsSlot.replaceChildren(
        h('h2', { class: 'section-title', text: 'أفكاري' }),
        profilePosts(me.ref, { navigate, emptyText: 'لم تنشر أي فكرة بعد. شارك أول فكرة من الصفحة الرئيسية.' }),
      );
      if (me.status === 'suspended') toast('حسابك موقوف مؤقتًا للمراجعة. لا يمكنك إرسال رسائل حاليًا.', 'error', 6000);
    } catch { /* offline: keep previous */ }
    notifSwitch.setAttribute('aria-checked', String(await notificationsEnabled()));
  }

  async function toggleNotifications() {
    const on = notifSwitch.getAttribute('aria-checked') !== 'true';
    try {
      const result = await setNotifications(on);
      notifSwitch.setAttribute('aria-checked', String(result));
      if (on && !result) toast('لم يتم السماح بالإشعارات من المتصفح.', 'error');
    } catch (err) { toast(err.message || 'تعذّر تغيير الإشعارات.', 'error'); }
  }

  function blockedSheet() {
    sheet(async (panel, close) => {
      const list = h('ul', { class: 'menu' }, h('li', {}, h('div', { class: 'menu__item' }, h('span', { text: 'جارٍ التحميل…' }))));
      panel.append(h('h2', { text: 'المحظورون' }), h('p', { text: 'الأشخاص الذين حظرتهم لا يمكنهم مراسلتك ولن يُختاروا لك.' }), list,
        h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', onclick: close }, 'إغلاق')));
      try {
        const { blocks } = await api.get('/api/blocks');
        if (!blocks.length) {
          list.replaceChildren(h('li', {}, h('div', { class: 'menu__item' }, h('span', { text: 'لم تحظر أحدًا.' }))));
          return;
        }
        list.replaceChildren(...blocks.map((b) => {
          const li = h('li', {}, h('div', { class: 'menu__item' },
            avatar('sm'),
            h('span', {}, h('b', { text: 'dzplay', dir: 'ltr' }), h('br'), h('small', { text: `حُظر ${formatDay(b.created_at)}` })),
            h('button', { class: 'btn btn--ghost', onclick: async () => {
              try { await api.del(`/api/blocks/${encodeURIComponent(b.id)}`); li.remove(); toast('تم إلغاء الحظر.'); }
              catch (err) { toast(err.message, 'error'); }
            } }, 'إلغاء الحظر'),
          ));
          return li;
        }));
      } catch (err) { list.replaceChildren(h('li', {}, h('div', { class: 'menu__item', text: err.message }))); }
    });
  }

  function privacySheet() {
    sheet((panel, close) => {
      panel.append(
        h('h2', { text: 'الخصوصية في DZPLAY' }),
        h('div', { class: 'prose' },
          h('h3', { text: 'ما يراه الآخرون' }),
          h('p', { text: 'لا شيء عنك. كل المستخدمين يظهرون باسم dzplay فقط. لا يُعرض بريدك أو رقمك أو موقعك أو أي معرّف داخلي.' }),
          h('h3', { text: 'ما نحتفظ به' }),
          h('ul', {},
            h('li', { text: 'بريدك وكلمة مرور مشفّرة (أو معرّف Google) لتسجيل الدخول فقط.' }),
            h('li', { text: 'الرسائل مؤقتة: تُحذف من الخادم تلقائيًا بعد قراءتها بوقت قصير أو بعد انتهاء مدتها.' }),
            h('li', { text: 'عناوين IP تُحفظ مشفّرة (بصمة غير قابلة للعكس) فقط لمنع الإساءة والمحاولات الآلية.' }),
          ),
          h('h3', { text: 'البلاغات' }),
          h('p', { text: 'عند الإبلاغ نحتفظ بنص الرسالة المُبلّغ عنها فقط لمراجعتها. لا يطّلع فريق الإدارة على محتوى الرسائل إلا عبر البلاغات.' }),
          h('h3', { text: 'على جهازك' }),
          h('p', { text: 'نسخة من محادثاتك تُحفظ على جهازك لتبقى مرئية لك، وتُمسح عند تسجيل الخروج.' }),
        ),
        h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', onclick: close }, 'إغلاق')),
      );
    });
  }

  async function logout() {
    const ok = await confirmSheet({ title: 'تسجيل الخروج؟', text: 'ستُمسح نسخة المحادثات المحفوظة على هذا الجهاز.', confirm: 'تسجيل الخروج', danger: true });
    if (ok) onLogout();
  }

  const item = (ic, label, onclick, extra = null, cls = '') =>
    h('li', {}, h('button', { class: `menu__item ${cls}`, type: 'button', onclick }, icon(ic), h('span', { text: label }), extra || icon('chev', 'chev')));

  page.replaceChildren(
    h('header', { class: 'topbar' }, h('h1', { class: 'page-title', text: 'حسابي' })),
    h('section', { class: 'id-card glass' },
      avatar('xl'),
      h('div', { class: 'id-card__name', text: 'dzplay' }),
      h('p', { class: 'id-card__hint', text: 'هذه هي هويتك الظاهرة للجميع. لا أحد يعرف من أنت.' }),
    ),
    statsBox,
    msgStats,
    h('ul', { class: 'menu glass' },
      item('bell', 'إشعارات الرسائل الجديدة', toggleNotifications, notifSwitch),
      item('block', 'المحظورون', blockedSheet),
      item('shield', 'الخصوصية', privacySheet),
      item('logout', 'تسجيل الخروج', logout, h('span'), 'menu__item--danger'),
    ),
    postsSlot,
    h('p', { class: 'version', text: 'DZPLAY · نسخة تجريبية' }),
  );
  statsBox.replaceChildren(stat('–', 'منشورات'), stat('–', 'إعجاب'), stat('–', 'عدم إعجاب'));
  load();
}
