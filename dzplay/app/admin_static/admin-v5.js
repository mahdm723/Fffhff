// Admin panel — V5 sections: media moderation (idea pictures, reported chat pictures),
// blue-star requests + payment settings, support tickets, live settings.
// Telegram buttons and these screens call the same server actions; everything is in the audit log.
import { h, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  BASE, attempt, call, chip, confirmDanger, detailSheet, emptyState, field, fmt, qs, sectionHead, segmented, spinner, userRef, when,
} from './admin-common.js';

const PURPOSE = { idea: 'صورة فكرة', chat: 'صورة محادثة', avatar: 'صورة شخصية' };
const MEDIA_ACTIONS = [['ok', 'قبول', 'btn--ghost'], ['keep', 'إبقاء', 'btn--ghost'], ['del', 'حذف', 'btn--danger'],
  ['ban', 'حذف + حظر', 'btn--danger'], ['minor', 'قاصر: حذف + حظر + دليل', 'btn--danger']];
const btn = (label, cls, onclick) => h('button', { type: 'button', class: `btn btn--sm ${cls}`, onclick }, label);
const input = (attrs = {}) => h('input', { class: 'input', ...attrs });

function preview(item) {
  if (!item.previewable) return h('div', { class: 'admin-thumb admin-thumb--none' }, icon('eyeOff'));
  const src = `${BASE}/api/admin/media/${item.id}/img`;
  return h('a', { class: 'admin-thumb', href: src, target: '_blank', rel: 'noopener' }, h('img', { src, alt: '', loading: 'lazy' }));
}

// ------------------------------------------------------------------ media moderation

const mediaState = { filter: 'pending' };

export function renderMedia(main) {
  const list = h('div', { class: 'admin-list' }, spinner());
  async function load() {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/media${qs({ filter: mediaState.filter })}`));
    if (!d) return;
    if (!d.items.length) { list.replaceChildren(emptyState('لا يوجد شيء هنا.')); return; }
    list.replaceChildren(...d.items.map((it) => h('article', { class: 'admin-card glass admin-media' },
      preview(it),
      h('div', { class: 'admin-media__body' },
        h('div', { class: 'admin-line' }, chip(PURPOSE[it.purpose] || it.purpose), chip(it.state),
          it.review ? chip(it.review === 'pending' ? 'بانتظار الموافقة' : it.review) : null,
          it.hidden ? chip('مخفي', 'chip--hot') : null, it.legal_hold ? chip('محفوظ كدليل', 'chip--hot') : null,
          it.reports ? chip(`${fmt(it.reports)} بلاغ`, 'chip--hot') : null),
        it.caption ? h('p', { class: 'admin-text', dir: 'auto', text: it.caption }) : null,
        h('p', { class: 'admin-meta' }, `${it.owner_public_id || '—'} · ${when(it.created_at)}`,
          it.nsfw != null ? ` · فحص: ${it.nsfw.toFixed(2)}` : '', it.removed_by ? ` · أزاله: ${it.removed_by}` : '', ' ',
          userRef(it.owner_ref, 'صفحة الناشر')),
        it.error ? h('p', { class: 'admin-meta', text: it.error }) : null,
        h('div', { class: 'admin-actions' }, ...MEDIA_ACTIONS
          .filter(([a]) => (a === 'ok' ? it.review === 'pending' : a === 'keep' ? (it.hidden || it.legal_hold) : it.state !== 'removed'))
          .map(([a, label, cls]) => btn(label, cls, async () => {
            if (a !== 'ok' && a !== 'keep' && !(await confirmDanger(`${label}؟`, 'يُطبّق فورًا ويُسجّل في سجل التدقيق، ويتحدّث زر Telegram أيضًا.'))) return;
            const r = await attempt(() => call('POST', `/api/admin/media/${it.id}/action`, { action: a }));
            if (r) { toast(r.result); load(); }
          })))))));
  }
  main.replaceChildren(
    sectionHead('الإشراف على الوسائط'),
    h('p', { class: 'admin-meta', text: 'صور الأفكار، وصور المحادثات المبلّغ عنها فقط. تصل نسخة من كل منشور إلى مجموعة الإشراف في Telegram.' }),
    segmented([['pending', 'بانتظار الموافقة'], ['published', 'منشور'], ['reported', 'مبلّغ عنه'], ['rejected', 'مرفوض بالفحص'], ['removed', 'محذوف']],
      mediaState.filter, (v) => { mediaState.filter = v; load(); }, 'تصفية الوسائط'),
    list);
  load();
}

// ------------------------------------------------------------------ verification + payment settings

const verifyState = { status: 'pending' };

/** Payment settings (wallet for memberships): one box, reused by the «العضوية والدفع» tab (V6). */
export function paymentSettingsBox() {
  const payBox = h('section', { class: 'admin-group glass' }, spinner());

  async function loadPay() {
    const d = await attempt(() => call('GET', '/api/admin/payment-settings'));
    if (!d) return;
    const p = d.payment;
    const currency = input({ value: p.currency, dir: 'ltr', placeholder: 'USDT', maxlength: '12' });
    const network = h('select', { class: 'input admin-select' }, h('option', { value: '', text: '—' }),
      ...Object.entries(d.networks).map(([k, v]) => h('option', { value: k, text: v.label })));
    network.value = p.network;
    const wallet = input({ value: p.wallet, dir: 'ltr', placeholder: 'عنوان المحفظة' });
    const explorer = input({ value: p.explorer, dir: 'ltr', placeholder: 'https://…/{txid}' });
    const note = input({ value: p.note, placeholder: 'ملاحظة تظهر للمستخدم (اختياري)' });
    network.addEventListener('change', () => { if (!explorer.value || explorer.dataset.auto) { explorer.value = d.networks[network.value]?.explorer || ''; explorer.dataset.auto = '1'; } });
    payBox.replaceChildren(
      h('h3', { text: 'إعدادات الدفع' }),
      h('p', { class: 'admin-meta', text: p.available ? 'الدفع متاح للمستخدمين.' : 'الدفع غير متاح حاليًا (يرى المستخدمون ذلك ولا يمكنهم الطلب).' }),
      field('العملة', currency), field('الشبكة', network), field('عنوان المحفظة', wallet),
      field('رابط المستكشف (يحتوي {txid})', explorer), field('ملاحظة', note),
      h('div', { class: 'admin-actions' },
        btn('حفظ', 'btn--primary', async () => {
          const r = await attempt(() => call('PUT', '/api/admin/payment-settings', {
            currency: currency.value.trim(), network: network.value, wallet: wallet.value.trim(), explorer: explorer.value.trim(), note: note.value.trim(),
          }));
          if (r) { toast('حُفظت إعدادات الدفع.'); loadPay(); }
        }),
        btn('إيقاف الدفع', 'btn--ghost', async () => {
          if (!(await confirmDanger('إيقاف الدفع؟', 'لن يستطيع أحد طلب العضوية حتى تضبط المحفظة من جديد.'))) return;
          if (await attempt(() => call('PUT', '/api/admin/payment-settings', {}))) { toast('أُوقف الدفع.'); loadPay(); }
        })));
  }
  loadPay();
  return payBox;
}

/** V5 blue-star requests (closed since V6: the star comes with the membership) — read-only history. */
export function verifyHistory() {
  const list = h('div', { class: 'admin-list' }, spinner());

  async function load() {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/verification${qs({ status: verifyState.status })}`));
    if (!d) return;
    if (!d.requests.length) { list.replaceChildren(emptyState('لا توجد طلبات.')); return; }
    list.replaceChildren(...d.requests.map((r) => {
      const note = input({ placeholder: 'السبب / المطلوب تصحيحه (يراه المستخدم)', maxlength: '500' });
      const decide = (action) => async () => {
        if (action === 'accept' && !(await confirmDanger('قبول ومنح النجمة؟', 'تأكد من الدفع في المستكشف أولًا.', 'قبول'))) return;
        const res = await attempt(() => call('POST', `/api/admin/verification/${r.id}/decide`, { action, note: note.value.trim() || null }));
        if (res) { toast('تم.'); load(); }
      };
      return h('article', { class: 'admin-card glass' },
        h('div', { class: 'admin-line' }, h('b', { text: r.public_id || '—' }), r.name ? h('bdi', { text: r.name }) : null, chip(r.status_label),
          r.verified ? chip('موثّق', 'chip--team') : null, userRef(r.user_ref, 'الصفحة')),
        h('p', { class: 'admin-meta', text: `${{ writer: 'كاتب', creator: 'صانع محتوى', page: 'صفحة', other: 'أخرى' }[r.account_type] || r.account_type} · ${when(r.created_at)}` }),
        h('p', { class: 'admin-text', dir: 'auto', text: r.description }),
        h('p', { class: 'admin-text', dir: 'auto', text: `السبب: ${r.reason}` }),
        h('p', { class: 'admin-meta' }, `${r.amount} ${r.currency} · ${r.network} · `, h('code', { dir: 'ltr', text: r.txid }), ' ',
          r.explorer_url ? h('a', { href: r.explorer_url, target: '_blank', rel: 'noopener noreferrer', text: 'فتح في المستكشف ↗' }) : null),
        h('p', { class: 'admin-meta', text: r.stats.map((s) => `${s.label}: ${s.value}`).join(' · ') }),
        r.admin_note ? h('p', { class: 'admin-meta', text: `ملاحظة: ${r.admin_note} (${r.decided_by || ''})` }) : null,
        ['pending', 'needs_fix'].includes(r.status) ? h('div', { class: 'admin-actions' }, note,
          btn('قبول', 'btn--primary', decide('accept')), btn('يحتاج تصحيحًا', 'btn--ghost', decide('fix')), btn('رفض', 'btn--danger', decide('reject'))) : null);
    }));
  }
  verifyState.status = '';
  load();
  return list;
}

// ------------------------------------------------------------------ support

export function renderSupport(main) {
  const list = h('div', { class: 'admin-list' }, spinner());
  let status = 'open';
  const search = input({ placeholder: '#1001 أو DZ-XXXXXX', dir: 'ltr' });
  async function load() {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/support${qs({ status, q: search.value.trim() })}`));
    if (!d) return;
    if (!d.tickets.length) { list.replaceChildren(emptyState('لا توجد تذاكر.')); return; }
    list.replaceChildren(...d.tickets.map((t) => h('button', { type: 'button', class: 'admin-card glass admin-line admin-line--btn', onclick: () => openTicket(t.id, load) },
      h('b', { dir: 'ltr', text: `#${t.number}` }), chip(t.category_label), chip(t.status_label), h('span', { dir: 'auto', text: t.subject }),
      h('span', { class: 'admin-meta', text: `${t.public_id} · ${when(t.updated_at)}` }))));
  }
  search.addEventListener('keydown', (e) => { if (e.key === 'Enter') load(); });
  main.replaceChildren(sectionHead('الدعم'),
    h('div', { class: 'admin-actions' }, search, btn('بحث', 'btn--ghost', load)),
    segmented([['open', 'مفتوحة'], ['answered', 'تم الرد'], ['closed', 'مغلقة'], ['', 'الكل']], status, (v) => { status = v; load(); }, 'حالة التذاكر'),
    list);
  load();
}

function openTicket(id, onChange) {
  detailSheet('تذكرة دعم', async (body) => {
    const paint = async () => {
      const d = await attempt(() => call('GET', `/api/admin/support/${id}`));
      if (!d) return;
      const t = d.ticket;
      const reply = h('textarea', { class: 'input', rows: '4', placeholder: 'ردّك (يظهر للمستخدم في «تذاكري» ويصله بريد تنبيه)' });
      body.replaceChildren(
        h('section', { class: 'admin-group glass' },
          h('h3', { dir: 'auto', text: `#${t.number} — ${t.subject}` }),
          h('p', { class: 'admin-meta' }, `${t.category_label} · ${t.status_label} · ${t.public_id} · `, h('code', { dir: 'ltr', text: t.email || '' }), ' ', userRef(t.user_ref, 'الصفحة'))),
        ...d.messages.map((m) => h('div', { class: `admin-card glass ${m.from === 'support' ? 'admin-card--team' : ''}` },
          h('p', { class: 'admin-meta', text: `${m.from === 'support' ? `الدعم (${m.admin || ''})` : 'المستخدم'} · ${when(m.created_at)}` }),
          h('p', { class: 'admin-text', dir: 'auto', text: m.body }))),
        h('section', { class: 'admin-group glass' }, reply,
          h('div', { class: 'admin-actions' },
            btn('إرسال الرد', 'btn--primary', async () => {
              if (!reply.value.trim()) return;
              if (await attempt(() => call('POST', `/api/admin/support/${id}/reply`, { body: reply.value.trim() }))) { toast('أُرسل الرد.'); paint(); onChange(); }
            }),
            t.status !== 'closed' ? btn('إغلاق', 'btn--ghost', async () => {
              if (await attempt(() => call('POST', `/api/admin/support/${id}/status`, { status: 'closed' }))) { paint(); onChange(); }
            }) : btn('إعادة فتح', 'btn--ghost', async () => {
              if (await attempt(() => call('POST', `/api/admin/support/${id}/status`, { status: 'open' }))) { paint(); onChange(); }
            }))));
    };
    paint();
  });
}

// ------------------------------------------------------------------ live settings (tunables)

export function renderSettings(main) {
  const box = h('div', {}, spinner());
  async function load() {
    const d = await attempt(() => call('GET', '/api/admin/settings'));
    if (!d) return;
    const groups = {};
    for (const s of d.settings) (groups[s.group] ||= []).push(s);
    box.replaceChildren(...Object.entries(groups).map(([g, items]) => h('section', { class: 'admin-group glass' },
      h('h3', { text: d.groups[g] || g }),
      ...items.map((s) => {
        let ctl;
        if (s.type === 'bool') { ctl = h('input', { type: 'checkbox', checked: s.value || null }); }
        else if (s.type === 'choice') ctl = h('select', { class: 'input', dir: 'ltr' }, ...s.choices.map((c) => h('option', { value: c, selected: c === s.value || null, text: c })));
        else ctl = input({ value: String(s.value ?? ''), dir: 'ltr', inputmode: s.type === 'int' || s.type === 'float' ? 'decimal' : null });
        const save = btn('حفظ', 'btn--ghost', async () => {
          let v;
          if (s.type === 'bool') v = ctl.checked;
          else if (s.type === 'int') v = parseInt(ctl.value, 10);
          else if (s.type === 'float') v = parseFloat(ctl.value);
          else v = ctl.value;
          if (await attempt(() => call('PUT', '/api/admin/settings', { changes: { [s.key]: v } }))) { toast('حُفظ وطُبّق فورًا.'); load(); }
        });
        const reset = s.overridden ? btn('افتراضي', 'btn--ghost', async () => {
          if (await attempt(() => call('PUT', '/api/admin/settings', { changes: { [s.key]: null } }))) { toast('عاد إلى قيمة .env.'); load(); }
        }) : null;
        const range = s.min != null ? ` (${s.min}–${s.max})` : '';
        return h('div', { class: 'admin-setting' },
          h('label', { text: `${s.label}${range}` }),
          h('div', { class: 'admin-actions' }, ctl, save, reset),
          h('small', { class: 'admin-meta', dir: 'ltr', text: `${s.key} · .env: ${JSON.stringify(s.default)}${s.overridden ? ` · من اللوحة (${s.updated_by || ''})` : ''}` }));
      }))));
  }
  main.replaceChildren(sectionHead('الإعدادات'),
    h('p', { class: 'admin-meta', text: 'الحدود والشروط تُطبّق فورًا دون إعادة تشغيل. الأسرار (رموز ومفاتيح) تبقى في .env ولا تظهر هنا.' }),
    box);
  load();
}

// ------------------------------------------------------------------ the V5 part of a user's page

export async function userV5Section(userId, paintAgain) {
  const box = h('div', {}, spinner());
  const d = await attempt(() => call('GET', `/api/admin/users/${encodeURIComponent(userId)}/v5`));
  if (!d) return box;
  const sec = (title, count, ...children) => h('details', { class: 'admin-group glass', open: count ? null : null },
    h('summary', { text: `${title} (${fmt(count)})` }), ...children);
  box.replaceChildren(
    h('section', { class: 'admin-group glass' },
      h('div', { class: 'admin-line' }, h('b', { text: 'النجمة الزرقاء:' }), chip(d.verified ? `موثّق منذ ${when(d.verified_at)}` : 'غير موثّق', d.verified ? 'chip--team' : ''),
        d.verified
          ? btn('سحب النجمة', 'btn--danger', async () => {
            if (!(await confirmDanger('سحب النجمة الزرقاء؟', 'تختفي النجمة فورًا من كل مكان.'))) return;
            if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(userId)}/verified`, { verified: false }))) { toast('سُحبت.'); paintAgain(); }
          })
          : btn('منح النجمة يدويًا', 'btn--ghost', async () => {
            if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(userId)}/verified`, { verified: true }))) { toast('مُنحت.'); paintAgain(); }
          }))),
    d.avatar_id ? h('section', { class: 'admin-group glass' }, h('div', { class: 'admin-line' }, h('b', { text: 'الصورة الشخصية:' }),
      btn('حذف الصورة', 'btn--danger', async () => {
        if (!(await confirmDanger('حذف الصورة الشخصية؟', 'تختفي فورًا من كل مكان، وتُحذف من التخزين.'))) return;
        if (await attempt(() => call('POST', `/api/admin/users/${encodeURIComponent(userId)}/avatar/remove`))) { toast('حُذفت.'); paintAgain(); }
      }))) : '',
    sec('طلبات التوثيق والدفع', d.verification.length, ...d.verification.map((r) => h('div', { class: 'admin-line' },
      chip(r.status_label), h('span', { dir: 'ltr', text: `${r.amount} ${r.currency} ${r.network}` }), h('code', { dir: 'ltr', text: r.txid.slice(0, 18) + '…' }),
      h('span', { class: 'admin-meta', text: when(r.created_at) })))),
    sec('الوسائط المرفوعة', d.media.length, h('div', { class: 'admin-grid' }, ...d.media.map((m) => h('div', { class: 'admin-media-mini' },
      preview(m), h('small', { class: 'admin-meta', text: `${PURPOSE[m.purpose] || m.purpose} · ${m.state}` }))))),
    sec('تذاكر الدعم', d.tickets.length, ...d.tickets.map((t) => h('div', { class: 'admin-line' },
      h('b', { dir: 'ltr', text: `#${t.number}` }), chip(t.status), h('span', { dir: 'auto', text: t.subject })))),
  );
  return box;
}
