// V6 phase 5b: «أرباحي» (owner only) — promotional rewards, separate from the membership: available / on hold /
// total from the ledger, the history by source, and «سحب» (TRC20 / BEP20, fee shown before confirming).
import { appName } from '../brand.js';
import { api } from '../api.js';
import { icon } from '../icons.js';
import { formatDay, h, sheet, toast } from '../ui.js';

const money = (v) => `${v} USDT`;

export function renderEarnings(page, { navigate }) {
  const body = h('div', { class: 'earnings' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'أرباحي' })),
    body);

  function withdrawSheet(d) {
    const w = d.withdraw;
    sheet((panel, close) => {
      const network = h('select', { class: 'input', id: 'wd-network' }, ...Object.entries(w.networks).map(([k, v]) => h('option', { value: k, text: v })));
      const address = h('input', { class: 'input', id: 'wd-address', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'عنوان محفظتك' });
      const amount = h('input', { class: 'input', id: 'wd-amount', dir: 'ltr', inputmode: 'decimal', placeholder: `${w.min} أو أكثر` });
      const password = h('input', { class: 'input', id: 'wd-password', type: 'password', autocomplete: 'current-password', placeholder: 'كلمة المرور' });
      const code = h('input', { class: 'input', id: 'wd-code', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: 'الرمز من بريدك' });
      const sendCode = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'أرسل الرمز');
      const summary = h('p', { class: 'wd-summary' });
      const err = h('div', { class: 'form-error', role: 'alert' });
      const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'تأكيد السحب');
      const paintSummary = () => {
        const a = Number(amount.value.replace(',', '.'));
        const fee = Number(w.fee);
        summary.replaceChildren(...(a > 0
          ? ['المبلغ ', h('b', { dir: 'ltr', text: money(a.toFixed(2)) }), ' · الرسوم ', h('b', { dir: 'ltr', text: money(w.fee) }),
            ' · يصلك ', h('b', { dir: 'ltr', text: money(Math.max(0, a - fee).toFixed(2)) })]
          : [`المتاح ${money(d.balances.available)} · الرسوم ${money(w.fee)} تُخصم من المبلغ`]));
      };
      amount.addEventListener('input', paintSummary);
      paintSummary();
      sendCode.addEventListener('click', async () => {
        sendCode.disabled = true;
        try { const s = await api.post('/api/rewards/withdraw/code', {}); toast(`أُرسل الرمز إلى ${s.sent_to}`); }
        catch (e) { err.textContent = e.message; }
        setTimeout(() => { sendCode.disabled = false; }, 30000);
      });
      go.addEventListener('click', async () => {
        err.textContent = '';
        go.disabled = true;
        try {
          await api.post('/api/rewards/withdraw', { network: network.value, address: address.value.trim(), amount: amount.value.trim().replace(',', '.'), password: password.value, code: code.value.trim() });
          close();
          toast('أُرسل طلب السحب. سيصلك إشعار عند التنفيذ.');
          load();
        } catch (e) { err.textContent = e.message; go.disabled = false; }
      });
      panel.append(h('h2', { text: 'سحب المكافآت' }),
        h('p', { class: 'muted', text: 'تأكد من الشبكة والعنوان: التحويل إلى عنوان خاطئ لا يمكن استرجاعه.' }),
        h('div', { class: 'field' }, h('label', { for: 'wd-network', text: 'الشبكة' }), network),
        h('div', { class: 'field' }, h('label', { for: 'wd-address', text: 'عنوان المحفظة' }), address),
        h('div', { class: 'field' }, h('label', { for: 'wd-amount', text: 'المبلغ (USDT)' }), amount),
        summary,
        h('div', { class: 'field' }, h('label', { for: 'wd-password', text: 'كلمة المرور' }), password),
        h('div', { class: 'field' }, h('label', { for: 'wd-code', text: 'رمز التأكيد' }), h('div', { class: 'mem-code' }, code, sendCode)),
        err, h('div', { class: 'actions' }, go, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
    });
  }

  function paint(d) {
    const b = d.balances;
    const w = d.withdraw;
    const tile = (label, value, cls = '') => h('div', { class: `earn-tile glass ${cls}` }, h('small', { text: label }), h('b', { dir: 'ltr', text: value }));
    const canWithdraw = w.enabled && !w.pending && Number(b.available) >= Number(w.min);
    body.replaceChildren(
      h('div', { class: 'earn-tiles' }, tile('المتاح', b.available, 'earn-tile--main'), tile('معلّق', b.pending), tile('الإجمالي', b.total)),
      h('p', { class: 'muted earn-note', text: d.note }),
      w.pending ? h('section', { class: 'mem-refund glass' }, h('h3', { text: 'طلب سحب قيد التنفيذ' }),
        h('p', { text: `${money(w.pending.net)} إلى ${w.pending.network_label} (طُلب ${formatDay(w.pending.created_at)}).` })) : '',
      h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: !canWithdraw, onclick: () => withdrawSheet(d) },
        icon('send'), w.enabled ? `سحب (الحد الأدنى ${w.min} USDT)` : 'السحب متوقف حاليًا'),
      h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => navigate('#/referrals') }, icon('plusUser'), 'دعوة الأصدقاء'),
      h('h3', { class: 'section-title', text: 'السجل' }),
      d.entries.length
        ? h('ul', { class: 'earn-list' }, ...d.entries.map((e) => h('li', { class: 'earn-item glass' },
          h('div', {}, h('b', { text: e.label }), e.note ? h('small', { dir: 'auto', text: e.note }) : '',
            h('small', { class: 'muted', text: `${formatDay(e.created_at)}${e.pending ? ` · معلّق حتى ${formatDay(e.available_at)}` : ''}` })),
          h('b', { class: `earn-amount ${e.amount_minor < 0 ? 'is-neg' : 'is-pos'}`, dir: 'ltr', text: `${e.amount_minor > 0 ? '+' : ''}${e.amount}` }))))
        : h('div', { class: 'empty glass' }, icon('inbox'), h('p', { text: 'لا حركات بعد. ادعُ أصدقاءك أو شارك في المسابقات.' })),
      w.history.length ? h('h3', { class: 'section-title', text: 'طلبات السحب' }) : '',
      w.history.length ? h('ul', { class: 'earn-list' }, ...w.history.map((x) => h('li', { class: 'earn-item glass' },
        h('div', {}, h('b', { text: x.status_label }), h('small', { class: 'muted', text: `${x.network_label} · ${formatDay(x.created_at)}` }),
          x.reason ? h('small', { dir: 'auto', text: x.reason }) : ''),
        h('b', { class: 'earn-amount', dir: 'ltr', text: x.net })))) : '');
  }

  async function load() {
    try { paint(await api.get('/api/rewards')); }
    catch (err) { body.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('p', { text: err.message }))); }
  }
  const onAccount = () => load();
  document.addEventListener('dz:account', onAccount);
  load();
  return () => document.removeEventListener('dz:account', onAccount);
}

export function renderReferrals(page, { navigate }) {
  const body = h('div', { class: 'referrals' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/earnings') }, icon('back')),
      h('h1', { class: 'page-title', text: 'دعوة الأصدقاء' })),
    body);
  (async () => {
    try {
      const d = await api.get('/api/rewards/referrals');
      const link = d.link.startsWith('http') ? d.link : `${location.origin}${d.link}`;
      const share = async () => {
        const text = `انضم إلى ${appName()}: مجتمع للمتداولين لمشاركة الأخبار والتحليلات والأفكار.`;
        try {
          if (window.DZPLAYAndroid) { window.DZPLAYAndroid.share(`${text} ${link}`); return; }
          if (navigator.share) { await navigator.share({ title: appName(), text, url: link }); return; }
          await navigator.clipboard.writeText(link);
          toast('نُسخ الرابط.');
        } catch (e) { if (e && e.name !== 'AbortError') toast(link, 'info', 8000); }
      };
      const stat = (n, label) => h('div', { class: 'stat glass' }, h('div', { class: 'stat__num', text: String(n) }), h('div', { class: 'stat__label', text: label }));
      body.replaceChildren(
        h('section', { class: 'ref-card glass' },
          h('p', { text: d.enabled
            ? `عندما يصبح صديقك عضوًا تحصل على ${d.reward} USDT في «أرباحي»، معلّقة ${d.hold_days} يومًا ثم تصبح متاحة. مستوى واحد فقط، ولا مكافأة على دعوة نفسك.`
            : 'مكافأة الدعوة متوقفة حاليًا. يمكنك مشاركة الرابط مع ذلك.' }),
          h('div', { class: 'pay__wallet' }, h('code', { dir: 'ltr', class: 'ref-link', text: link }),
            h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => navigator.clipboard.writeText(link).then(() => toast('نُسخ الرابط.'), () => toast(link)) }, icon('copy'), 'نسخ')),
          h('button', { class: 'btn btn--primary btn--block', type: 'button', onclick: share }, icon('send'), 'مشاركة الرابط')),
        h('div', { class: 'stats' }, stat(d.invited, 'سجّلوا'), stat(d.members, 'أصبحوا أعضاء'), stat(d.earned, 'مكافآت (USDT)')));
    } catch (err) {
      body.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('p', { text: err.message })));
    }
  })();
}
