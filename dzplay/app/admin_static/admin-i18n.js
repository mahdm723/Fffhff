// Admin panel — V6 phase 12: «اللغات والنصوص». Every interface text in Arabic, French and English side by side
// (search, «missing translation» filter, edit with plural forms, history and restore, back to the shipped text),
// the content system (policies, in-app texts, e-mails) in the three languages, and translation files (export /
// import with a preview). Every change is audited, keeps a backup revision, and reaches the open apps at once.
// The panel itself stays in Arabic.
import { h, toast } from '/js/ui.js';
import { attempt, call, chip, confirmDanger, detailSheet, emptyState, qs, sectionHead, segmented, spinner, when } from './admin-common.js';
import { renderContentTexts } from './admin-texts.js';

const LANGS = [['ar', 'العربية'], ['fr', 'الفرنسية'], ['en', 'الإنجليزية']];
const LANG_NAME = Object.fromEntries(LANGS);
const DIR = { ar: 'rtl', fr: 'ltr', en: 'ltr' };
const FORMS = { ar: ['zero', 'one', 'two', 'few', 'many', 'other'], fr: ['one', 'other'], en: ['one', 'other'] };
const FORM_NAME = { zero: 'صفر', one: 'مفرد', two: 'مثنى', few: 'قليل (3–10)', many: 'كثير (11–99)', other: 'غير ذلك' };
const PAGE = 60;
const btn = (label, cls, onclick) => h('button', { type: 'button', class: `btn btn--sm ${cls}`, onclick }, label);
const shown = (v) => (v && typeof v === 'object' ? Object.entries(v).map(([k, t]) => `${FORM_NAME[k] || k}: ${t}`).join(' · ') : v);

export function renderLanguages(main) {
  const body = h('div', { class: 'admin-list' });
  const views = { ui: () => uiTexts(body), content: () => renderContentTexts(body), files: () => files(body) };
  main.replaceChildren(
    sectionHead('اللغات والنصوص'),
    h('p', { class: 'admin-meta', text: 'كل نصوص التطبيق بالعربية والفرنسية والإنجليزية. النص العربي هو الأصل؛ الترجمة الناقصة يظهر مكانها النص العربي (لا يظهر أي مفتاح تقني). كل تعديل يُحفظ مع نسخة احتياطية ويصل إلى التطبيق فورًا دون نشر جديد. لوحة القيادة نفسها تبقى بالعربية.' }),
    segmented([['ui', 'نصوص الواجهة'], ['content', 'الصفحات والبريد'], ['files', 'الاستيراد والتصدير']], 'ui', (v) => views[v](), 'القسم'),
    body);
  views.ui();
}

// ------------------------------------------------------------------ interface texts, side by side

function uiTexts(box) {
  const q = h('input', { class: 'input', type: 'search', placeholder: 'ابحث في النصوص (بأي لغة)', 'aria-label': 'بحث' });
  const filter = h('select', { class: 'input admin-select', 'aria-label': 'تصفية' },
    h('option', { value: '' }, 'كل النصوص'), h('option', { value: 'fr' }, 'ناقصة بالفرنسية'),
    h('option', { value: 'en' }, 'ناقصة بالإنجليزية'), h('option', { value: 'edited' }, 'المعدّلة من اللوحة'));
  const counts = h('div', { class: 'admin-actions' });
  const list = h('div', { class: 'i18n-list' }, spinner());
  const more = btn('عرض المزيد', 'btn--ghost', () => load(true));
  more.hidden = true;
  let offset = 0;
  let timer = null;
  box.replaceChildren(h('div', { class: 'admin-filters glass' }, h('div', { class: 'admin-filter' }, q), filter, counts), list, more);

  async function load(append = false) {
    offset = append ? offset + PAGE : 0;
    const f = filter.value;
    const d = await attempt(() => call('GET', `/api/admin/i18n${qs({ q: q.value.trim(), missing: f === 'fr' || f === 'en' ? f : '', edited: f === 'edited' ? 'true' : '', offset, limit: PAGE })}`));
    if (!d) return;
    counts.replaceChildren(chip(`${d.total} نصًا`), chip(`ناقصة بالفرنسية: ${d.missing.fr}`, d.missing.fr ? 'chip--warn' : 'chip--live'),
      chip(`ناقصة بالإنجليزية: ${d.missing.en}`, d.missing.en ? 'chip--warn' : 'chip--live'), chip(`المعروض: ${d.shown}`));
    const rows = d.rows.map(row);
    if (append) list.append(...rows); else list.replaceChildren(...(rows.length ? rows : [emptyState('لا نصوص تطابق البحث.')]));
    more.hidden = offset + PAGE >= d.shown;
  }

  function row(r) {
    const cell = (lang) => {
      const c = r.cells[lang];
      const missing = lang !== 'ar' && c.text == null;
      const text = lang === 'ar' && c.text == null ? r.msgid : shown(c.text);
      return h('button', { type: 'button', class: `i18n-cell${missing ? ' is-missing' : ''}`, onclick: () => editor(r.key, lang, load) },
        h('span', { class: 'i18n-cell__lang' }, LANG_NAME[lang], c.edited ? chip('معدّل', 'chip--team') : null, missing ? chip('ناقص', 'chip--warn') : null),
        h('span', { class: 'i18n-cell__text', dir: DIR[lang], lang, text: missing ? `يظهر النص العربي: ${r.msgid}` : text }));
    };
    return h('div', { class: 'i18n-row glass' }, ...['ar', 'fr', 'en'].map(cell),
      h('span', { class: 'admin-meta i18n-where', dir: 'ltr', text: `${r.client ? 'app' : 'server'} · ${r.where.join(', ')}` }));
  }

  q.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => load(), 300); });
  filter.addEventListener('change', () => load());
  load();
}

function editor(key, lang, onChange) {
  detailSheet(`النص · ${LANG_NAME[lang]}`, async (body) => {
    const paint = (d) => {
      const plural = d.plural || (d.text && typeof d.text === 'object') || (d.default && typeof d.default === 'object');
      const current = d.text ?? (lang === 'ar' ? null : null);
      let read;
      let control;
      if (plural) {
        const inputs = FORMS[lang].map((form) => {
          const value = current && typeof current === 'object' ? current[form] || '' : (form === 'other' && typeof current === 'string' ? current : '');
          const input = h('input', { class: 'input', dir: DIR[lang], lang, value, placeholder: lang === 'ar' && form === 'other' ? d.msgid : '', 'aria-label': FORM_NAME[form] });
          return [form, input];
        });
        control = h('div', { class: 'admin-form' }, h('p', { class: 'admin-meta', text: 'نص يتغير حسب العدد {n}. «غير ذلك» إلزامي، والباقي اختياري.' }),
          ...inputs.map(([form, input]) => h('label', { class: 'field' }, h('span', { text: FORM_NAME[form] }), input)));
        read = () => Object.fromEntries(inputs.map(([form, input]) => [form, input.value.trim()]).filter(([, v]) => v));
      } else {
        const area = h('textarea', { class: 'input', rows: '4', dir: DIR[lang], lang, 'aria-label': 'الترجمة' });
        area.value = typeof current === 'string' ? current : (lang === 'ar' ? d.msgid : '');
        control = area;
        read = () => area.value.trim();
      }
      const note = h('input', { class: 'input', maxlength: '200', placeholder: 'ملاحظة للسجل (اختياري)' });
      const save = btn('حفظ', 'btn--primary', async () => {
        const text = read();
        if (!text || (typeof text === 'object' && !text.other)) { toast(plural ? 'اكتب صيغة «غير ذلك» على الأقل.' : 'النص فارغ.', 'error'); return; }
        const r = await attempt(() => call('PUT', `/api/admin/i18n/${lang}/${key}`, { text, note: note.value.trim() || null }));
        if (r) { toast('حُفظ ✅ — يظهر في التطبيق الآن.'); paint(r); onChange(); }
      });
      const reset = btn('الرجوع إلى الافتراضي', 'btn--ghost', async () => {
        if (!(await confirmDanger('الرجوع إلى النص الافتراضي؟', 'يبقى السجل، ويمكنك استرجاع أي نسخة لاحقًا.', 'رجوع'))) return;
        const r = await attempt(() => call('DELETE', `/api/admin/i18n/${lang}/${key}`));
        if (r) { toast('عاد النص الافتراضي.'); paint(r); onChange(); }
      });
      const history = h('details', { class: 'admin-group glass' }, h('summary', { text: `السجل والنسخ الاحتياطية (${d.revisions.length})` }),
        ...(d.revisions.length ? d.revisions.map((rev) => h('div', { class: 'admin-line admin-line--col' },
          h('span', { class: 'admin-meta', text: `#${rev.id} · ${when(rev.at)} · ${rev.by || '—'}${rev.note ? ` · ${rev.note}` : ''}` }),
          h('span', { dir: DIR[lang], lang, text: rev.text == null ? '(الرجوع إلى الافتراضي)' : shown(rev.text) }),
          btn('استرجاع هذه النسخة', 'btn--ghost', async () => {
            const x = await attempt(() => call('POST', '/api/admin/i18n/restore', { revision_id: rev.id }));
            if (x) { toast('استُرجعت النسخة.'); paint(x); onChange(); }
          }))) : [h('p', { class: 'admin-meta', text: 'لا تعديلات بعد.' })]));
      body.replaceChildren(...[
        h('section', { class: 'admin-group glass' },
          h('h3', { text: 'النص العربي (الأصل)' }),
          h('p', { dir: 'rtl', lang: 'ar', class: 'i18n-original', text: d.msgid }),
          h('p', { class: 'admin-meta', dir: 'ltr', text: d.where.join(' · ') }),
          d.default != null && lang !== 'ar' ? h('p', { class: 'admin-meta' }, 'الترجمة المرفقة بالتطبيق: ', h('span', { dir: DIR[lang], lang, text: shown(d.default) })) : null),
        h('section', { class: 'admin-group glass' },
          h('h3', {}, LANG_NAME[lang], d.edited ? chip(`معدّل`, 'chip--team') : chip(d.text == null ? 'ناقص — يظهر العربي' : 'الافتراضي')),
          control, note, h('div', { class: 'admin-actions' }, save, d.edited ? reset : null)),
        history,
      ].filter(Boolean));
    };
    const d = await attempt(() => call('GET', `/api/admin/i18n/${lang}/${key}`));
    if (d) paint(d);
  });
}

// ------------------------------------------------------------------ translation files

function files(box) {
  const exportRow = h('div', { class: 'admin-actions' }, ...LANGS.map(([code, name]) => btn(`تصدير ${name}`, 'btn--ghost', async () => {
    const d = await attempt(() => call('GET', `/api/admin/i18n/export${qs({ lang: code })}`));
    if (!d) return;
    const blob = new Blob([JSON.stringify(d, null, 1)], { type: 'application/json' });
    const a = h('a', { href: URL.createObjectURL(blob), download: `dalta-${code}.json` });
    document.body.append(a);
    a.click();
    setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 1000);
    toast(`صُدّر ملف ${name}: ${d.count} نصًا + ${Object.keys(d.content || {}).length} صفحة ونص.`);
  })));
  const lang = h('select', { class: 'input admin-select', 'aria-label': 'لغة الملف' }, ...LANGS.map(([code, name]) => h('option', { value: code }, name)));
  lang.value = 'fr';
  const file = h('input', { type: 'file', accept: 'application/json,.json', class: 'input', 'aria-label': 'ملف JSON' });
  const result = h('div', { class: 'admin-list' });
  let data = null;
  file.addEventListener('change', async () => {
    result.replaceChildren();
    data = null;
    const f = file.files && file.files[0];
    if (!f) return;
    if (f.size > 3 * 1024 * 1024) { toast('الملف أكبر من 3 MB.', 'error'); return; }
    try { data = JSON.parse(await f.text()); } catch { toast('الملف ليس JSON صالحًا.', 'error'); return; }
    if (data && data.lang && LANGS.some(([c]) => c === data.lang)) lang.value = data.lang;
    preview();
  });
  lang.addEventListener('change', () => { if (data) preview(); });

  async function preview() {
    const p = await attempt(() => call('POST', '/api/admin/i18n/import/preview', { lang: lang.value, data }));
    if (!p) return;
    const total = p.changes.length + p.content_changes.length;
    const apply = btn(`تطبيق الاستيراد (${total} تغييرًا)`, 'btn--primary', async () => {
      if (!(await confirmDanger('تطبيق الاستيراد؟', `سيُحفظ ${total} تغييرًا بالـ${LANG_NAME[lang.value]}، مع نسخة احتياطية لكل نص يمكن استرجاعها.`, 'تطبيق'))) return;
      const r = await attempt(() => call('POST', '/api/admin/i18n/import', { lang: lang.value, data }));
      if (r) { toast(`استُورد ${r.applied} نصًا و${r.content_applied} صفحة/نصًا ✅`); result.replaceChildren(); file.value = ''; data = null; }
    });
    apply.disabled = !total;
    result.replaceChildren(
      h('section', { class: 'admin-group glass' },
        h('h3', { text: 'معاينة الاستيراد' }),
        h('div', { class: 'admin-actions' }, chip(`تغييرات في الواجهة: ${p.changes.length}`, 'chip--team'), chip(`صفحات ونصوص: ${p.content_changes.length}`, 'chip--team'),
          chip(`دون تغيير: ${p.unchanged}`), p.unknown_count ? chip(`غير معروفة (تُتجاهل): ${p.unknown_count}`, 'chip--warn') : null),
        ...p.changes.slice(0, 40).map((c) => h('div', { class: 'admin-line admin-line--col' },
          h('span', { dir: 'rtl', lang: 'ar', class: 'admin-meta', text: c.msgid }),
          h('span', { dir: DIR[lang.value], lang: lang.value }, h('s', { text: c.old == null ? '—' : shown(c.old) }), ' → ', h('b', { text: shown(c.new) })))),
        p.changes.length > 40 ? h('p', { class: 'admin-meta', text: `و${p.changes.length - 40} تغييرًا آخر.` }) : null,
        ...p.content_changes.map((c) => h('div', { class: 'admin-line' }, h('b', { text: c.name }), h('code', { dir: 'ltr', text: c.key }))),
        h('div', { class: 'admin-actions' }, apply)));
  }

  box.replaceChildren(
    h('section', { class: 'admin-group glass' },
      h('h3', { text: 'تصدير ملف ترجمة (JSON)' }),
      h('p', { class: 'admin-meta', text: 'الملف يحوي كل نصوص الواجهة باللغة المختارة، مع الصفحات والنصوص ورسائل البريد. عدّله ثم استورده هنا.' }),
      exportRow),
    h('section', { class: 'admin-group glass' },
      h('h3', { text: 'استيراد ملف ترجمة' }),
      h('p', { class: 'admin-meta', text: 'تُعرض التغييرات أولًا ولا يُحفظ شيء قبل «تطبيق». النصوص غير المعروفة تُتجاهل. كل نص يتغير يُحفظ مع نسخة احتياطية.' }),
      h('label', { class: 'field' }, h('span', { text: 'لغة الملف' }), lang),
      file),
    result);
}
