// V5: the blue star — conditions with progress bars, the request form and the (manual) crypto payment.
// The star means "an official, trusted DZPLAY account"; it is not an identity check and asks for no
// personal data or documents.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { copyText, formatDay, h, starMark, toast } from '../ui.js';

function topbar(onBack) {
  return h('header', { class: 'topbar topbar--back' },
    h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: onBack }, icon('back')),
    h('h1', { class: 'page-title', text: 'النجمة الزرقاء' }));
}

export function renderVerify(page, { navigate, onMe }) {
  const body = h('div', { class: 'verify' });
  page.replaceChildren(topbar(() => navigate('#/profile')), body);

  function bar(item) {
    const pct = item.target > 0 ? Math.min(100, Math.round((item.value / item.target) * 100)) : 100;
    const fill = h('span');
    fill.style.width = `${pct}%`; // CSSOM, not a style attribute: allowed by the CSP (style-src 'self')
    const text = item.key === 'clean' ? (item.met ? 'لا توجد مخالفات' : 'توجد مخالفات على حسابك') : `${item.value} / ${item.target}`;
    return h('li', { class: `cond ${item.met ? 'is-met' : ''}` },
      h('div', { class: 'cond__head' }, h('span', { text: item.label }), h('span', { class: 'cond__val' }, item.met ? icon('check') : null, h('bdi', { dir: item.key === 'clean' ? 'rtl' : 'ltr', text }))),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct) }, fill));
  }

  function form(data, existing) {
    const pay = data.payment;
    let type = existing ? existing.account_type : 'writer';
    const types = h('div', { class: 'chips', role: 'radiogroup', 'aria-label': 'نوع الحساب' });
    const paintTypes = () => types.replaceChildren(...Object.entries(data.account_types).map(([id, label]) => h('button', {
      type: 'button', class: `chip-btn ${id === type ? 'is-on' : ''}`, role: 'radio', 'aria-checked': String(id === type),
      onclick: () => { type = id; paintTypes(); },
    }, label)));
    paintTypes();
    const desc = h('textarea', { class: 'input', id: 'verify-desc', rows: '3', maxlength: '1000', dir: 'auto', placeholder: 'ماذا تنشر؟ (مثال: خواطر يومية، شعر، محتوى تعليمي)' });
    const reason = h('textarea', { class: 'input', id: 'verify-reason', rows: '2', maxlength: '1000', dir: 'auto', placeholder: 'لماذا تريد التوثيق؟' });
    const amount = h('input', { class: 'input', id: 'verify-amount', inputmode: 'decimal', dir: 'ltr', placeholder: pay.min_amount ? `≥ ${pay.min_amount}` : '0.00' });
    const txid = h('input', { class: 'input', id: 'verify-txid', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'TXID / Hash' });
    const agree = h('input', { type: 'checkbox', id: 'verify-agree' });
    if (existing) { desc.value = existing.description; reason.value = existing.reason; amount.value = existing.amount; txid.value = existing.txid; }
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: !pay.available },
      existing ? 'إعادة إرسال الطلب' : 'إرسال طلب التوثيق');
    submit.addEventListener('click', async () => {
      if (!agree.checked) { toast('وافق على شروط الدفع أولًا.', 'error'); return; }
      submit.disabled = true;
      const payload = { account_type: type, description: desc.value.trim(), reason: reason.value.trim(), amount: amount.value.trim(), txid: txid.value.trim() };
      try {
        if (existing) await api.put(`/api/verification/${encodeURIComponent(existing.id)}`, payload);
        else await api.post('/api/verification', payload);
        toast('أُرسل طلبك. ستصلك النتيجة هنا وعلى بريدك.');
        load();
      } catch (err) { toast(err.message, 'error', 5000); submit.disabled = false; }
    });
    const payBox = pay.available
      ? h('section', { class: 'pay glass' },
        h('h3', { text: `الدفع: ${pay.currency} على شبكة ${pay.network_label}` }),
        h('img', { class: 'pay__qr', src: pay.qr, alt: 'رمز QR لعنوان المحفظة', width: '180', height: '180' }),
        h('div', { class: 'pay__wallet' }, h('code', { dir: 'ltr', text: pay.wallet }),
          h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(pay.wallet, 'نُسخ العنوان.') }, icon('copy'), 'نسخ')),
        h('p', { class: 'pay__warn' }, icon('info'), pay.warning),
        pay.note ? h('p', { class: 'muted', text: pay.note }) : null,
        h('p', { class: 'muted', text: pay.min_amount ? `المبلغ حر، وأقله ${pay.min_amount} ${pay.currency}.` : 'المبلغ حر: أرسل ما تراه مناسبًا.' }),
        h('div', { class: 'field' }, h('label', { for: 'verify-amount', text: `المبلغ الذي أرسلته (${pay.currency})` }), amount),
        h('div', { class: 'field' }, h('label', { for: 'verify-txid', text: 'رقم العملية (TXID)' }), txid))
      : h('section', { class: 'pay glass pay--off' }, icon('lock'), h('p', { text: 'الدفع غير متاح حاليًا. عُد لاحقًا.' }));
    return h('section', { class: 'card glass', 'data-kb-block': '' },
      h('h2', { class: 'card__title', text: existing ? 'تعديل الطلب' : 'طلب التوثيق' }),
      h('p', { class: 'card__note', text: 'لا نطلب اسمك الحقيقي ولا أي وثيقة. هذه المعلومات تُستعمل لمراجعة الطلب فقط.' }),
      types,
      h('div', { class: 'field' }, h('label', { for: 'verify-desc', text: 'وصف المحتوى' }), desc),
      h('div', { class: 'field' }, h('label', { for: 'verify-reason', text: 'سبب الطلب' }), reason),
      payBox,
      h('label', { class: 'choice' }, agree, h('span', { text: 'أفهم أن الدفع بالعملات الرقمية نهائي ولا يُسترجع، وأن الإرسال عبر شبكة خاطئة مسؤوليتي، وأن النجمة ليست تحققًا من هويتي ويمكن سحبها عند المخالفة.' })),
      h('a', { class: 'link-btn', href: '/policies/verification', target: '_blank', rel: 'noopener' }, 'شروط التوثيق والدفع'),
      submit);
  }

  async function load() {
    let data;
    try { data = await api.get('/api/verification'); } catch (err) { body.replaceChildren(h('p', { class: 'muted', text: err.message })); return; }
    const nodes = [h('section', { class: 'card glass verify-intro' },
      h('div', { class: 'verify-intro__star' }, starMark()),
      h('p', { text: 'النجمة الزرقاء تعني أن الحساب رسمي وموثوق داخل DZPLAY. ليست تحققًا من الهوية الحقيقية، ولا تُطلب أي وثائق. يمكن سحبها عند مخالفة الإرشادات.' }))];
    if (data.verified) {
      nodes.push(h('section', { class: 'card glass verify-done' }, starMark(), h('h2', { text: 'حسابك موثّق' }),
        h('p', { class: 'muted', text: `منذ ${formatDay(data.verified_at)}` })));
      if (onMe) onMe({ verified: true });
      body.replaceChildren(...nodes);
      return;
    }
    const req = data.request;
    if (req && req.status !== 'accepted') {
      const note = req.admin_note ? h('p', { class: 'verify-note', dir: 'auto', text: req.admin_note }) : null;
      nodes.push(h('section', { class: `card glass req req--${req.status}` },
        h('h2', { class: 'card__title', text: `طلبك: ${req.status_label}` }),
        h('p', { class: 'muted', text: `${req.amount} ${req.currency} · ${req.network} · ${formatDay(req.created_at)}` }), note));
    }
    if (!data.enabled) { // V6: the star comes with memberships (phase 5); existing stars are kept
      nodes.push(h('p', { class: 'muted', text: 'طلبات النجمة الزرقاء متوقفة: ستصبح النجمة جزءًا من العضوية قريبًا.' }));
      body.replaceChildren(...nodes);
      return;
    }
    nodes.push(h('h2', { class: 'section-title', text: 'الشروط' }),
      h('ul', { class: 'conds glass' }, ...data.conditions.items.map(bar)));
    if (req && req.status === 'needs_fix') nodes.push(form(data, req));
    else if (req && req.status === 'pending') nodes.push(h('p', { class: 'muted', text: 'طلبك قيد المراجعة. ستصلك النتيجة هنا وعلى بريدك.' }));
    else if (data.conditions.met) nodes.push(form(data, null));
    else nodes.push(h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, 'أكمل الشروط لتقديم الطلب'));
    body.replaceChildren(...nodes);
  }
  load();
  const onAccount = () => load(); // decided from the panel / Telegram: live update
  document.addEventListener('dz:account', onAccount);
  return () => document.removeEventListener('dz:account', onAccount);
}
