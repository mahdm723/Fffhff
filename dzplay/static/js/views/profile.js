import { api } from '../api.js';
import { icon } from '../icons.js';
import { profilePosts } from '../ideas.js';
import { privacySheet } from '../privacy.js';
import { notificationsEnabled, setNotifications } from '../notify.js';
import { genderPicker } from '../onboarding.js';
import { PickError, chooseFile, prepareImage, uploadBlob, uploadConfig, waitReady } from '../media-pick.js';
import { confirmAge } from '../people.js';
import { avatar, confirmSheet, formatDay, h, idChip, isAndroidApp, nameLine, personAvatar, sheet, toast } from '../ui.js';

const dateFmt = new Intl.DateTimeFormat('ar-DZ', { day: 'numeric', month: 'long' });

export function renderProfile(page, { config, onLogout, navigate, onMe }) {
  let me = null;
  const idCard = h('section', { class: 'id-card glass' }, avatar('xl'), h('div', { class: 'id-card__name' }, nameLine('dzplay')));
  const ageItem = h('li', { hidden: true });
  const statsBox = h('div', { class: 'stats' });
  const msgStats = h('p', { class: 'msg-stats' });
  const postsSlot = h('div');
  const notifSwitch = h('span', { class: 'switch', role: 'switch', 'aria-checked': 'false' });
  const supportBadge = h('span', { class: 'menu__end' }, icon('chev', 'chev'));
  const paintSupport = (n) => supportBadge.replaceChildren(...[n ? h('span', { class: 'chip chip--hot', text: 'رد جديد' }) : null, icon('chev', 'chev')].filter(Boolean));

  function stat(num, label) {
    return h('div', { class: 'stat glass' }, h('div', { class: 'stat__num', text: String(num) }), h('div', { class: 'stat__label', text: label }));
  }

  // V6 phase 3: profile picture — square, checked on the phone, then by the server like every upload.
  async function pickAvatar(btn) {
    const file = await chooseFile('image/jpeg,image/png,image/webp,image/heic,image/heif');
    if (!file) return;
    btn.setAttribute('aria-busy', 'true');
    try {
      const cfg = await uploadConfig(true);
      if (!cfg.available) throw new PickError('رفع الصور غير متاح الآن. حاول لاحقًا.');
      if (cfg.avatar && cfg.avatar.remaining <= 0) throw new PickError(`يمكنك تغيير صورتك ${cfg.avatar.limit} مرات في اليوم. حاول لاحقًا.`);
      const picked = await prepareImage(file, cfg, { square: 512 });
      if (picked.preview) URL.revokeObjectURL(picked.preview);
      await api.post('/api/uploads/precheck', { purpose: 'avatar' });
      toast('جارٍ رفع الصورة وفحصها…');
      const up = await uploadBlob(picked.blob, { purpose: 'avatar' });
      await waitReady(up.id);
      const r = await api.put('/api/me/avatar', { media_id: up.id });
      me = { ...me, avatar_url: r.avatar_url };
      if (onMe) onMe(me);
      drawCard();
      toast('تم تحديث صورتك.');
    } catch (err) {
      toast(err instanceof PickError ? err.message : (err.message || 'تعذّر تحديث الصورة.'), 'error', 4500);
    }
    btn.removeAttribute('aria-busy');
  }

  function avatarSheet() {
    sheet((panel, close) => {
      panel.append(h('h2', { text: 'الصورة الشخصية' }),
        h('p', { text: 'تظهر بجانب اسمك للجميع. تُفحص قبل النشر، ويمكن الإبلاغ عنها.' }),
        h('div', { class: 'actions' },
          h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: () => { close(); pickAvatar(idCard.querySelector('.avatar-edit')); } },
            icon('image'), me.avatar_url ? 'تغيير الصورة' : 'اختيار صورة'),
          me.avatar_url ? h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: async () => {
            try { await api.del('/api/me/avatar'); me = { ...me, avatar_url: null }; drawCard(); close(); toast('أُزيلت الصورة.'); }
            catch (e) { toast(e.message, 'error'); }
          } }, icon('trash'), 'إزالة الصورة') : '',
          h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
    });
  }

  function drawCard() {
    const avatarBtn = h('button', { class: 'avatar-edit', type: 'button', 'aria-label': 'تغيير الصورة الشخصية', onclick: avatarSheet },
      personAvatar(me.display_name, { size: 'xl', url: me.avatar_url }), h('span', { class: 'avatar-edit__badge', 'aria-hidden': 'true' }, icon('image')));
    idCard.replaceChildren(...[
      avatarBtn,
      h('div', { class: 'id-card__name' }, nameLine(me.display_name, me.gender === 'unspecified' ? null : me.gender, '', me.verified)),
      idChip(me.public_id),
      h('p', { class: 'id-card__hint', text: me.has_custom_name
        ? 'يظهر اسمك وصورتك في الأفكار والتعليقات والمحادثات.'
        : 'اختر اسمًا يظهر للآخرين لتتمكن من النشر والمراسلة.' }),
      h('div', { class: 'id-card__actions' }, h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: editSheet }, icon('edit'), 'تعديل الملف')),
    ].filter(Boolean));
    ageItem.hidden = me.age_confirmed !== false;
  }

  function editSheet() {
    const names = config.names || { min: 3, max: 20, cooldown_days: 14 };
    sheet((panel, close) => {
      const locked = !!me.next_name_change_at;
      const input = h('input', {
        class: 'input', id: 'display-name', dir: 'auto', maxlength: String(names.max), autocomplete: 'nickname',
        value: me.has_custom_name ? me.display_name : '', placeholder: 'اسمك الظاهر', disabled: locked,
      });
      const err = h('div', { class: 'form-error', role: 'alert' });
      const save = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'حفظ');
      const submit = async (body) => {
        err.textContent = '';
        if (!Object.keys(body).length) { close(); return; }
        save.disabled = true;
        try {
          me = { ...me, ...(await api.patch('/api/me/profile', body)) };
          if (onMe) onMe(me);
          drawCard();
          close();
          toast('حُفظ ملفك.');
        } catch (e) { err.textContent = e.message; save.disabled = false; }
      };
      save.addEventListener('click', () => {
        const body = {};
        const name = input.value.trim().replace(/\s+/g, ' ');
        if (!locked && name !== (me.has_custom_name ? me.display_name : '')) body.display_name = name;
        const gender = panel.querySelector('input[name="edit-gender"]:checked')?.value;
        if (gender && gender !== me.gender) body.gender = gender;
        submit(body);
      });
      panel.append(
        h('h2', { text: 'تعديل الملف' }),
        h('div', { class: 'field' },
          h('label', { for: 'display-name', text: 'الاسم الظاهر' }),
          input,
          h('small', { class: 'field__hint', text: locked
            ? `يمكنك تغيير الاسم مجددًا يوم ${dateFmt.format(new Date(me.next_name_change_at))}.`
            : `من ${names.min} إلى ${names.max} حرفًا: حروف عربية أو لاتينية وأرقام ومسافة و _ فقط. يمكن تغييره مرة كل ${names.cooldown_days} يومًا.` }),
        ),
        h('div', { class: 'field' }, h('label', { text: 'الجنس' }), genderPicker('edit-gender', me.gender)),
        err,
        h('div', { class: 'actions' }, save, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')),
      );
    });
  }

  function contactSheet() {
    const rows = [
      ['accept_direct', 'استقبال الرسائل المباشرة', 'من يعرف اسمك أو معرّفك يستطيع إرسال طلب مراسلة.'],
      ['searchable_by_name', 'الظهور في البحث بالاسم', 'عند الإيقاف يبقى بالإمكان إيجادك بمعرّفك DZ فقط.'],
    ];
    sheet((panel, close) => {
      const isOn = (k) => (k === 'accept_direct' ? me.privacy[k] === 'everyone' : !!me.privacy[k]);
      const list = h('ul', { class: 'menu' }, ...rows.map(([key, label, hint]) => {
        const sw = h('span', { class: 'switch', role: 'switch', 'aria-checked': String(isOn(key)), 'aria-label': label });
        const btn = h('button', { class: 'menu__item menu__item--setting', type: 'button' },
          h('span', {}, h('b', { text: label }), h('small', { text: hint })), sw);
        btn.addEventListener('click', async () => {
          const on = !isOn(key);
          btn.disabled = true;
          try {
            const privacy = await api.patch('/api/me/privacy', { [key]: key === 'accept_direct' ? (on ? 'everyone' : 'nobody') : on });
            me.privacy = privacy;
            sw.setAttribute('aria-checked', String(isOn(key)));
          } catch (e) { toast(e.message, 'error'); }
          btn.disabled = false;
        });
        return h('li', {}, btn);
      }));
      panel.append(h('h2', { text: 'الخصوصية والتواصل' }), list,
        h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'تم')));
    });
  }

  async function load() {
    try {
      me = await api.get('/api/profile');
      if (onMe) onMe(me);
      drawCard();
      paintSupport(me.support_unread);
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
            h('span', {}, h('b', { text: b.name || 'dzplay', dir: 'auto' }), h('br'), h('small', { text: `حُظر ${formatDay(b.created_at)}` })),
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

  async function shareApp() {
    const url = `${location.origin}${config.download_page || '/download'}`;
    const text = 'جرّب DZPLAY: مجتمع للمتداولين، أفكار وتحليلات وأخبار العملات الرقمية، وتراسل دون أن ينكشف بريدك. حمّل التطبيق من هنا:';
    try {
      if (window.DZPLAYAndroid) { window.DZPLAYAndroid.share(`${text} ${url}`); return; } // native share sheet in the app
      if (navigator.share) { await navigator.share({ title: 'DZPLAY', text, url }); return; }
      await navigator.clipboard.writeText(`${text} ${url}`);
      toast('نُسخ رابط التطبيق. أرسله لأصدقائك.');
    } catch (err) {
      if (err && err.name !== 'AbortError') toast(url, 'info', 8000);
    }
  }

  function deleteAccount() {
    sheet((panel, close) => {
      const google = me && me.sign_in_method === 'google';
      const input = google
        ? h('input', { class: 'input', id: 'delete-confirm', placeholder: 'اكتب: حذف حسابي', autocomplete: 'off' })
        : h('input', { class: 'input', id: 'delete-password', type: 'password', placeholder: 'كلمة المرور', autocomplete: 'current-password' });
      const err = h('div', { class: 'form-error', role: 'alert' });
      const go = h('button', { class: 'btn btn--danger btn--block', type: 'button' }, icon('trash'), 'حذف حسابي نهائيًا');
      go.addEventListener('click', async () => {
        err.textContent = '';
        go.disabled = true;
        try {
          await api.post('/api/me/delete', google ? { confirm: input.value.trim() } : { password: input.value });
          close();
          toast('حُذف حسابك. شكرًا لأنك كنت معنا.');
          onLogout();
        } catch (e) { err.textContent = e.message; go.disabled = false; }
      });
      panel.append(
        h('h2', { text: 'حذف حسابي' }),
        h('p', { text: 'سيُحذف نهائيًا ولا يمكن التراجع: حسابك واسمك ومعرّفك، وأفكارك وصورها وتعليقاتها، وتعليقاتك وتفاعلاتك، ومحادثاتك ورسائلك، وتذاكر الدعم، وطلبات التوثيق. وتُحذف صورك من مستودع التخزين.' }),
        h('p', { class: 'muted', text: 'يبقى فقط ما أُبلغ عنه كدليل لمدة محدودة، والبلاغات التي قدّمها آخرون، وسطر في سجل الإدارة بأن حسابًا حُذف.' }),
        h('a', { class: 'link-btn', href: '/policies/privacy', target: '_blank', rel: 'noopener' }, 'التفاصيل في سياسة الخصوصية'),
        h('div', { class: 'field' }, input), err,
        h('div', { class: 'actions' }, go, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
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
    idCard,
    statsBox,
    msgStats,
    h('ul', { class: 'menu glass' },
      isAndroidApp() ? null : item('bell', 'إشعارات الرسائل الجديدة', toggleNotifications, notifSwitch),
      item('verified', 'عضويتي', () => navigate('#/membership')),
      item('inbox', 'أرباحي', () => navigate('#/earnings')),
      item('plusUser', 'دعوة الأصدقاء', () => navigate('#/referrals')),
      item('info', 'الدعم والمساعدة', () => navigate('#/support'), supportBadge),
      item('lock', 'الخصوصية والتواصل', () => me && contactSheet()),
      ageItem,
      item('block', 'المحظورون', blockedSheet),
      item('shield', 'سياسة الخصوصية', privacySheet),
      item('info', 'السياسات والشروط', () => window.open('/policies', '_blank', 'noopener')),
      config && config.android_apk_url ? item('send', 'مشاركة تطبيق DZPLAY', shareApp) : null,
      item('logout', 'تسجيل الخروج', logout, h('span', { class: 'menu__end' }), 'menu__item--danger'),
      item('trash', 'حذف حسابي', deleteAccount, h('span', { class: 'menu__end' }), 'menu__item--danger'),
    ),
    postsSlot,
    h('p', { class: 'version', text: 'DZPLAY · نسخة تجريبية' }),
    ...(config && config.footer_text ? [h('p', { class: 'profile-footer', text: config.footer_text })] : []), // replaceChildren would print null
  );
  ageItem.replaceChildren(item('check', 'تأكيد العمر (18+)', async () => {
    if (await confirmAge()) { me.age_confirmed = true; drawCard(); toast('شكرًا. تم التأكيد.'); }
  }).firstChild);
  statsBox.replaceChildren(stat('–', 'منشورات'), stat('–', 'إعجاب'), stat('–', 'عدم إعجاب'));
  load();
}
