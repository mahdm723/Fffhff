// Admin panel — security & system: Telegram bot, e-mail, media cache, network blocks (lift),
// password-reset requests, failed sign-ins, data cleanup and the admin-account CLI.
import { h, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import { appName } from '/js/brand.js';
import {
  attempt, authorLine, bytes, call, chip, confirmDanger, emptyState, field, fmt, hooks, sectionHead, spinner, userRef, when,
} from './admin-common.js';

const RESET_STATUS = {
  pending: 'بانتظار المشرف', sent: 'أُرسل الرمز', verified: 'تم التحقق', used: 'تم التغيير', cancelled: 'أُلغي', expired: 'انتهى',
  failed: 'فشل',
};
const LOGIN_TYPES = {
  login_failed: 'دخول فاشل', login_blocked_ip: 'حظر (الشبكة)', login_blocked_ip_account: 'حظر (الشبكة + الحساب)',
  account_locked: 'قفل مؤقت للحساب', admin_login_failed: 'دخول مشرف فاشل',
};
const BLOCK_SCOPE = { ip: 'شبكة', ipacct: 'شبكة + حساب', acct: 'حساب' };
const CLEANUP_LABELS = {
  messages: 'رسائل منتهية', conversations: 'محادثات منتهية', sessions: 'جلسات منتهية', challenges: 'تحديات مكافحة الروبوت',
  auth_throttle: 'سجلات حظر الدخول', security_events: 'سجلات أمان قديمة', reports: 'بلاغات قديمة مغلقة', flags: 'رصد قديم مغلق',
  notifications: 'إشعارات قديمة',
};

function card(title, iconName, ...children) {
  return h('article', { class: 'admin-group glass' }, h('h3', { class: 'admin-group__title' }, icon(iconName), title), ...children);
}

function rows(pairs) {
  return h('dl', { class: 'admin-group__rows' }, ...pairs.map(([k, v, cls]) => h('div', { class: 'admin-row' },
    h('dt', { text: k }), h('dd', { class: cls || null, text: String(v) }))));
}

export async function renderSystem(main) {
  const body = h('div', { class: 'admin-section' }, spinner());
  main.replaceChildren(sectionHead('الأمان والنظام'), body);
  const d = await attempt(() => call('GET', '/api/admin/system'));
  if (!d) { body.replaceChildren(); return; }

  const used = d.media_cache.bytes;
  const limit = d.media_cache.limit_bytes || 1;
  const pct = Math.min(100, Math.round((used / limit) * 100));
  const fill = h('span', {});
  fill.style.inlineSize = `${pct}%`;

  body.replaceChildren(
    botSection(),
    smtpSection('system'),
    smtpSection('support'),
    h('div', { class: 'admin-groups' },
      card('ذاكرة الوسائط المؤقتة', 'image',
        h('div', { class: 'admin-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct),
          'aria-label': 'استخدام الذاكرة المؤقتة' }, fill),
        h('p', { class: 'admin-meta', text: `${bytes(used)} من ${bytes(limit)} (${fmt(pct)}٪). الأقدم استخدامًا يُحذف تلقائيًا ويُعاد تنزيله من Telegram عند الحاجة.` }))),
    marketCard(),
    blocksSection(d.ip_blocks),
    resetsSection(d.reset_requests),
    loginsSection(d.failed_logins),
    cleanupCard(),
    card('أمان حسابات المشرفين', 'lock',
      h('p', { class: 'admin-meta', text: 'إنشاء مشرف، أو إذا فقدت هاتف المصادقة، أو لتغيير كلمة المرور — في الخادم:' }),
      h('pre', { class: 'admin-code', dir: 'ltr' }, h('code', {
        text: 'cd /opt/dzplay/dzplay\n'
          + 'docker compose exec app python -m app.admin_cli create-admin NAME\n'
          + 'docker compose exec app python -m app.admin_cli reset-admin-2fa NAME\n'
          + 'docker compose exec app python -m app.admin_cli set-admin-password NAME\n'
          + 'docker compose exec app python -m app.admin_cli audit',
      }))));
}

// ------------------------------------------------------------------ V6 phase 2: market (Bybit)

function marketCard() {
  const box = h('div', {}, spinner());
  const paint = (m) => box.replaceChildren(
    rows([
      ['الحالة', !m.enabled ? 'موقوف من الإعدادات' : m.stale ? 'قديمة (لم يُحدَّث مؤخرًا)' : 'يعمل', m.enabled && !m.stale ? 'admin-ok' : 'admin-warn'],
      ['المصدر', m.source],
      ['آخر تحديث ناجح', m.updated_at ? when(m.updated_at) : '—'],
      ['آخر محاولة', m.checked_at ? when(m.checked_at) : '—'],
      ['أزواج صالحة', m.pairs ?? '—'],
      ['آخر خطأ', m.last_error || '—'],
    ]),
    h('div', { class: 'admin-actions' }, h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: async () => {
      const r = await attempt(() => call('POST', '/api/admin/market/refresh'));
      if (r) { toast(r.last_error ? `فشل: ${r.last_error}` : 'حُدّث.'); paint(r); }
    } }, 'تحديث الآن')),
    h('p', { class: 'admin-meta', text: 'المصدر وفترة التحديث والاستبعاد من «الإعدادات ← السوق». فحص الوصول من الخادم: docker compose exec app python -m app.admin_cli market-check' }));
  attempt(() => call('GET', '/api/admin/market')).then((m) => { if (m) paint(m); else box.replaceChildren(); });
  return card('السوق (Bybit)', 'chart', box);
}

// ------------------------------------------------------------------ Telegram bot (token is write-only)

const SOURCE = { panel: 'من لوحة التحكم', env: 'من ملف .env في الخادم', none: '—' };

function botSection() {
  const status = h('div', {}, spinner());
  const token = h('input', { class: 'input', type: 'password', dir: 'ltr', autocomplete: 'off', spellcheck: 'false',
    autocapitalize: 'off', maxlength: '100', placeholder: '123456789:AA…' });
  const chat = h('input', { class: 'input', dir: 'ltr', inputmode: 'numeric', autocomplete: 'off', maxlength: '24', placeholder: '323530056' });
  const code = h('input', { class: 'input', dir: 'ltr', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6',
    pattern: '[0-9]{6}', placeholder: '123456' });
  const save = h('button', { type: 'submit', class: 'btn btn--primary btn--block' }, icon('send'), 'حفظ وربط البوت');
  const testBtn = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', hidden: true }, 'رسالة تجربة');
  const removeBtn = h('button', { type: 'button', class: 'btn btn--danger btn--sm', hidden: true }, 'إزالة الربط');
  const needCode = () => {
    if (/^[0-9]{6}$/.test(code.value.trim())) return code.value.trim();
    toast('اكتب رمز التحقق من تطبيق المصادقة (6 أرقام).', 'error');
    code.focus();
    return null;
  };

  const paint = (s) => {
    const r = [
      ['الحالة', s.configured ? `مربوط${s.bot_username ? ` — @${s.bot_username}` : ''}` : 'غير مربوط', s.configured ? 'admin-ok' : 'admin-warn'],
    ];
    if (s.configured) {
      r.push(['المصدر', SOURCE[s.source] || s.source], ['رقم المحادثة', s.chat_id || '—'], ['الرمز المحفوظ', s.token_hint || '—']);
      if (s.webhook) {
        r.push(['الاستقبال (Webhook)', s.webhook.url_set ? 'يعمل' : 'غير مضبوط', s.webhook.url_set ? 'admin-ok' : 'admin-warn']);
        if (s.webhook.last_error) r.push(['آخر خطأ من Telegram', s.webhook.last_error, 'admin-warn']);
      }
    }
    if (s.error) r.push(['تعذّر الاتصال بـ Telegram', s.error, 'admin-warn']);
    status.replaceChildren(rows(r));
    if (s.chat_id && !chat.value) chat.value = s.chat_id;
    testBtn.hidden = !s.configured;
    removeBtn.hidden = s.source !== 'panel';
    save.lastChild.textContent = s.configured ? 'تحديث الرمز' : 'حفظ وربط البوت';
  };
  const load = async () => { const s = await attempt(() => call('GET', '/api/admin/telegram')); if (s) paint(s); };

  const form = h('form', {
    class: 'admin-form',
    onsubmit: async (e) => {
      e.preventDefault();
      const otp = needCode();
      if (!otp) return;
      save.disabled = true;
      const s = await attempt(() => call('PUT', '/api/admin/telegram', { token: token.value.trim(), chat_id: chat.value.trim(), code: otp }));
      save.disabled = false;
      token.value = '';
      code.value = '';
      if (!s) return;
      paint(s);
      if (s.test_error) toast(s.test_error, 'error');
      else toast('تم الربط ✅ — وصلتك رسالة تجربة في Telegram.');
    },
  },
  h('ol', { class: 'admin-steps' },
    h('li', { text: 'في Telegram افتح @BotFather، أنشئ بوتًا أو اختر بوتك، وانسخ الرمز (Token). إذا شاركت الرمز مع أحد، اضغط Revoke وخذ رمزًا جديدًا.' }),
    h('li', { text: 'رقم محادثتك: أرسل أي رسالة إلى @userinfobot وانسخ الرقم (Id).' }),
    h('li', { text: 'افتح بوتك واضغط Start (مرة واحدة)، ثم املأ الحقول هنا.' })),
  field('رمز البوت (Token)', token),
  field('رقم محادثتك (Chat ID)', chat),
  field('رمز التحقق من تطبيق المصادقة', code),
  h('p', { class: 'admin-meta', text: 'الرمز يُحفظ مشفّرًا ولا يُعرض بعد الحفظ أبدًا. كل تغيير يُسجَّل في سجل الإدارة.' }),
  save);

  testBtn.addEventListener('click', async () => {
    if (await attempt(() => call('POST', '/api/admin/telegram/test'))) toast('أُرسلت رسالة تجربة إلى Telegram.');
  });
  removeBtn.addEventListener('click', async () => {
    const otp = needCode();
    if (!otp || !(await confirmDanger('إزالة ربط البوت؟', 'تتوقف الإحصائيات وطلبات استعادة كلمات المرور وأزرار الإشراف عبر Telegram (أو يعود لإعداد ملف .env إن وُجد).', 'إزالة'))) return;
    const s = await attempt(() => call('POST', '/api/admin/telegram/remove', { code: otp }));
    code.value = '';
    if (s) { paint(s); toast('أُزيل الربط.'); }
  });

  load();
  return card('بوت Telegram', 'send', h('p', { class: 'admin-meta', text: 'للإحصائيات (/stats)، وطلبات استعادة كلمات المرور، وأزرار الإشراف على الصور والتوثيق.' }), status, h('div', { class: 'admin-actions' }, testBtn, removeBtn), form);
}

// ------------------------------------------------------------------ e-mail: two mailboxes (password is write-only)
// V6 phase 7: «بريد النظام» (codes, membership, rewards, prizes — "do not reply" footer) and «بريد الدعم»
// (tickets and replies; its address is shown to users in «تواصل معنا»). Without its own settings the
// support mailbox uses the system one.

const MAILBOX = {
  system: {
    title: 'بريد النظام (الرموز والإشعارات)', base: '/api/admin/smtp',
    note: 'يرسل رموز الاستعادة والسحب والاسترجاع، والعضوية والمكافآت والظرف الأحمر، وتنبيهات الأمان. تنتهي رسائله بـ«رسالة آلية، لا ترد عليها». بدون إعداد: الاستعادة يدوية عبر Telegram، والرموز الأخرى غير متاحة.',
    on: 'يعمل — تُرسل الرموز والإشعارات تلقائيًا',
    off: 'غير مُعدّ — وضع يدوي: يصلك رمز الاستعادة في Telegram لترسله أنت إلى المستخدم',
  },
  support: {
    title: 'بريد الدعم (التذاكر والردود)', base: '/api/admin/smtp/support',
    note: 'يرسل إشعارات التذاكر إلى صندوق الدعم وردودكم إلى المستخدمين. عنوانه هو «بريد الدعم الرسمي» الظاهر في صفحة «تواصل معنا»، ويُستعمل Reply-To لرسائل النظام.',
    on: 'يعمل — من حساب الدعم',
    off: 'غير مُعدّ بعد — تُرسل رسائل الدعم من بريد النظام مؤقتًا',
  },
};

function smtpSection(profile) {
  const M = MAILBOX[profile];
  const name = appName();
  const status = h('div', {}, spinner());
  const host = h('input', { class: 'input', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', maxlength: '253', placeholder: 'smtp.gmail.com' });
  const port = h('input', { class: 'input', dir: 'ltr', type: 'number', min: '1', max: '65535', value: '587', inputmode: 'numeric' });
  const security = h('select', { class: 'input admin-select', dir: 'ltr' },
    h('option', { value: 'starttls', text: 'STARTTLS (587)' }), h('option', { value: 'ssl', text: 'SSL (465)' }));
  const user = h('input', { class: 'input', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', autocapitalize: 'off', maxlength: '200', placeholder: profile === 'support' ? 'support.you@gmail.com' : 'no-reply.you@gmail.com' });
  const pass = h('input', { class: 'input', dir: 'ltr', type: 'password', autocomplete: 'new-password', maxlength: '200' });
  const sender = h('input', { class: 'input', dir: 'ltr', autocomplete: 'off', maxlength: '200', placeholder: `${name} <you@gmail.com>` });
  const testTo = h('input', { class: 'input', dir: 'ltr', type: 'email', autocomplete: 'off', maxlength: '254', placeholder: 'you@gmail.com' });
  const code = h('input', { class: 'input', dir: 'ltr', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6', placeholder: '123456' });
  const save = h('button', { type: 'submit', class: 'btn btn--primary btn--block' }, icon('bell'), 'حفظ وإرسال بريد تجربة');
  const removeBtn = h('button', { type: 'button', class: 'btn btn--danger btn--sm', hidden: true }, 'إزالة الإعداد');
  const testBtn = h('button', { type: 'button', class: 'btn btn--ghost btn--sm', hidden: true }, 'إرسال تجربة');
  const gmail = h('button', { type: 'button', class: 'btn btn--ghost btn--sm' }, 'إعداد Gmail');
  const senderFor = (addr) => `${name}${profile === 'support' ? ' — الدعم' : ''} <${addr}>`;
  gmail.addEventListener('click', () => {
    host.value = 'smtp.gmail.com'; port.value = '587'; security.value = 'starttls';
    if (!sender.value && user.value) sender.value = senderFor(user.value.trim());
    user.focus();
  });
  user.addEventListener('change', () => { if (!sender.value && user.value.includes('@')) sender.value = senderFor(user.value.trim()); });

  const paint = (s) => {
    const own = s.configured && !s.inherited;
    status.replaceChildren(rows(own
      ? [['الحالة', M.on, 'admin-ok'], ['المصدر', SOURCE[s.source] || s.source],
        ['الخادم', `${s.host}:${s.port} (${s.security})`], ['المرسل', s.sender || '—'], ['كلمة المرور', s.password_set ? 'محفوظة' : '—'],
        ...(profile === 'support' ? [['بريد الدعم الرسمي', s.support_address || '—']] : [])]
      : [['الحالة', M.off, 'admin-warn'], ...(profile === 'support' && s.support_address ? [['بريد الدعم الرسمي', s.support_address]] : [])]));
    if (own) {
      host.value = s.host || ''; port.value = String(s.port || 587); security.value = s.security === 'ssl' ? 'ssl' : 'starttls';
      user.value = s.username || ''; sender.value = s.sender || '';
      pass.placeholder = s.password_set ? 'اتركها فارغة للإبقاء على الحالية' : '';
    }
    removeBtn.hidden = !(own && s.source === 'panel');
    testBtn.hidden = !s.configured;
  };
  const load = async () => { const s = await attempt(() => call('GET', M.base)); if (s) paint(s); };

  const form = h('form', {
    class: 'admin-form',
    onsubmit: async (e) => {
      e.preventDefault();
      if (!/^[0-9]{6}$/.test(code.value.trim())) { toast('اكتب رمز التحقق من تطبيق المصادقة (6 أرقام).', 'error'); code.focus(); return; }
      save.disabled = true;
      const s = await attempt(() => call('PUT', M.base, {
        host: host.value.trim(), port: Math.trunc(Number(port.value) || 0), security: security.value, username: user.value.trim(),
        password: pass.value, sender: sender.value.trim(), test_to: testTo.value.trim(), code: code.value.trim(),
      }));
      save.disabled = false;
      pass.value = '';
      code.value = '';
      if (!s) return;
      paint(s);
      if (s.test_error) toast(s.test_error, 'error');
      else toast(testTo.value ? 'حُفظ ✅ — تحقق من وصول بريد التجربة.' : 'حُفظ ✅');
    },
  },
  h('p', { class: 'admin-meta', text: 'Gmail: أنشئ الحساب ← فعّل التحقق بخطوتين في حساب Google ← أنشئ «كلمة مرور التطبيقات» (App Password) ← الصقها هنا. الخادم smtp.gmail.com والمنفذ 587 (STARTTLS)، واسم المستخدم هو عنوان Gmail نفسه.' }),
  h('div', { class: 'admin-actions' }, gmail),
  h('div', { class: 'admin-filters__row' }, field('الخادم (SMTP)', host), field('المنفذ', port)),
  field('التشفير', security),
  field('اسم المستخدم (البريد)', user),
  field('كلمة المرور (App Password)', pass),
  field('اسم وعنوان المرسل', sender),
  field('أرسل بريد تجربة إلى (اختياري)', testTo),
  field('رمز التحقق من تطبيق المصادقة', code),
  save);

  testBtn.addEventListener('click', async () => {
    const to = testTo.value.trim();
    if (!to) { toast('اكتب عنوانًا في «أرسل بريد تجربة إلى».', 'error'); testTo.focus(); return; }
    if (await attempt(() => call('POST', `${M.base}/test`, { to }))) toast('أُرسل ✅ — تحقق من صندوق الوارد.');
  });
  removeBtn.addEventListener('click', async () => {
    if (!/^[0-9]{6}$/.test(code.value.trim())) { toast('اكتب رمز التحقق أولًا.', 'error'); code.focus(); return; }
    const after = profile === 'support' ? 'تُرسل رسائل الدعم من بريد النظام.' : 'تعود الاستعادة إلى الوضع اليدوي (أو إعداد ملف .env إن وُجد).';
    if (!(await confirmDanger('إزالة إعداد البريد؟', after, 'إزالة'))) return;
    const s = await attempt(() => call('POST', `${M.base}/remove`, { code: code.value.trim() }));
    code.value = '';
    if (s) { paint(s); toast('أُزيل الإعداد.'); }
  });

  load();
  return card(M.title, 'bell', h('p', { class: 'admin-meta', text: M.note }), status, h('div', { class: 'admin-actions' }, testBtn, removeBtn), form);
}

function blocksSection(blocks) {
  const list = h('div', { class: 'admin-events glass' });
  const paint = (items) => list.replaceChildren(...(items.length ? items.map((b) => h('div', { class: 'admin-event admin-event--risk' },
    h('div', { class: 'admin-event__top' },
      h('span', { class: 'admin-event__type' }, icon('shield'),
        b.kind === 'reset' ? 'حظر طلبات الاستعادة' : `حظر دخول (${BLOCK_SCOPE[b.scope] || b.scope})`),
      h('time', { class: 'admin-event__time', text: `حتى ${when(b.until)}` })),
    h('div', { class: 'admin-event__meta' },
      h('span', {}, 'البصمة: ', h('code', { dir: 'ltr', text: b.ref })),
      h('span', {}, `محاولات: ${fmt(b.failures)}`),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: async () => {
        if (!(await confirmDanger('رفع الحظر؟', 'تستطيع هذه الشبكة المحاولة من جديد فورًا.', 'رفع الحظر'))) return;
        if (await attempt(() => call('DELETE', `/api/admin/ip-blocks/${encodeURIComponent(b.id)}`))) {
          toast('رُفع الحظر.');
          paint(items.filter((x) => x.id !== b.id));
          hooks.refreshStats();
        }
      } }, 'رفع الحظر'))))
    : [h('p', { class: 'admin-events__empty', text: 'لا حظر نشط الآن.' })]));
  paint(blocks);
  return h('section', { class: 'admin-section' },
    h('h3', { class: 'admin-group__title' }, icon('shield'), `الحظر النشط (${fmt(blocks.length)})`),
    h('p', { class: 'admin-meta', text: 'الشبكات ببصمة مشفّرة مختصرة فقط — عناوين IP الحقيقية لا تُخزَّن.' }),
    list);
}

function resetsSection(resets) {
  return h('section', { class: 'admin-section' },
    h('h3', { class: 'admin-group__title' }, icon('lock'), 'طلبات استعادة كلمة المرور'),
    resets.length ? h('div', { class: 'admin-events glass' }, ...resets.map((r) => h('div', { class: 'admin-event' },
      h('div', { class: 'admin-event__top' },
        h('span', { class: 'admin-event__type' }, h('code', { dir: 'ltr', text: r.id }), ' ', chip(RESET_STATUS[r.status] || r.status, r.status === 'pending' ? 'chip--hot' : '')),
        h('time', { class: 'admin-event__time', text: when(r.created_at) })),
      h('div', { class: 'admin-event__meta' }, authorLine(r.user), h('span', {}, `محاولات الرمز: ${fmt(r.attempts)}`)))))
      : emptyState('لا طلبات.'));
}

function loginsSection(events) {
  return h('section', { class: 'admin-section' },
    h('h3', { class: 'admin-group__title' }, icon('flag'), 'محاولات الدخول الفاشلة والحظر'),
    events.length ? h('div', { class: 'admin-events glass' }, ...events.map((e) => h('div', { class: 'admin-event admin-event--risk' },
      h('div', { class: 'admin-event__top' },
        h('span', { class: 'admin-event__type', text: LOGIN_TYPES[e.type] || e.type }),
        h('time', { class: 'admin-event__time', text: when(e.at) })),
      h('div', { class: 'admin-event__meta' },
        e.user_id ? h('span', {}, 'حساب: ', userRef(e.user_id)) : null,
        e.ip_ref ? h('span', {}, 'شبكة: ', h('code', { dir: 'ltr', text: e.ip_ref })) : null))))
      : emptyState('لا محاولات فاشلة.'));
}

function cleanupCard() {
  const result = h('dl', { class: 'admin-group__rows', hidden: true });
  const run = h('button', { type: 'button', class: 'btn btn--ghost btn--block' }, icon('trash'), 'تشغيل التنظيف الآن');
  run.addEventListener('click', async () => {
    run.disabled = true;
    const r = await attempt(() => call('POST', '/api/admin/cleanup'));
    run.disabled = false;
    if (!r) return;
    const total = Object.values(r.deleted).reduce((a, b) => a + b, 0);
    result.replaceChildren(...Object.entries(r.deleted).map(([k, v]) => h('div', { class: 'admin-row' },
      h('dt', { text: CLEANUP_LABELS[k] || k }), h('dd', { text: fmt(v) }))));
    result.hidden = false;
    toast(total ? `تم حذف ${fmt(total)} عنصرًا منتهيًا.` : 'لا يوجد شيء منتهٍ للحذف.');
    hooks.refreshStats();
  });
  return card('تنظيف البيانات المنتهية', 'clock',
    h('p', { class: 'admin-meta', text: 'يعمل تلقائيًا بشكل دوري: الرسائل والمحادثات المنتهية، الجلسات القديمة، وسجلات الأمان والبلاغات المغلقة القديمة.' }),
    run, result);
}
