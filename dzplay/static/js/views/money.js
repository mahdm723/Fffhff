// V5: monetization (verified creators) and «أموالي» — read only: balance, total earnings, history.
// Payouts are sent by the team as a Red Packet to the payout e-mail; nothing here can move money.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { formatDay, h, toast } from '../ui.js';

const STATUS_NOTE = {
  pending: 'طلبك قيد المراجعة. ستصلك النتيجة هنا.',
  rejected: 'رُفض طلبك.',
  needs_fix: 'طلبك يحتاج تصحيحًا. عدّله وأعد الإرسال.',
};

export function renderMoney(page, { navigate }) {
  const body = h('div');
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'الأرباح' })),
    body);

  function bar(item) {
    const pct = item.target > 0 ? Math.min(100, Math.round((item.value / item.target) * 100)) : 100;
    const fill = h('span');
    fill.style.width = `${pct}%`;
    return h('li', { class: `cond ${item.met ? 'is-met' : ''}` },
      h('div', { class: 'cond__head' }, h('span', { text: item.label }),
        h('span', { class: 'cond__val' }, item.met ? icon('check') : null, h('bdi', { dir: 'ltr', text: `${item.value} / ${item.target}` }))),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuenow': String(pct) }, fill));
  }

  function wallet(m) {
    const list = m.history.length
      ? h('ul', { class: 'ledger' }, ...m.history.map((e) => {
        const neg = e.amount.startsWith('-');
        return h('li', { class: 'glass' },
          h('span', {}, h('b', { text: e.kind_label }), h('br'),
            h('small', { class: 'muted', text: `${e.paid_on || formatDay(e.created_at)}${e.note ? ` · ${e.note}` : ''}` })),
          h('span', { class: `ledger__amt ${neg ? 'ledger__amt--neg' : 'ledger__amt--pos'}`, text: `${neg ? '' : '+'}${e.amount} ${e.currency}` }));
      }))
      : h('p', { class: 'muted', text: 'لا توجد حركات بعد.' });
    body.replaceChildren(
      h('section', { class: 'card glass money-balance' },
        h('small', { text: 'رصيدك الحالي' }),
        h('div', { class: 'money-balance__num', text: `${m.balance} ${m.currency}` }),
        h('small', { text: `إجمالي الأرباح: ${m.total_earned} ${m.currency}` })),
      h('p', { class: 'muted' }, icon('info'), ` تُرسل الدفعات عبر «الظرف الأحمر» إلى ${m.payout_email || 'بريدك'}. يحدد الفريق الأرباح وفق شروط تحقيق الدخل.`),
      h('h2', { class: 'section-title', text: 'السجل' }),
      list);
  }

  function application(ov) {
    const a = ov.application;
    const nodes = [h('section', { class: 'card glass' },
      h('h2', { class: 'card__title', text: 'تحقيق الدخل' }),
      h('p', { class: 'card__note', text: 'للحسابات الموثّقة التي تنشر فيديوهات من الاستوديو. الأرباح يحددها الفريق وتُرسل عبر «الظرف الأحمر».' }))];
    if (a && STATUS_NOTE[a.status]) {
      nodes.push(h('section', { class: `card glass req req--${a.status}` }, h('h2', { class: 'card__title', text: a.status_label }),
        h('p', { class: 'muted', text: STATUS_NOTE[a.status] }), a.admin_note ? h('p', { class: 'verify-note', dir: 'auto', text: a.admin_note }) : null));
    }
    nodes.push(h('h2', { class: 'section-title', text: 'الشروط' }), h('ul', { class: 'conds glass' }, ...ov.conditions.items.map(bar)));
    const canApply = ov.enabled && ov.conditions.met && (!a || ['rejected', 'needs_fix'].includes(a.status));
    if (!canApply) {
      if (!a || a.status !== 'pending') nodes.push(h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, 'أكمل الشروط لتقديم الطلب'));
      body.replaceChildren(...nodes);
      return;
    }
    const content = h('input', { class: 'input', id: 'monetize-content', maxlength: '200', dir: 'auto', placeholder: 'نوع المحتوى (مثال: مقاطع تعليمية)' });
    const email = h('input', { class: 'input', id: 'monetize-email', type: 'email', dir: 'ltr', value: (a && a.payout_email) || ov.account_email });
    const code = h('input', { class: 'input', id: 'monetize-code', inputmode: 'numeric', maxlength: '6', dir: 'ltr', placeholder: '000000' });
    const codeRow = h('div', { class: 'field', hidden: true }, h('label', { for: 'monetize-code', text: 'رمز التأكيد المرسل إلى هذا البريد' }), code);
    const sendCode = h('button', { class: 'btn btn--ghost btn--sm', type: 'button', hidden: true }, 'إرسال رمز التأكيد');
    const terms = h('input', { type: 'checkbox', id: 'monetize-terms' });
    const differs = () => email.value.trim().toLowerCase() !== ov.account_email.toLowerCase();
    email.addEventListener('input', () => { sendCode.hidden = !differs(); codeRow.hidden = !differs(); });
    sendCode.addEventListener('click', async () => {
      try { await api.post('/api/monetization/email-code', { email: email.value.trim() }); toast('أُرسل الرمز إلى هذا البريد.'); }
      catch (err) { toast(err.message, 'error'); }
    });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال الطلب');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await api.post('/api/monetization', { content_type: content.value.trim(), payout_email: email.value.trim(), code: code.value.trim() || null, terms: terms.checked });
        toast('أُرسل طلبك.');
        load();
      } catch (err) { toast(err.message, 'error', 5000); submit.disabled = false; }
    });
    nodes.push(h('section', { class: 'card glass', 'data-kb-block': '' },
      h('div', { class: 'field' }, h('label', { for: 'monetize-content', text: 'نوع المحتوى' }), content),
      h('div', { class: 'field' }, h('label', { for: 'monetize-email', text: 'بريد استلام الأرباح' }), email, sendCode),
      codeRow,
      h('label', { class: 'choice' }, terms, h('span', { text: 'أوافق على شروط تحقيق الدخل: الأرباح تقديرية يحددها الفريق، وتُرسل عبر الظرف الأحمر إلى البريد المؤكد.' })),
      h('a', { class: 'link-btn', href: '/policies/earnings', target: '_blank', rel: 'noopener' }, 'شروط تحقيق الدخل'),
      submit));
    body.replaceChildren(...nodes);
  }

  async function load() {
    try {
      const ov = await api.get('/api/monetization');
      if (ov.accepted) wallet(await api.get('/api/money'));
      else application(ov);
    } catch (err) { body.replaceChildren(h('p', { class: 'muted', text: err.message })); }
  }
  load();
  const onMoney = () => load();
  document.addEventListener('dz:money', onMoney);
  document.addEventListener('dz:account', onMoney);
  return () => { document.removeEventListener('dz:money', onMoney); document.removeEventListener('dz:account', onMoney); };
}
