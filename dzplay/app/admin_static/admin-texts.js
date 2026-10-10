// Admin panel — V6 phase 8: «النصوص» (the content system). Policies, terms, help pages, in-app texts and e-mail
// texts: edit with a live preview (same converter as the site), revision history with restore, back to the
// default. A «major change» to privacy / terms / guidelines needs a fresh 2FA code and asks every user to
// accept the new version. Every change is audited server-side.
import { h, toast } from '/js/ui.js';
import { safeHtml } from '/js/content.js';
import { attempt, call, chip, confirmDanger, detailSheet, emptyState, sectionHead, spinner, when } from './admin-common.js';

const KIND = { page: 'صفحة', text: 'نص داخل التطبيق', email: 'بريد' };
const btn = (label, cls, onclick) => h('button', { type: 'button', class: `btn btn--sm ${cls}`, onclick }, label);

export function renderTexts(main) {
  const list = h('div', { class: 'admin-list' }, spinner());
  main.replaceChildren(
    sectionHead('النصوص'),
    h('p', { class: 'admin-meta', text: 'كل النصوص غير الأساسية: السياسات والشروط وصفحات المساعدة ونصوص التطبيق ورسائل البريد. Markdown مبسط: # عنوان، - قائمة، **غامق**، *مائل*، [رابط](https://…). المتغيرات بين {{ }} تُملأ من الإعدادات الفعلية (السعر، المدد، الحدود…).' }),
    list);

  async function load() {
    const d = await attempt(() => call('GET', '/api/admin/content'));
    if (!d) return;
    const groups = ['page', 'text', 'email'].map((k) => [k, d.items.filter((i) => i.kind === k)]);
    list.replaceChildren(...groups.filter(([, items]) => items.length).map(([k, items]) => h('section', { class: 'admin-group glass' },
      h('h3', { text: KIND[k] === 'صفحة' ? 'الصفحات' : KIND[k] === 'بريد' ? 'رسائل البريد' : 'نصوص التطبيق' }),
      ...items.map((i) => h('button', { type: 'button', class: 'admin-line admin-line--btn', onclick: () => editor(i.key) },
        h('b', { text: i.name }), h('code', { dir: 'ltr', text: i.key }),
        i.edited ? chip(`عُدّل ${when(i.updated_at)}`, 'chip--team') : chip('الافتراضي'),
        i.ack ? chip('موافقة') : null)))));
    if (!d.items.length) list.replaceChildren(emptyState('لا نصوص.'));
  }

  function editor(key) {
    detailSheet('تعديل نص', async (body) => {
      const paint = (d) => {
        const title = h('input', { class: 'input', maxlength: '200', value: d.title || '', 'aria-label': d.kind === 'email' ? 'عنوان الرسالة' : 'العنوان' });
        const area = h('textarea', { class: 'input admin-editor', rows: '16', dir: 'auto', spellcheck: 'false', 'aria-label': 'النص' });
        area.value = d.body || '';
        const preview = h('div', { class: d.kind === 'email' ? 'admin-preview admin-preview--mail' : 'admin-preview policy-body' });
        const note = h('input', { class: 'input', maxlength: '200', placeholder: 'ملاحظة للسجل (اختياري)' });
        const major = h('input', { type: 'checkbox' });
        const code = h('input', { class: 'input', dir: 'ltr', inputmode: 'numeric', maxlength: '6', placeholder: 'رمز المصادقة', hidden: true });
        major.addEventListener('change', () => { code.hidden = !major.checked; });
        let timer = null;
        const refresh = async () => {
          const r = await attempt(() => call('POST', `/api/admin/content/${key}/preview`, { body: area.value }));
          if (!r) return;
          if (d.kind === 'email') preview.textContent = r.text; else preview.replaceChildren(safeHtml(r.html)); // allowlist-checked
        };
        area.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(refresh, 350); });
        const save = btn('حفظ', 'btn--primary', async () => {
          if (major.checked && !/^[0-9]{6}$/.test(code.value.trim())) { toast('التغيير الجوهري يحتاج رمز المصادقة.', 'error'); code.focus(); return; }
          if (major.checked && !(await confirmDanger('تغيير جوهري؟', 'سيُطلب من كل المستخدمين الموافقة على النسخة الجديدة عند فتح التطبيق.', 'حفظ وطلب الموافقة'))) return;
          const r = await attempt(() => call('PUT', `/api/admin/content/${key}`, {
            body: area.value, title: d.kind === 'text' ? null : title.value.trim() || null, note: note.value.trim() || null,
            major: major.checked, code: major.checked ? code.value.trim() : null,
          }));
          if (r) { toast(r.ack_version ? 'حُفظ ✅ — سيُطلب من المستخدمين الموافقة.' : 'حُفظ ✅'); paint(r); load(); }
        });
        const reset = btn('النص الافتراضي', 'btn--ghost', async () => {
          if (!(await confirmDanger('الرجوع إلى النص الافتراضي؟', 'يبقى السجل، ويمكنك استرجاع أي نسخة لاحقًا.', 'رجوع'))) return;
          const r = await attempt(() => call('POST', `/api/admin/content/${key}/reset`));
          if (r) { toast('عاد النص الافتراضي.'); paint(r); load(); }
        });
        const history = h('details', { class: 'admin-group glass' }, h('summary', { text: `السجل (${d.revisions.length})` }),
          ...(d.revisions.length ? d.revisions.map((r) => h('div', { class: 'admin-line' },
            h('span', { text: `#${r.id} · ${when(r.created_at)} · ${r.created_by || '—'}` }),
            r.note ? h('span', { class: 'admin-meta', text: r.note }) : null,
            r.length ? btn('استرجاع', 'btn--ghost', async () => {
              const x = await attempt(() => call('POST', `/api/admin/content/${key}/restore`, { revision_id: r.id }));
              if (x) { toast('استُرجعت النسخة.'); paint(x); load(); }
            }) : null)) : [h('p', { class: 'admin-meta', text: 'لا نسخ بعد.' })]));
        body.replaceChildren(...[
          h('section', { class: 'admin-group glass' },
            h('h3', { text: d.name }),
            h('p', { class: 'admin-meta', text: `${KIND[d.kind]} · ${d.key}${d.edited ? ` · آخر تعديل ${when(d.updated_at)}` : ' · النص الافتراضي'}${d.note ? ` · ${d.note}` : ''}` }),
            d.kind !== 'text' ? h('label', { class: 'field' }, h('span', { text: d.kind === 'email' ? 'عنوان الرسالة (Subject)' : 'العنوان' }), title) : null,
            area,
            h('div', { class: 'admin-actions' }, note),
            d.ack ? h('label', { class: 'admin-line' }, major, h('span', { text: 'تغيير جوهري (يطلب موافقة المستخدمين من جديد)' })) : null,
            d.ack ? code : null,
            h('div', { class: 'admin-actions' }, save, d.edited ? reset : null)),
          h('section', { class: 'admin-group glass' }, h('h3', { text: 'معاينة' }), preview),
          history,
        ].filter(Boolean));
        refresh();
      };
      const d = await attempt(() => call('GET', `/api/admin/content/${key}`));
      if (d) paint(d);
    });
  }

  load();
}
