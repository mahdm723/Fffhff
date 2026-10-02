// Admin panel — security & system: Telegram bot, e-mail, media cache, network blocks (lift),
// password-reset requests, failed sign-ins, data cleanup and the admin-account CLI.
import { h, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  attempt, authorLine, bytes, call, chip, confirmDanger, emptyState, fmt, hooks, sectionHead, spinner, userRef, when,
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

  const bot = d.bot;
  const hook = bot.webhook;
  const botRows = [['البوت', bot.configured ? 'مُعدّ' : 'غير مُعدّ (TELEGRAM_BOT_TOKEN فارغ)', bot.configured ? 'admin-ok' : 'admin-warn']];
  if (hook) {
    botRows.push(['Webhook', hook.url_set ? 'مضبوط' : 'غير مضبوط', hook.url_set ? 'admin-ok' : 'admin-warn'],
      ['تحديثات بالانتظار', fmt(hook.pending_updates || 0)]);
    if (hook.last_error) botRows.push(['آخر خطأ من Telegram', hook.last_error, 'admin-warn']);
  }
  if (bot.error) botRows.push(['تعذّر الاتصال بـ Telegram', bot.error, 'admin-warn']);

  const used = d.media_cache.bytes;
  const limit = d.media_cache.limit_bytes || 1;
  const pct = Math.min(100, Math.round((used / limit) * 100));
  const fill = h('span', {});
  fill.style.inlineSize = `${pct}%`;

  body.replaceChildren(
    h('div', { class: 'admin-groups' },
      card('بوت Telegram', 'send', rows(botRows)),
      card('البريد (استعادة كلمة المرور)', 'bell', rows([
        ['SMTP', d.smtp_configured ? 'مُعدّ' : 'غير مُعدّ — الرموز تُرسل عبر Telegram فقط', d.smtp_configured ? 'admin-ok' : 'admin-warn']])),
      card('ذاكرة الوسائط المؤقتة', 'reels',
        h('div', { class: 'admin-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct),
          'aria-label': 'استخدام الذاكرة المؤقتة' }, fill),
        h('p', { class: 'admin-meta', text: `${bytes(used)} من ${bytes(limit)} (${fmt(pct)}٪). الأقدم استخدامًا يُحذف تلقائيًا ويُعاد تنزيله من Telegram عند الحاجة.` }))),
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
