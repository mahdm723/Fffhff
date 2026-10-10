// V6 phase 5: «عضويتي» — features only (the blue star, ideas with a picture, chat pictures). Never any return.
// Pay once to the wallet shown (QR + copy), send the TXID, the team checks it. Optional refund within the window.
import { contentBlock } from '../content.js';
import { api } from '../api.js';
import { icon } from '../icons.js';
import { copyText, formatDay, h, starMark, toast } from '../ui.js';

export function renderMembership(page, { navigate, onMe }) {
  const body = h('div', { class: 'membership' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'عضويتي' })),
    contentBlock('membership_intro', 'mem-intro glass'),
    body);

  const features = (d) => h('ul', { class: 'mem-features glass' }, ...d.features.map((f) => h('li', {}, icon('check'), h('span', { text: f }))));

  function payBox(d) {
    const p = d.payment;
    if (!p) return h('div', { class: 'pay pay--off glass' }, icon('clock'), h('p', { text: 'الدفع غير متاح حاليًا. حاول لاحقًا.' }));
    const txid = h('input', { class: 'input', id: 'mem-txid', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'TXID / Hash' });
    const send = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال رقم العملية');
    const err = h('div', { class: 'form-error', role: 'alert' });
    send.addEventListener('click', async () => {
      err.textContent = '';
      if (!txid.value.trim()) { err.textContent = 'أدخل رقم العملية (TXID) بعد الدفع.'; return; }
      send.disabled = true;
      try { paint(await api.post('/api/membership/requests', { txid: txid.value.trim() })); toast('أُرسل طلبك وسيُراجع قريبًا.'); }
      catch (e) { err.textContent = e.message; send.disabled = false; }
    });
    return h('section', { class: 'pay glass' },
      h('h3', { text: `ادفع ${d.price} ${p.currency} مرة واحدة على شبكة ${p.network_label}` }),
      h('img', { class: 'pay__qr', src: p.qr, alt: 'رمز QR لعنوان المحفظة', width: '180', height: '180' }),
      h('div', { class: 'pay__wallet' }, h('code', { dir: 'ltr', text: p.wallet }),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(p.wallet, 'نُسخ العنوان.') }, icon('copy'), 'نسخ')),
      h('p', { class: 'pay__warn' }, icon('info'), `أرسل ${p.currency} على شبكة ${p.network_label} فقط. الإرسال على شبكة أخرى يضيع المبلغ ولا يمكن استرجاعه.`),
      p.note ? h('p', { class: 'muted', dir: 'auto', text: p.note }) : '',
      h('div', { class: 'field' }, h('label', { for: 'mem-txid', text: 'رقم العملية بعد الدفع' }), txid), err, send);
  }

  function refundBox(d) {
    const r = d.refund;
    if (!r.enabled) return '';
    const last = r.request;
    if (last && last.status === 'requested') {
      return h('section', { class: 'mem-refund glass' }, h('h3', { text: 'الاسترجاع' }),
        h('p', { text: `طلبك قيد التنفيذ: ${last.amount} USDT إلى عنوانك على ${last.network}.` }));
    }
    if (!r.eligible) {
      return last ? h('section', { class: 'mem-refund glass' }, h('h3', { text: 'الاسترجاع' }),
        h('p', { text: last.status === 'done' ? `تم إرجاع ${last.amount} USDT.` : `رُفض طلب الاسترجاع: ${last.reason || ''}` })) : '';
    }
    const open = h('button', { class: 'btn btn--ghost btn--block', type: 'button' }, 'طلب استرجاع العضوية');
    const box = h('section', { class: 'mem-refund glass' }, h('h3', { text: 'الاسترجاع' }),
      h('p', { text: `يمكنك طلب الاسترجاع حتى ${formatDay(r.deadline)}: يُعاد ${r.amount} USDT (بعد رسوم ${r.fee}) وتتوقف ميزات العضوية.` }),
      open);
    open.addEventListener('click', () => {
      const network = h('select', { class: 'input', id: 'refund-network' }, ...Object.entries(r.networks).map(([k, v]) => h('option', { value: k, text: v })));
      const address = h('input', { class: 'input', id: 'refund-address', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'عنوان محفظتك' });
      const password = h('input', { class: 'input', id: 'refund-password', type: 'password', autocomplete: 'current-password', placeholder: 'كلمة المرور' });
      const code = h('input', { class: 'input', id: 'refund-code', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: 'الرمز من بريدك' });
      const sendCode = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, 'أرسل الرمز إلى بريدي');
      const err = h('div', { class: 'form-error', role: 'alert' });
      const go = h('button', { class: 'btn btn--danger btn--block', type: 'button' }, 'تأكيد طلب الاسترجاع');
      sendCode.addEventListener('click', async () => {
        sendCode.disabled = true;
        try { const s = await api.post('/api/membership/refund/code', {}); toast(`أُرسل الرمز إلى ${s.sent_to}`); }
        catch (e) { err.textContent = e.message; }
        setTimeout(() => { sendCode.disabled = false; }, 30000);
      });
      go.addEventListener('click', async () => {
        err.textContent = '';
        go.disabled = true;
        try {
          paint(await api.post('/api/membership/refund', { network: network.value, address: address.value.trim(), password: password.value, code: code.value.trim() }));
          toast('أُرسل طلب الاسترجاع.');
        } catch (e) { err.textContent = e.message; go.disabled = false; }
      });
      box.replaceChildren(h('h3', { text: 'طلب الاسترجاع' }),
        h('p', { class: 'muted', text: `يصلك ${r.amount} USDT على الشبكة التي تختارها. تأكد من العنوان جيدًا.` }),
        h('div', { class: 'field' }, h('label', { for: 'refund-network', text: 'الشبكة' }), network),
        h('div', { class: 'field' }, h('label', { for: 'refund-address', text: 'عنوان المحفظة' }), address),
        h('div', { class: 'field' }, h('label', { for: 'refund-password', text: 'كلمة المرور' }), password),
        h('div', { class: 'field' }, h('label', { for: 'refund-code', text: 'رمز التأكيد' }), h('div', { class: 'mem-code' }, code, sendCode)),
        err, go);
    });
    return box;
  }

  function paint(d) {
    const req = d.request;
    let status;
    if (d.member) {
      status = h('section', { class: 'mem-status mem-status--on glass' }, starMark(),
        h('div', {}, h('h2', { text: 'عضويتك مفعّلة' }), h('p', { text: `منذ ${formatDay(d.member_since)}` })));
    } else if (req && req.status === 'pending') {
      status = h('section', { class: 'mem-status glass' }, icon('clock'),
        h('div', {}, h('h2', { text: 'طلبك: قيد المراجعة' }), h('p', { dir: 'ltr', class: 'muted', text: req.txid })));
    } else {
      status = h('section', { class: 'mem-status glass' }, icon('verified'),
        h('div', {}, h('h2', { text: `العضوية: ${d.price} ${d.currency} مرة واحدة` }),
          h('p', { text: 'ميزات إضافية داخل التطبيق. لا علاقة لها بالتداول ولا بأي عائد مالي.' })));
    }
    body.replaceChildren(
      status,
      req && req.status === 'rejected' && !d.member ? h('p', { class: 'mem-note', text: `رُفض طلبك السابق: ${req.note || ''}` }) : '',
      h('h3', { class: 'section-title', text: 'ميزات الأعضاء' }), features(d),
      d.member || (req && req.status === 'pending') ? '' : payBox(d),
      d.member ? refundBox(d) : '',
      h('p', { class: 'muted mem-terms' }, 'التفاصيل في ', h('a', { href: '/policies/terms', target: '_blank', rel: 'noopener' }, 'شروط الاستخدام'), '.'));
  }

  async function load() {
    try {
      const d = await api.get('/api/membership');
      paint(d);
      if (onMe) onMe({ member: d.member });
    } catch (err) {
      body.replaceChildren(h('div', { class: 'empty glass' }, icon('info'), h('p', { text: err.message })));
    }
  }
  const onAccount = () => load();
  document.addEventListener('dz:account', onAccount);
  load();
  return () => document.removeEventListener('dz:account', onAccount);
}
