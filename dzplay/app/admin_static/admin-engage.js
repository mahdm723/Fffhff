// Admin panel — engagement control: boosts (add / set displayed value, immediate or gradual),
// team comments from the library (or a new text), running jobs, and the comment library itself.
// Real counters are never touched: boosts live in separate fields and displayed = max(0, real + boost).
import { h, sheet, toast } from '/js/ui.js';
import { icon } from '/js/icons.js';
import {
  attempt, call, chip, confirmDanger, emptyState, field, fmt, hooks, qs, sectionHead, segmented, shortRef, spinner, when,
} from './admin-common.js';

const view = { tab: 'boost', jobStatus: 'running', libCategory: '', libQ: '' };
// Targets carried over from the content tab's multi-selection (or typed by hand).
const targets = { type: 'idea', text: '' };
const UNITS = [['m', 'دقيقة', 1], ['h', 'ساعة', 60], ['d', 'يوم', 1440]];
const JOB_STATUS = { running: 'قيد التنفيذ', done: 'اكتمل', cancelled: 'أُلغي', failed: 'فشل' };
const TYPE_LABEL = { idea: 'فكرة', reel: 'Reel' };

/** Called from the content tab's selection bar. */
export function openEngage(kind, selection) {
  targets.type = selection.type;
  targets.text = [...selection.ids].join('\n');
  view.tab = kind;
  hooks.showTab('engage');
}

const parseIds = (text) => [...new Set(text.split(/[\s,،]+/).map((s) => s.trim()).filter(Boolean))];

export function renderEngage(main) {
  const body = h('div', { class: 'admin-section' });
  const show = { boost: showBoost, comments: showComments, jobs: showJobs, library: showLibrary };
  main.replaceChildren(
    sectionHead('التفاعل'),
    segmented([['boost', 'تعزيز'], ['comments', 'تعليقات'], ['jobs', 'العمليات'], ['library', 'المكتبة']], view.tab,
      (v) => { view.tab = v; show[v](body); }, 'أدوات التفاعل'),
    body);
  show[view.tab](body);
}

// ------------------------------------------------------------------ shared form pieces

function targetPicker() {
  const ta = h('textarea', {
    class: 'input admin-ids', rows: '3', dir: 'ltr', spellcheck: 'false',
    placeholder: 'المعرّفات، واحد في كل سطر (أو حدّدها من تبويب المحتوى)',
  });
  ta.value = targets.text;
  const count = h('span', { class: 'admin-meta' });
  const paint = () => { targets.text = ta.value; count.textContent = `${fmt(parseIds(ta.value).length)} محدد`; };
  ta.addEventListener('input', paint);
  paint();
  const box = h('fieldset', { class: 'admin-fieldset' },
    h('legend', { text: 'المنشورات المستهدفة' }),
    segmented([['idea', 'أفكار'], ['reel', 'Reels']], targets.type, (v) => {
      if (v !== targets.type && ta.value) { ta.value = ''; paint(); }
      targets.type = v;
    }, 'نوع المنشور'),
    ta,
    h('div', { class: 'admin-actions' }, count,
      h('button', { type: 'button', class: 'admin-link', onclick: () => hooks.showTab('content') }, icon('bulb'), 'اختيار من المحتوى')));
  return { el: box, ids: () => parseIds(ta.value), type: () => targets.type };
}

function timingPicker() {
  let gradual = false;
  const amount = h('input', { class: 'input', type: 'number', min: '1', value: '6', inputmode: 'numeric' });
  const unit = h('select', { class: 'input admin-select' }, ...UNITS.map(([v, t]) => h('option', { value: v, text: t })));
  unit.value = 'h';
  const row = h('div', { class: 'admin-filters__row', hidden: true }, amount, unit);
  const hint = h('p', { class: 'admin-meta', text: 'يُطبَّق كل شيء فورًا.' });
  const box = h('fieldset', { class: 'admin-fieldset' },
    h('legend', { text: 'التوقيت' }),
    segmented([[false, 'فوري'], [true, 'تدريجي']], false, (v) => {
      gradual = v === true || v === 'true';
      row.hidden = !gradual;
      hint.textContent = gradual ? 'يتوزع بالتساوي على المدة، ويمكنك إلغاؤه من «العمليات».' : 'يُطبَّق كل شيء فورًا.';
    }, 'التوقيت'),
    row, hint);
  return {
    el: box,
    minutes: () => {
      if (!gradual) return null;
      const n = Math.max(1, Math.round(Number(amount.value) || 0));
      return n * UNITS.find(([v]) => v === unit.value)[2];
    },
  };
}

function numberInput(placeholder) {
  return h('input', { class: 'input', type: 'number', inputmode: 'numeric', step: '1', placeholder });
}
const numOrNull = (el) => (el.value.trim() === '' ? null : Math.trunc(Number(el.value)));

function resultTable(rows, cols) {
  return h('div', { class: 'viz-table glass' }, h('table', {},
    h('thead', {}, h('tr', {}, ...cols.map(([label]) => h('th', { scope: 'col', text: label })))),
    h('tbody', {}, ...rows.map((r) => h('tr', {}, ...cols.map(([, get], i) => h(i ? 'td' : 'th', { scope: i ? null : 'row', dir: 'ltr', text: get(r) })))))));
}

// ------------------------------------------------------------------ boost

function showBoost(body) {
  const picker = targetPicker();
  const timing = timingPicker();
  let mode = 'add';
  const likes = numberInput('0');
  const dislikes = numberInput('0');
  const modeHint = h('p', { class: 'admin-meta' });
  const paintHint = () => {
    modeHint.textContent = mode === 'add'
      ? 'يُضاف العدد إلى الرقم الظاهر (يمكن أن يكون سالبًا لإنقاصه). الرقم الظاهر لا يقل عن صفر أبدًا.'
      : 'يصبح الرقم الظاهر هو العدد المكتوب بالضبط. التفاعلات الحقيقية لا تتغير، والفرق يُحفظ كتعزيز منفصل.';
  };
  paintHint();
  const out = h('div', {});
  const submit = h('button', { type: 'submit', class: 'btn btn--primary btn--block' }, icon('spark'), 'تطبيق');
  const form = h('form', {
    class: 'admin-form glass',
    onsubmit: async (e) => {
      e.preventDefault();
      const ids = picker.ids();
      const payload = { target_type: picker.type(), ids, mode, likes: numOrNull(likes), dislikes: numOrNull(dislikes), duration_minutes: timing.minutes() };
      if (!ids.length) { toast('حدد منشورًا واحدًا على الأقل.', 'error'); return; }
      if (payload.likes === null && payload.dislikes === null) { toast('اكتب عدد الإعجابات أو عدم الإعجاب.', 'error'); return; }
      submit.disabled = true;
      const r = await attempt(() => call('POST', '/api/admin/engagement/boost', payload));
      submit.disabled = false;
      if (!r) return;
      toast(r.gradual ? 'بدأت العملية التدريجية.' : 'تم التطبيق.');
      out.replaceChildren(
        h('p', { class: 'admin-meta', text: r.gradual ? 'الأرقام الحالية (ستتغير تدريجيًا):' : 'الأرقام بعد التطبيق:' }),
        resultTable(r.targets, [
          ['المنشور', (t) => shortRef(t.id)],
          ['👍 ظاهر = حقيقي + مضاف', (t) => `${fmt(t.shown.likes)} = ${fmt(t.real.likes)} + ${fmt(t.boost.likes)}`],
          ['👎 ظاهر = حقيقي + مضاف', (t) => `${fmt(t.shown.dislikes)} = ${fmt(t.real.dislikes)} + ${fmt(t.boost.dislikes)}`],
        ]));
    },
  },
  picker.el,
  h('fieldset', { class: 'admin-fieldset' },
    h('legend', { text: 'العملية' }),
    segmented([['add', 'إضافة عدد'], ['set', 'تحديد الرقم الظاهر']], mode, (v) => { mode = v; paintHint(); }, 'نوع العملية'),
    h('div', { class: 'admin-filters__row' }, field('👍 إعجاب', likes), field('👎 عدم إعجاب', dislikes)),
    modeHint),
  timing.el,
  submit);
  body.replaceChildren(form, out);
}

// ------------------------------------------------------------------ team comments

async function showComments(body) {
  const picker = targetPicker();
  const timing = timingPicker();
  let source = 'library';
  let appearance = 'dzplay';
  const chosen = new Set();
  const sourceBox = h('div', {});
  body.replaceChildren(spinner());
  const lib = await attempt(() => call('GET', '/api/admin/library'));
  if (!lib) { body.replaceChildren(); return; }

  const catSelect = (withAll) => {
    const s = h('select', { class: 'input admin-select' },
      ...(withAll ? [h('option', { value: '', text: 'كل التصنيفات' })] : []),
      ...lib.categories.map((c) => h('option', { value: c.id, text: `${c.name} (${fmt(c.items)})` })));
    return s;
  };

  const libraryPick = () => {
    const filter = catSelect(true);
    const q = h('input', { class: 'input', type: 'search', dir: 'auto', placeholder: 'بحث في المكتبة' });
    const list = h('div', { class: 'admin-picklist' });
    const n = h('span', { class: 'admin-meta' });
    const paint = () => {
      const term = q.value.trim().toLowerCase();
      const rows = lib.items.filter((i) => (!filter.value || i.category === filter.value) && (!term || i.text.toLowerCase().includes(term)));
      n.textContent = `${fmt(chosen.size)} تعليق محدد`;
      list.replaceChildren(...(rows.length ? rows.map((i) => {
        const box = h('input', { type: 'checkbox', checked: chosen.has(i.id) ? true : null });
        box.addEventListener('change', () => { if (box.checked) chosen.add(i.id); else chosen.delete(i.id); n.textContent = `${fmt(chosen.size)} تعليق محدد`; });
        return h('label', { class: 'admin-check admin-pick' }, box, h('span', { dir: 'auto', text: i.text }),
          h('span', { class: 'admin-meta', text: `${i.category_name} · استُخدم ${fmt(i.usage_count)}` }));
      }) : [h('p', { class: 'admin-meta', text: 'المكتبة فارغة هنا. أضف تعليقات من «المكتبة».' })]));
    };
    filter.addEventListener('change', paint);
    q.addEventListener('input', paint);
    paint();
    return h('div', {}, h('div', { class: 'admin-filters__row' }, filter, q), n, list);
  };

  let randomCat = null;
  let randomCount = null;
  const randomPick = () => {
    randomCat = catSelect(false);
    randomCount = h('input', { class: 'input', type: 'number', min: '1', value: '3', inputmode: 'numeric' });
    return h('div', {}, h('div', { class: 'admin-filters__row' }, field('التصنيف', randomCat), field('العدد لكل منشور', randomCount)),
      h('p', { class: 'admin-meta', text: 'تُختار تعليقات عشوائية مختلفة لكل منشور، ولا يتكرر نفس النص على نفس المنشور أبدًا.' }));
  };

  let newText = null;
  const textPick = () => {
    newText = h('textarea', { class: 'input', rows: '4', dir: 'auto', maxlength: '1000', placeholder: 'نص التعليق' });
    return h('div', {}, newText);
  };

  const paintSource = () => sourceBox.replaceChildren(({ library: libraryPick, random: randomPick, text: textPick })[source]());
  paintSource();

  const appearanceHint = h('p', { class: 'admin-meta' });
  const paintAppearance = () => {
    appearanceHint.textContent = appearance === 'official'
      ? 'يظهر باسم «DZPLAY الرسمي» مع شارة التوثيق.'
      : 'يظهر باسم «dzplay» من أحد حسابات النظام (لا تدخل في المطابقة ولا في إحصائيات المستخدمين).';
  };
  paintAppearance();
  const out = h('div', {});
  const submit = h('button', { type: 'submit', class: 'btn btn--primary btn--block' }, icon('chat'), 'نشر التعليقات');
  const form = h('form', {
    class: 'admin-form glass',
    onsubmit: async (e) => {
      e.preventDefault();
      const ids = picker.ids();
      if (!ids.length) { toast('حدد منشورًا واحدًا على الأقل.', 'error'); return; }
      const src = { kind: source };
      if (source === 'library') {
        if (!chosen.size) { toast('اختر تعليقًا واحدًا على الأقل.', 'error'); return; }
        src.library_ids = [...chosen];
      } else if (source === 'random') {
        src.category = randomCat.value;
        src.count = Math.trunc(Number(randomCount.value) || 0);
      } else {
        if (!newText.value.trim()) { toast('اكتب نص التعليق.', 'error'); return; }
        src.text = newText.value;
      }
      submit.disabled = true;
      const r = await attempt(() => call('POST', '/api/admin/engagement/comments', {
        target_type: picker.type(), ids, source: src, appearance, duration_minutes: timing.minutes(),
      }));
      submit.disabled = false;
      if (!r) return;
      const planned = r.targets.reduce((a, t) => a + t.planned, 0);
      const posted = r.targets.reduce((a, t) => a + t.posted, 0);
      toast(r.gradual ? `جُدول ${fmt(planned)} تعليقًا.` : `نُشر ${fmt(posted)} تعليقًا.`);
      out.replaceChildren(
        h('p', { class: 'admin-meta', text: 'التعليقات المكررة على نفس المنشور تُتخطى تلقائيًا.' }),
        resultTable(r.targets, [['المنشور', (t) => shortRef(t.id)], ['مخطط', (t) => fmt(t.planned)], ['منشور الآن', (t) => fmt(t.posted)]]));
    },
  },
  picker.el,
  h('fieldset', { class: 'admin-fieldset' },
    h('legend', { text: 'مصدر التعليقات' }),
    segmented([['library', 'من المكتبة'], ['random', 'عشوائي من تصنيف'], ['text', 'نص جديد']], source, (v) => { source = v; paintSource(); }, 'المصدر'),
    sourceBox),
  h('fieldset', { class: 'admin-fieldset' },
    h('legend', { text: 'الظهور' }),
    segmented([['dzplay', 'dzplay'], ['official', 'DZPLAY الرسمي']], appearance, (v) => { appearance = v; paintAppearance(); }, 'الظهور'),
    appearanceHint,
    h('p', { class: 'admin-meta', text: 'على الأفكار: يصل التعليق لصاحب الفكرة فقط (مثل أي تعليق). على Reels: يظهر للجميع.' })),
  timing.el,
  submit);
  body.replaceChildren(form, out);
}

// ------------------------------------------------------------------ jobs

function bar(pct) {
  const fill = h('span', {});
  fill.style.inlineSize = `${pct}%`; // CSSOM, allowed by the CSP (no inline style attributes)
  return fill;
}

async function showJobs(body) {
  const list = h('div', { class: 'admin-list' }, spinner());
  const load = async () => {
    list.replaceChildren(spinner());
    const d = await attempt(() => call('GET', `/api/admin/engagement/jobs${qs({ status: view.jobStatus })}`));
    list.replaceChildren();
    if (!d) return;
    if (!d.jobs.length) { list.append(emptyState(view.jobStatus === 'running' ? 'لا توجد عمليات جارية.' : 'لا عمليات بعد.')); return; }
    const batches = new Map();
    for (const j of d.jobs) {
      if (!batches.has(j.batch_id)) batches.set(j.batch_id, []);
      batches.get(j.batch_id).push(j);
    }
    for (const [batch, jobs] of batches) {
      const first = jobs[0];
      const running = jobs.some((j) => j.status === 'running');
      const done = jobs.reduce((a, j) => a + Math.abs(j.applied), 0);
      const total = jobs.reduce((a, j) => a + Math.abs(j.total), 0);
      const pct = total ? Math.round((done / total) * 100) : 100;
      list.append(h('article', { class: 'admin-card glass' },
        h('div', { class: 'admin-card__head' },
          chip(first.kind === 'boost' ? 'تعزيز' : 'تعليقات'),
          h('span', { text: `${fmt(new Set(jobs.map((j) => j.target_id)).size)} ${TYPE_LABEL[first.target_type] || ''}` }),
          chip(JOB_STATUS[running ? 'running' : first.status] || first.status, running ? 'chip--live' : ''),
          h('time', { class: 'admin-card__time', text: when(first.start_at) })),
        h('div', { class: 'admin-progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100', 'aria-valuenow': String(pct) },
          bar(pct)),
        h('p', { class: 'admin-meta', text: `${fmt(done)} من ${fmt(total)} · ينتهي ${when(first.end_at)} · بواسطة ${first.created_by}` }),
        h('details', { class: 'admin-fold' }, h('summary', {}, h('span', { text: 'التفاصيل' }), h('span', { class: 'chip', text: fmt(jobs.length) })),
          ...jobs.map((j) => h('div', { class: 'admin-line' },
            h('code', { dir: 'ltr', text: shortRef(j.target_id) }),
            h('span', { text: j.kind === 'boost' ? (j.metric === 'likes' ? '👍' : '👎') : '💬' }),
            h('span', { text: `${fmt(j.applied)} / ${fmt(j.total)}` }),
            chip(JOB_STATUS[j.status] || j.status),
            j.status === 'running' ? h('button', { type: 'button', class: 'admin-link', onclick: () => cancel(j.id) }, 'إلغاء') : null))),
        running ? h('div', { class: 'admin-actions' },
          h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: () => cancel(batch) }, 'إلغاء العملية كلها')) : null));
    }
  };
  const cancel = async (id) => {
    if (!(await confirmDanger('إلغاء العملية؟', 'يتوقف ما تبقى منها. ما طُبِّق حتى الآن يبقى كما هو.', 'إلغاء العملية'))) return;
    const r = await attempt(() => call('POST', `/api/admin/engagement/jobs/${encodeURIComponent(id)}/cancel`));
    if (r) { toast('أُلغيت.'); load(); }
  };
  body.replaceChildren(
    segmented([['running', 'الجارية'], ['', 'الكل']], view.jobStatus, (v) => { view.jobStatus = v; load(); }, 'الحالة'),
    list);
  load();
}

// ------------------------------------------------------------------ library

async function showLibrary(body) {
  const cats = h('div', { class: 'admin-chips' });
  const list = h('div', { class: 'admin-list' });
  const q = h('input', { class: 'input', type: 'search', dir: 'auto', value: view.libQ, placeholder: 'بحث في نص التعليقات' });
  let data = null;

  const load = async () => {
    list.replaceChildren(spinner());
    data = await attempt(() => call('GET', `/api/admin/library${qs({ q: view.libQ, category: view.libCategory })}`));
    if (!data) { list.replaceChildren(); return; }
    paintCats();
    list.replaceChildren(h('p', { class: 'admin-meta', text: `${fmt(data.items.length)} تعليق` }),
      ...(data.items.length ? data.items.map(itemCard) : [emptyState('لا تعليقات هنا بعد.')]));
  };

  const paintCats = () => {
    const pick = (id) => { view.libCategory = id; load(); };
    cats.replaceChildren(
      h('button', { type: 'button', class: 'chip admin-chip', 'aria-pressed': String(!view.libCategory), onclick: () => pick('') }, 'الكل'),
      ...data.categories.map((c) => h('button', {
        type: 'button', class: 'chip admin-chip', 'aria-pressed': String(view.libCategory === c.id), onclick: () => pick(c.id),
      }, `${c.name} · ${fmt(c.items)}`)),
      h('button', { type: 'button', class: 'chip admin-chip', onclick: manageCategories }, '＋ التصنيفات'));
  };

  const itemCard = (i) => h('article', { class: 'admin-card glass' },
    h('p', { class: 'admin-text', dir: 'auto', text: i.text }),
    h('div', { class: 'admin-actions' },
      chip(i.category_name),
      h('span', { class: 'admin-meta', text: `استُخدم ${fmt(i.usage_count)} مرة` }),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: () => editItem(i) }, 'تعديل'),
      h('button', { type: 'button', class: 'btn btn--danger btn--sm', onclick: async () => {
        if (!(await confirmDanger('حذف التعليق من المكتبة؟', i.text.slice(0, 120), 'حذف'))) return;
        if (await attempt(() => call('DELETE', `/api/admin/library/${encodeURIComponent(i.id)}`))) { toast('حُذف.'); load(); }
      } }, 'حذف')));

  const categorySelect = (value) => {
    const s = h('select', { class: 'input admin-select' }, ...data.categories.map((c) => h('option', { value: c.id, text: c.name })));
    s.value = value || view.libCategory || (data.categories[0] && data.categories[0].id) || '';
    return s;
  };

  const editItem = (item) => sheet((panel, close) => {
    const cat = categorySelect(item && item.category);
    const ta = h('textarea', { class: 'input', rows: '4', dir: 'auto', maxlength: '1000' });
    ta.value = item ? item.text : '';
    panel.append(h('h2', { text: item ? 'تعديل تعليق' : 'تعليق جديد' }), field('التصنيف', cat), field('النص', ta),
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn btn--primary btn--block', onclick: async () => {
          const path = item ? `/api/admin/library/${encodeURIComponent(item.id)}` : '/api/admin/library';
          if (await attempt(() => call(item ? 'PUT' : 'POST', path, { category: cat.value, text: ta.value }))) { toast('حُفظ.'); close(); load(); }
        } }, 'حفظ'),
        h('button', { type: 'button', class: 'btn btn--ghost btn--block', onclick: close }, 'إلغاء')));
    ta.focus();
  });

  const bulkImport = () => sheet((panel, close) => {
    const cat = categorySelect();
    const ta = h('textarea', { class: 'input', rows: '10', dir: 'auto', placeholder: 'تعليق في كل سطر' });
    panel.append(h('h2', { text: 'استيراد دفعة' }),
      h('p', { class: 'admin-meta', text: 'كل سطر يصبح تعليقًا. الأسطر الفارغة والمكررة (داخل نفس التصنيف) تُتخطى.' }),
      field('التصنيف', cat), ta,
      h('div', { class: 'actions' },
        h('button', { type: 'button', class: 'btn btn--primary btn--block', onclick: async () => {
          const r = await attempt(() => call('POST', '/api/admin/library/import', { category: cat.value, text: ta.value }));
          if (r) { toast(`أُضيف ${fmt(r.added)} · تُخطي ${fmt(r.skipped)}.`); close(); load(); }
        } }, 'استيراد'),
        h('button', { type: 'button', class: 'btn btn--ghost btn--block', onclick: close }, 'إلغاء')));
  });

  const manageCategories = () => sheet((panel, close) => {
    const rows = h('div', { class: 'admin-list' });
    const paint = () => rows.replaceChildren(...data.categories.map((c) => {
      const name = h('input', { class: 'input', value: c.name, maxlength: '64', 'aria-label': 'اسم التصنيف' });
      return h('div', { class: 'admin-line' }, name, h('span', { class: 'chip', text: fmt(c.items) }),
        h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: async () => {
          if (await attempt(() => call('PUT', `/api/admin/library/categories/${encodeURIComponent(c.id)}`, { name: name.value }))) { toast('حُفظ.'); await load(); paint(); }
        } }, 'حفظ'),
        h('button', { type: 'button', class: 'btn btn--danger btn--sm', disabled: c.items ? true : null, title: c.items ? 'انقل تعليقاته أو احذفها أولًا' : null, onclick: async () => {
          if (!(await confirmDanger(`حذف التصنيف «${c.name}»؟`, 'التصنيف فارغ.', 'حذف'))) return;
          if (await attempt(() => call('DELETE', `/api/admin/library/categories/${encodeURIComponent(c.id)}`))) {
            if (view.libCategory === c.id) view.libCategory = '';
            toast('حُذف.'); await load(); paint();
          }
        } }, 'حذف'));
    }));
    const add = h('input', { class: 'input', maxlength: '64', placeholder: 'اسم تصنيف جديد' });
    panel.append(h('h2', { text: 'التصنيفات' }), rows,
      h('form', { class: 'admin-filter', onsubmit: async (e) => {
        e.preventDefault();
        if (!add.value.trim()) return;
        if (await attempt(() => call('POST', '/api/admin/library/categories', { name: add.value }))) { add.value = ''; toast('أُضيف.'); await load(); paint(); }
      } }, add, h('button', { type: 'submit', class: 'btn btn--primary btn--sm' }, 'إضافة')),
      h('div', { class: 'actions' }, h('button', { type: 'button', class: 'btn btn--ghost btn--block', onclick: close }, 'إغلاق')));
    paint();
  });

  body.replaceChildren(
    cats,
    h('form', { class: 'admin-filter', onsubmit: (e) => { e.preventDefault(); view.libQ = q.value.trim(); load(); } },
      q, h('button', { type: 'submit', class: 'btn btn--ghost btn--sm' }, 'بحث')),
    h('div', { class: 'admin-actions' },
      h('button', { type: 'button', class: 'btn btn--primary btn--sm', onclick: () => editItem(null) }, '＋ تعليق جديد'),
      h('button', { type: 'button', class: 'btn btn--ghost btn--sm', onclick: bulkImport }, 'استيراد دفعة')),
    list);
  load();
}
