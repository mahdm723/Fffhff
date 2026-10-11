// V6 phase 5: «عضويتي» — features only (the blue star, ideas with a picture, chat pictures). Never any return.
// Pay once to the wallet shown (QR + copy), send the TXID, the team checks it. Optional refund within the window.
import { contentBlock } from '../content.js';
import { api } from '../api.js';
import { icon } from '../icons.js';
import { copyText, formatDay, h, starMark, toast } from '../ui.js';
import { fmtMoney, t } from '../i18n.js';

export function renderMembership(page, { navigate, onMe }) {
  const body = h('div', { class: 'membership' }, h('div', { class: 'feed-status' }, h('span', { class: 'spinner', 'aria-label': t('جارٍ التحميل') })));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': t('رجوع'), onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: t('عضويتي') })),
    contentBlock('membership_intro', 'mem-intro glass'),
    body);

  const features = (d) => h('ul', { class: 'mem-features glass' }, ...d.features.map((f) => h('li', {}, icon('check'), h('span', { text: f }))));

  function payBox(d) {
    const p = d.payment;
    if (!p) return h('div', { class: 'pay pay--off glass' }, icon('clock'), h('p', { text: t('الدفع غير متاح حاليًا. حاول لاحقًا.') }));
    const txid = h('input', { class: 'input', id: 'mem-txid', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'TXID / Hash' });
    const send = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, t('إرسال رقم العملية'));
    const err = h('div', { class: 'form-error', role: 'alert' });
    send.addEventListener('click', async () => {
      err.textContent = '';
      if (!txid.value.trim()) { err.textContent = t('أدخل رقم العملية (TXID) بعد الدفع.'); return; }
      send.disabled = true;
      try { paint(await api.post('/api/membership/requests', { txid: txid.value.trim() })); toast(t('أُرسل طلبك وسيُراجع قريبًا.')); }
      catch (e) { err.textContent = e.message; send.disabled = false; }
    });
    return h('section', { class: 'pay glass' },
      h('h3', { text: t('ادفع {price} {currency} مرة واحدة على شبكة {network_label}', { price: d.price, currency: p.currency, network_label: p.network_label }) }),
      h('img', { class: 'pay__qr', src: p.qr, alt: t('رمز QR لعنوان المحفظة'), width: '180', height: '180' }),
      h('div', { class: 'pay__wallet' }, h('code', { dir: 'ltr', text: p.wallet }),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(p.wallet, t('نُسخ العنوان.')) }, icon('copy'), t('نسخ'))),
      h('p', { class: 'pay__warn' }, icon('info'), t('أرسل {currency} على شبكة {network_label} فقط. الإرسال على شبكة أخرى يضيع المبلغ ولا يمكن استرجاعه.', { currency: p.currency, network_label: p.network_label })),
      p.note ? h('p', { class: 'muted', dir: 'auto', text: p.note }) : '',
      h('div', { class: 'field' }, h('label', { for: 'mem-txid', text: t('رقم العملية بعد الدفع') }), txid), err, send);
  }

  // V6 phase 13: the team asked for the missing amount — send it to the same address, then add its TXID here
  function topupBox(req) {
    const top = req.topup;
    const box = h('section', { class: 'mem-topup glass' },
      h('h3', { text: t('أكمل دفع العضوية') }),
      h('p', { text: t('دفعتك أقل من سعر العضوية. المبلغ الناقص: {amount}.', { amount: fmtMoney(top.missing) }) }),
      h('p', { class: 'pay__warn' }, icon('info'), t('أرسل الفارق على شبكة {network} فقط، إلى العنوان نفسه:', { network: top.network })),
      h('div', { class: 'pay__wallet' }, h('code', { dir: 'ltr', text: top.wallet }),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => copyText(top.wallet, t('نُسخ العنوان.')) }, icon('copy'), t('نسخ'))),
      h('ul', { class: 'mem-txids' }, ...top.txids.map((x) => h('li', { dir: 'ltr', text: x }))),
      top.checking ? h('p', { class: 'muted', text: t('نتحقق من عمليتك على البلوكشين…') }) : '');
    if (!top.can_add) {
      box.append(h('p', { class: 'muted', text: t('وصلت إلى الحد الأقصى لعدد العمليات في الطلب. تواصل مع الدعم.') }));
      return box;
    }
    const txid = h('input', { class: 'input', id: 'mem-topup-txid', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: 'TXID / Hash' });
    const err = h('div', { class: 'form-error', role: 'alert' });
    const add = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, t('إضافة رقم عملية الفارق'));
    add.addEventListener('click', async () => {
      err.textContent = '';
      if (!txid.value.trim()) { err.textContent = t('أدخل رقم العملية (TXID) بعد الدفع.'); return; }
      add.disabled = true;
      try { paint(await api.post(`/api/membership/requests/${req.id}/txids`, { txid: txid.value.trim() })); toast(t('أُضيف رقم العملية، وسنتحقق منه قريبًا.')); }
      catch (e) { err.textContent = e.message; add.disabled = false; }
    });
    box.append(h('div', { class: 'field' }, h('label', { for: 'mem-topup-txid', text: t('رقم عملية الفارق') }), txid), err, add);
    return box;
  }

  function refundBox(d) {
    const r = d.refund;
    if (!r.enabled) return '';
    const last = r.request;
    if (last && last.status === 'requested') {
      return h('section', { class: 'mem-refund glass' }, h('h3', { text: t('الاسترجاع') }),
        h('p', { text: t('طلبك قيد التنفيذ: {amount} USDT إلى عنوانك على {network}.', { amount: last.amount, network: last.network }) }));
    }
    if (!r.eligible) {
      return last ? h('section', { class: 'mem-refund glass' }, h('h3', { text: t('الاسترجاع') }),
        h('p', { text: last.status === 'done' ? t('تم إرجاع {amount} USDT.', { amount: last.amount }) : t('رُفض طلب الاسترجاع: {reason}', { reason: last.reason || '' }) })) : '';
    }
    const open = h('button', { class: 'btn btn--ghost btn--block', type: 'button' }, t('طلب استرجاع العضوية'));
    const box = h('section', { class: 'mem-refund glass' }, h('h3', { text: t('الاسترجاع') }),
      h('p', { text: t('يمكنك طلب الاسترجاع حتى {date}: يُعاد {amount} USDT (بعد رسوم {fee}) وتتوقف ميزات العضوية.', { date: formatDay(r.deadline), amount: r.amount, fee: r.fee }) }),
      open);
    open.addEventListener('click', () => {
      const network = h('select', { class: 'input', id: 'refund-network' }, ...Object.entries(r.networks).map(([k, v]) => h('option', { value: k, text: v })));
      const address = h('input', { class: 'input', id: 'refund-address', dir: 'ltr', autocomplete: 'off', spellcheck: 'false', placeholder: t('عنوان محفظتك') });
      const password = h('input', { class: 'input', id: 'refund-password', type: 'password', autocomplete: 'current-password', placeholder: t('كلمة المرور') });
      const code = h('input', { class: 'input', id: 'refund-code', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: t('الرمز من بريدك') });
      const sendCode = h('button', { class: 'btn btn--ghost btn--sm', type: 'button' }, t('أرسل الرمز إلى بريدي'));
      const err = h('div', { class: 'form-error', role: 'alert' });
      const go = h('button', { class: 'btn btn--danger btn--block', type: 'button' }, t('تأكيد طلب الاسترجاع'));
      sendCode.addEventListener('click', async () => {
        sendCode.disabled = true;
        try { const s = await api.post('/api/membership/refund/code', {}); toast(t('أُرسل الرمز إلى {sent_to}', { sent_to: s.sent_to })); }
        catch (e) { err.textContent = e.message; }
        setTimeout(() => { sendCode.disabled = false; }, 30000);
      });
      go.addEventListener('click', async () => {
        err.textContent = '';
        go.disabled = true;
        try {
          paint(await api.post('/api/membership/refund', { network: network.value, address: address.value.trim(), password: password.value, code: code.value.trim() }));
          toast(t('أُرسل طلب الاسترجاع.'));
        } catch (e) { err.textContent = e.message; go.disabled = false; }
      });
      box.replaceChildren(h('h3', { text: t('طلب الاسترجاع') }),
        h('p', { class: 'muted', text: t('يصلك {amount} USDT على الشبكة التي تختارها. تأكد من العنوان جيدًا.', { amount: r.amount }) }),
        h('div', { class: 'field' }, h('label', { for: 'refund-network', text: t('الشبكة') }), network),
        h('div', { class: 'field' }, h('label', { for: 'refund-address', text: t('عنوان المحفظة') }), address),
        h('div', { class: 'field' }, h('label', { for: 'refund-password', text: t('كلمة المرور') }), password),
        h('div', { class: 'field' }, h('label', { for: 'refund-code', text: t('رمز التأكيد') }), h('div', { class: 'mem-code' }, code, sendCode)),
        err, go);
    });
    return box;
  }

  function paint(d) {
    const req = d.request;
    let status;
    if (d.member) {
      status = h('section', { class: 'mem-status mem-status--on glass' }, starMark({ large: true }),
        h('div', {}, h('h2', { text: t('عضويتك مفعّلة') }), h('p', { text: t('منذ {date}', { date: formatDay(d.member_since) }) })));
    } else if (req && req.status === 'pending') {
      status = h('section', { class: 'mem-status glass' }, icon('clock'),
        h('div', {}, h('h2', { text: t('طلبك: قيد المراجعة') }), h('p', { dir: 'ltr', class: 'muted', text: req.txid })));
    } else {
      status = h('section', { class: 'mem-status glass' }, icon('verified'),
        h('div', {}, h('h2', { text: t('العضوية: {price} {currency} مرة واحدة', { price: d.price, currency: d.currency }) }),
          h('p', { text: t('ميزات إضافية داخل التطبيق. لا علاقة لها بالتداول ولا بأي عائد مالي.') })));
    }
    body.replaceChildren(
      status,
      req && req.status === 'rejected' && !d.member ? h('p', { class: 'mem-note', text: t('رُفض طلبك السابق: {note}', { note: req.note || '' }) }) : '',
      req && req.status === 'pending' && req.topup ? topupBox(req) : '',
      h('h3', { class: 'section-title', text: t('ميزات الأعضاء') }), features(d),
      d.member || (req && req.status === 'pending') ? '' : payBox(d),
      d.member ? refundBox(d) : '',
      h('p', { class: 'muted mem-terms' }, t('التفاصيل في '), h('a', { href: '/policies/terms', target: '_blank', rel: 'noopener' }, t('شروط الاستخدام')), '.'));
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
