// Admin panel — V6 money: «العضوية والدفع» (payment wallet, membership requests, refunds, old star requests).
// Rewards, withdrawals (5b) and the red envelope (5c) are added here too. Every action is audited server-side.
import { h, toast } from '/js/ui.js';
import {
  attempt, call, chip, confirmDanger, emptyState, qs, sectionHead, segmented, spinner, userRef, when,
} from './admin-common.js';
import { paymentSettingsBox, verifyHistory } from './admin-v5.js';

const input = (attrs) => h('input', { class: 'input', ...attrs });
const btn = (label, cls, onclick) => h('button', { type: 'button', class: `btn btn--sm ${cls}`, onclick }, label);
const memState = { status: 'pending' };

function txLine(r) {
  return h('p', { class: 'admin-meta' }, `${r.amount} ${r.currency || 'USDT'} · ${r.network} · `, h('code', { dir: 'ltr', text: r.txid }), ' ',
    r.explorer_url ? h('a', { href: r.explorer_url, target: '_blank', rel: 'noopener noreferrer', text: 'فتح في المستكشف ↗' }) : '');
}

export function renderMembership(main) {
  const summary = h('p', { class: 'admin-meta' });
  const list = h('div', { class: 'admin-list' }, spinner());
  const refunds = h('div', { class: 'admin-list' });

  function requestCard(r) {
    const note = input({ placeholder: 'سبب الرفض (يراه المستخدم)', maxlength: '500' });
    const decide = (action) => async () => {
      if (action === 'accept' && !(await confirmDanger('قبول العضوية؟', `تأكد في المستكشف أن ${r.amount} ${r.currency} وصلت إلى محفظتك على ${r.network}.`, 'قبول'))) return;
      if (await attempt(() => call('POST', `/api/admin/membership/requests/${r.id}/decide`, { action, note: note.value.trim() || null }))) { toast('تم.'); load(); }
    };
    return h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-line' }, h('b', { text: r.public_id || '—' }), r.name ? h('bdi', { text: r.name }) : '', chip(r.status_label),
        r.member ? chip('عضو', 'chip--team') : '', userRef(r.user_ref, 'الصفحة')),
      h('p', { class: 'admin-meta', text: when(r.created_at) }),
      txLine(r),
      r.note ? h('p', { class: 'admin-meta', text: `ملاحظة: ${r.note} (${r.decided_by || ''})` }) : '',
      r.status === 'pending' ? h('div', { class: 'admin-actions' }, note,
        btn('قبول', 'btn--primary', decide('accept')), btn('رفض', 'btn--danger', decide('reject'))) : '');
  }

  function refundCard(r) {
    const tx = input({ placeholder: 'TXID تحويل الإرجاع', dir: 'ltr', maxlength: '200' });
    const reason = input({ placeholder: 'سبب الرفض (يراه المستخدم)', maxlength: '500' });
    return h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-line' }, h('b', { text: r.public_id || '—' }), chip(r.status_label), userRef(r.user_ref, 'الصفحة')),
      h('p', { class: 'admin-meta', text: `طُلب ${when(r.created_at)} · ${r.network_label}` }),
      h('p', { class: 'admin-meta' }, 'أرسل ', h('b', { text: `${r.amount} USDT` }), ` (بعد رسوم ${r.fee}) إلى: `, h('code', { dir: 'ltr', text: r.address })),
      r.refund_txid ? h('p', { class: 'admin-meta' }, 'تم: ', h('code', { dir: 'ltr', text: r.refund_txid }), ' ',
        r.explorer_url ? h('a', { href: r.explorer_url, target: '_blank', rel: 'noopener noreferrer', text: '↗' }) : '') : '',
      r.reason ? h('p', { class: 'admin-meta', text: `السبب: ${r.reason}` }) : '',
      r.status === 'requested' ? h('div', { class: 'admin-actions' }, tx,
        btn('تم الإرسال', 'btn--primary', async () => {
          if (!tx.value.trim()) { toast('أدخل رقم عملية التحويل أولًا.', 'error'); return; }
          if (!(await confirmDanger('تسجيل الإرجاع؟', 'تنتهي العضوية وتُسجَّل الحركة في الدفتر. لا تراجع.', 'تسجيل'))) return;
          if (await attempt(() => call('POST', `/api/admin/membership/refunds/${r.id}/decide`, { action: 'done', txid: tx.value.trim() }))) { toast('سُجّل.'); load(); }
        }),
        reason,
        btn('رفض', 'btn--danger', async () => {
          if (await attempt(() => call('POST', `/api/admin/membership/refunds/${r.id}/decide`, { action: 'reject', note: reason.value.trim() }))) { toast('رُفض.'); load(); }
        })) : '');
  }

  async function load() {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/membership${qs({ status: memState.status })}`));
    if (!d) return;
    summary.textContent = `الأعضاء الحاليون: ${d.members} · بانتظار المراجعة: ${d.counts.pending} · استرجاع مفتوح: ${d.open_refunds}`;
    list.replaceChildren(...(d.requests.length ? d.requests.map(requestCard) : [emptyState('لا توجد طلبات.')]));
    refunds.replaceChildren(...(d.refunds.length ? d.refunds.map(refundCard) : [emptyState('لا توجد طلبات استرجاع.')]));
  }

  main.replaceChildren(
    sectionHead('العضوية والدفع'),
    h('p', { class: 'admin-meta', text: 'العضوية ميزات داخل التطبيق فقط (النجمة، الأفكار مع صورة، صور المحادثة) ولا علاقة لها بأي عائد. السعر ومدة الاسترجاع ورسومه من «الإعدادات ← العضوية».' }),
    summary,
    paymentSettingsBox(),
    h('h3', { text: 'طلبات العضوية' }),
    segmented([['pending', 'قيد المراجعة'], ['accepted', 'مقبول'], ['rejected', 'مرفوض'], ['', 'الكل (سجل الدفع)']],
      memState.status, (v) => { memState.status = v; load(); }, 'حالة الطلبات'),
    list,
    h('h3', { text: 'الاسترجاع' }),
    refunds,
    h('details', { class: 'admin-group glass' }, h('summary', { text: 'طلبات النجمة القديمة (V5، للاطلاع)' }), verifyHistory()));
  load();
}

// ------------------------------------------------------------------ V6 phase 5b: rewards + withdrawals

export function renderRewards(main) {
  const summary = h('p', { class: 'admin-meta' });
  const wList = h('div', { class: 'admin-list' }, spinner());
  const rList = h('div', { class: 'admin-list' });
  const batches = h('div', { class: 'admin-list' });

  function withdrawalCard(w) {
    const tx = input({ placeholder: 'TXID التحويل', dir: 'ltr', maxlength: '200' });
    const reason = input({ placeholder: 'سبب الرفض (يُعاد المبلغ للرصيد)', maxlength: '500' });
    return h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-line' }, h('b', { text: w.public_id || '—' }), w.name ? h('bdi', { text: w.name }) : '', chip(w.status_label), userRef(w.user_ref, 'الصفحة')),
      h('p', { class: 'admin-meta', text: `${when(w.created_at)} · ${w.network_label}` }),
      h('p', { class: 'admin-meta' }, 'أرسل ', h('b', { text: `${w.net} USDT` }), ` (المبلغ ${w.amount} − الرسوم ${w.fee}) إلى: `, h('code', { dir: 'ltr', text: w.address })),
      w.txid ? h('p', { class: 'admin-meta' }, 'تم: ', h('code', { dir: 'ltr', text: w.txid }), ' ',
        w.explorer_url ? h('a', { href: w.explorer_url, target: '_blank', rel: 'noopener noreferrer', text: '↗' }) : '') : '',
      w.reason ? h('p', { class: 'admin-meta', text: `السبب: ${w.reason}` }) : '',
      w.status === 'pending' ? h('div', { class: 'admin-actions' }, tx,
        btn('تم الإرسال', 'btn--primary', async () => {
          if (!tx.value.trim()) { toast('أدخل رقم عملية التحويل أولًا.', 'error'); return; }
          if (await attempt(() => call('POST', `/api/admin/withdrawals/${w.id}/decide`, { action: 'done', txid: tx.value.trim() }))) { toast('سُجّل.'); load(); }
        }),
        reason,
        btn('رفض', 'btn--danger', async () => {
          if (await attempt(() => call('POST', `/api/admin/withdrawals/${w.id}/decide`, { action: 'reject', note: reason.value.trim() }))) { toast('رُفض وأُعيد المبلغ.'); load(); }
        })) : '');
  }

  function referralCard(r) {
    return h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-line' }, h('span', { text: 'الداعي:' }), h('b', { text: r.referrer.public_id || '—' }), userRef(r.referrer.ref, 'الصفحة'),
        h('span', { text: '← المدعو:' }), h('b', { text: r.referee.public_id || '—' }), r.referee.member ? chip('عضو', 'chip--team') : '', chip(r.status_label)),
      r.flags ? h('p', { class: 'admin-meta', text: `علامات: ${r.flags.replace('same_network', 'نفس الشبكة').replace('same_device', 'نفس الجهاز').replace('burst', 'دعوات كثيرة من شبكة واحدة')}` }) : '',
      h('p', { class: 'admin-meta', text: when(r.created_at) }),
      r.status === 'review' ? h('div', { class: 'admin-actions' },
        btn('اعتماد المكافأة', 'btn--primary', async () => {
          if (await attempt(() => call('POST', `/api/admin/referrals/${r.id}/review`, { action: 'approve' }))) { toast('اعتُمدت.'); load(); }
        }),
        btn('رفض', 'btn--danger', async () => {
          if (await attempt(() => call('POST', `/api/admin/referrals/${r.id}/review`, { action: 'reject' }))) { toast('رُفضت.'); load(); }
        })) : '');
  }

  const amount = input({ dir: 'ltr', inputmode: 'decimal', placeholder: '2' });
  const activeDays = input({ dir: 'ltr', inputmode: 'numeric', value: '30' });
  const minPosts = input({ dir: 'ltr', inputmode: 'numeric', value: '1' });
  const minAge = input({ dir: 'ltr', inputmode: 'numeric', value: '7' });
  const note = input({ placeholder: 'السبب (يظهر للمستفيدين)', maxlength: '200' });
  const code = input({ dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: 'رمز المصادقة' });
  const preview = h('p', { class: 'admin-meta' });
  const groupBody = () => ({ amount: Number(amount.value), active_days: Number(activeDays.value || 0), min_posts: Number(minPosts.value || 0),
    min_age_days: Number(minAge.value || 0), note: note.value.trim() });
  const groupBox = h('section', { class: 'admin-group glass' },
    h('h3', { text: 'مكافأة جماعية' }),
    h('p', { class: 'admin-meta', text: 'معايير النشاط فقط — لا تدخل العضوية ولا الدفع في أي معيار. تُستبعد الحسابات الموقوفة والمحظورة وحسابات الفريق.' }),
    h('div', { class: 'admin-actions' }, h('label', { text: 'المبلغ لكل شخص (USDT)' }), amount),
    h('div', { class: 'admin-actions' }, h('label', { text: 'نشط خلال (يوم)' }), activeDays, h('label', { text: 'أفكار ≥' }), minPosts, h('label', { text: 'عمر الحساب ≥ (يوم)' }), minAge),
    h('div', { class: 'admin-actions' }, note),
    h('div', { class: 'admin-actions' }, btn('معاينة', 'btn--ghost', async () => {
      const r = await attempt(() => call('POST', '/api/admin/rewards/group/preview', groupBody()));
      if (r) preview.textContent = `سيستفيد ${r.count} شخصًا × ${r.amount} = ${r.total} USDT`;
    }), code, btn('تنفيذ', 'btn--primary', async () => {
      if (!(await confirmDanger('تنفيذ المكافأة الجماعية؟', preview.textContent || 'اعرض المعاينة أولًا.', 'تنفيذ'))) return;
      const r = await attempt(() => call('POST', '/api/admin/rewards/group', { ...groupBody(), code: code.value.trim() }));
      if (r) { toast(`نُفّذت: ${r.count} شخصًا، ${r.total} USDT.`); code.value = ''; load(); }
    })),
    preview);

  async function load() {
    const d = await attempt(() => call('GET', '/api/admin/rewards'));
    if (!d) return;
    summary.textContent = `سحب بانتظار التنفيذ: ${d.pending} · دعوات للمراجعة: ${d.review}`;
    wList.replaceChildren(...(d.withdrawals.length ? d.withdrawals.map(withdrawalCard) : [emptyState('لا طلبات سحب.')]));
    rList.replaceChildren(...(d.referrals.length ? d.referrals.map(referralCard) : [emptyState('لا دعوات بعد.')]));
    batches.replaceChildren(...d.batches.map((b) => h('div', { class: 'admin-line' }, chip(`${b.count} × ${b.amount}`), h('b', { text: `${b.total} USDT` }),
      h('span', { dir: 'auto', text: b.note }), h('span', { class: 'admin-meta', text: `${b.by} · ${when(b.at)}` }))));
  }
  main.replaceChildren(sectionHead('المكافآت والسحب'),
    h('p', { class: 'admin-meta', text: 'مكافآت ترويجية مستقلة عن العضوية. القيم (مكافأة الدعوة، مدة التعليق، حد السحب ورسومه) من «الإعدادات ← المكافآت والسحب».' }),
    summary, h('h3', { text: 'طلبات السحب' }), wList, h('h3', { text: 'الدعوات' }), rList, groupBox,
    h('details', { class: 'admin-group glass' }, h('summary', { text: 'سجل المكافآت الجماعية' }), batches));
  load();
}

/** A member's ledger on the user page (membership + rewards), and «إيقاف العضوية». */
function rewardForm(userId, paintAgain) {
  const kind = h('select', { class: 'input admin-select' }, h('option', { value: 'contest', text: 'مسابقة' }),
    h('option', { value: 'activity', text: 'نشاط' }), h('option', { value: 'correction', text: 'تصحيح (+/−)' }));
  const amount = input({ dir: 'ltr', inputmode: 'decimal', placeholder: 'المبلغ USDT' });
  const reason = input({ placeholder: 'السبب (إلزامي، يراه المستخدم)', maxlength: '300' });
  return h('details', {}, h('summary', { text: 'مكافأة فردية / تصحيح' }),
    h('div', { class: 'admin-actions' }, kind, amount, reason, btn('تنفيذ', 'btn--primary', async () => {
      if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(userId)}/rewards`, { kind: kind.value, amount: Number(amount.value), reason: reason.value.trim() }))) { toast('سُجّلت.'); paintAgain(); }
    })));
}

export async function userMoneySection(userId, paintAgain) {
  const box = h('section', { class: 'admin-group glass' }, spinner());
  const d = await attempt(() => call('GET', `/api/admin/users/${encodeURIComponent(userId)}/balances`));
  if (!d) return box;
  const rows = (acc) => d[acc].entries.map((e) => h('div', { class: 'admin-line' },
    chip(e.kind), h('b', { dir: 'ltr', text: e.amount }), e.pending ? chip('معلّق') : '', h('span', { class: 'admin-meta', text: `${e.note || ''} · ${when(e.created_at)}` })));
  const reason = input({ placeholder: 'السبب (إلزامي)', maxlength: '500' });
  box.replaceChildren(
    h('h3', { text: 'العضوية والأرصدة' }),
    h('div', { class: 'admin-line' }, chip(d.member ? `عضو منذ ${when(d.member_since)}` : 'ليس عضوًا', d.member ? 'chip--team' : '')),
    h('p', { class: 'admin-meta', text: `العضوية: ${d.membership.balances.total} · المكافآت: متاح ${d.rewards.balances.available} / معلّق ${d.rewards.balances.pending}` }),
    h('details', {}, h('summary', { text: `حركات العضوية (${d.membership.entries.length})` }), ...rows('membership')),
    h('details', {}, h('summary', { text: `حركات المكافآت (${d.rewards.entries.length})` }), ...rows('rewards')),
    rewardForm(userId, paintAgain),
    d.member ? h('div', { class: 'admin-actions' }, reason, btn('إيقاف العضوية', 'btn--danger', async () => {
      if (!reason.value.trim()) { toast('اكتب السبب.', 'error'); return; }
      if (!(await confirmDanger('إيقاف العضوية؟', 'تتوقف ميزات الأعضاء فورًا، وتُزال النجمة إن كانت من العضوية. لا يُرجَع أي مبلغ تلقائيًا.'))) return;
      if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(userId)}/membership/end`, { reason: reason.value.trim() }))) { toast('أُوقفت.'); paintAgain(); }
    })) : '');
  return box;
}

