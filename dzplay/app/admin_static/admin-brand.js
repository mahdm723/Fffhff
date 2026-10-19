// «الهوية» (super admin): the new logo before it is applied — the chosen concept (three candlesticks) in its two
// versions to choose from, A and B side by side (A on the left, B on the right, whatever the page direction), each
// at real pixel sizes, on dark and light, as an Android icon (circle / rounded square / squircle, themed
// monochrome), as the notification icon, in one colour, next to the DALTA.BIT wordmark, with the dot's glow pulse
// and on a phone home screen; the earlier concepts; the brand colours and a proposal (not applied) to align the
// app's default accent with the brand red. Nothing here changes the app.
// V6 phase 19 «الشارات»: the blue star and the gold badge at every size, on the app's dark and light pages and on
// two wallpapers, with their measured contrast.
import { appName, wordmarkParts } from '/js/brand.js';
import { goldMark, h, nameLine, starMark } from '/js/ui.js';
import { icon } from '/js/icons.js';
import { call, handleError, spinner } from './admin-common.js';

const NS = 'http://www.w3.org/2000/svg';
const SIZES = [16, 24, 48, 96, 192, 512]; // small first: legibility is decided at 16–48px
// Android adaptive icon: a 108-unit canvas, content guaranteed visible inside the 66-unit circle. The marks
// (100 units) are scaled so every drawn pixel stays inside it (the same value as brand/build.py).
const FG_SCALE = 0.57;

// The logo files come from the repository (trusted), and are still filtered: only plain shapes and the dot's
// radial gradients survive; a fill may only point at a gradient of the same file (url(#id)).
const TAGS = new Set(['svg', 'g', 'path', 'circle', 'rect', 'defs', 'radialGradient', 'stop']);
const ATTRS = new Set(['class', 'd', 'cx', 'cy', 'r', 'x', 'y', 'width', 'height', 'rx', 'fill', 'stroke', 'stroke-width',
  'stroke-linejoin', 'stroke-linecap', 'fill-rule', 'viewBox', 'id', 'fx', 'fy', 'offset', 'stop-color', 'stop-opacity']);
const URL_REF = /^url\(#([A-Za-z][\w-]*)\)$/;
let copies = 0;

function shapes(svgText) {
  const doc = new DOMParser().parseFromString(svgText, 'image/svg+xml');
  const root = doc.documentElement;
  if (root.nodeName !== 'svg') return [];
  // every copy gets its own gradient ids (dozens of copies share one page; some sit in closed <details>)
  const suffix = `-c${(copies += 1)}`;
  const clean = (node) => {
    if (node.nodeType !== 1 || !TAGS.has(node.nodeName)) return null;
    const el = document.createElementNS(NS, node.nodeName);
    for (const a of node.attributes) {
      if (!ATTRS.has(a.name)) continue;
      let v = a.value;
      if (a.name === 'id') v += suffix;
      if (v.startsWith('url(')) { const m = v.match(URL_REF); if (!m) continue; v = `url(#${m[1]}${suffix})`; }
      el.setAttribute(a.name, v);
    }
    for (const c of node.childNodes) { const k = clean(c); if (k) el.append(k); }
    return el;
  };
  return [...root.childNodes].map(clean).filter(Boolean);
}

function svgEl(size, viewBox) {
  const s = document.createElementNS(NS, 'svg');
  s.setAttribute('viewBox', viewBox);
  s.setAttribute('width', String(size));
  s.setAttribute('height', String(size));
  s.setAttribute('aria-hidden', 'true');
  return s;
}

/** The mark alone (transparent): `ink` for the shape, the dot red unless `mono`. */
function mark(concept, size, ink, { mono = false } = {}) {
  const s = svgEl(size, '0 0 100 100');
  s.classList.add('brand-mark');
  if (concept.variant) s.dataset.logo = concept.variant;
  if (mono) s.classList.add('brand-mark--mono');
  s.style.color = ink;
  s.append(...shapes(concept.svg));
  return s;
}

const MASKS = {
  squircle: (bg) => { const r = document.createElementNS(NS, 'rect'); Object.entries({ width: 108, height: 108, rx: 38, fill: bg }).forEach(([k, v]) => r.setAttribute(k, v)); return r; },
  rounded: (bg) => { const r = document.createElementNS(NS, 'rect'); Object.entries({ width: 108, height: 108, rx: 24, fill: bg }).forEach(([k, v]) => r.setAttribute(k, v)); return r; },
  circle: (bg) => { const c = document.createElementNS(NS, 'circle'); Object.entries({ cx: 54, cy: 54, r: 54, fill: bg }).forEach(([k, v]) => c.setAttribute(k, v)); return c; },
};

/** The app icon: a background in the given mask + the mark in the safe zone. */
function appIcon(concept, size, { shape = 'squircle', bg, ink, mono = false } = {}) {
  const s = svgEl(size, '0 0 108 108');
  s.classList.add('brand-icon');
  if (concept.variant) s.dataset.logo = concept.variant;
  if (mono) s.classList.add('brand-mark--mono');
  s.style.color = ink;
  const g = document.createElementNS(NS, 'g');
  const off = 54 - 50 * FG_SCALE;
  g.setAttribute('transform', `translate(${off} ${off}) scale(${FG_SCALE})`);
  g.append(...shapes(concept.svg));
  s.append(MASKS[shape](bg), g);
  return s;
}

function wordmark(colors, ink, size = 26) {
  const dot = h('span', { class: 'brand-word__dot', 'aria-hidden': 'true' });
  dot.style.background = colors.red;
  const [first, rest] = wordmarkParts(); // the name from the settings (APP_NAME): its dot is the red dot
  const w = h('span', { class: 'brand-word', dir: 'ltr', 'aria-label': appName() }, first, dot, rest || null);
  w.style.color = ink;
  w.style.fontSize = `${size}px`;
  return w;
}

function labelled(node, text) {
  return h('figure', { class: 'brand-cell' }, node, h('figcaption', { text }));
}

/** A big size drawn to fit the column (512 never fits a phone): the real size is shown up to 192. */
function fitted(node, text) {
  return h('figure', { class: 'brand-cell brand-cell--fit' }, node, h('figcaption', { text }));
}

function wrap(...cells) {
  return h('div', { class: 'brand-strip brand-strip--wrap' }, ...cells);
}

// dark / light: the brand black / white (for the mark alone); wall-dark / wall-light: a neutral home-screen
// wallpaper, so the outline of a black or white icon stays visible
const PANEL_BG = { dark: null, light: null, 'wall-dark': '#2b2f3a', 'wall-light': '#e6e8ee' };

function panel(tone, colors, ...children) {
  const p = h('div', { class: `brand-panel brand-panel--${tone}` }, ...children);
  const dark = tone.endsWith('dark');
  p.style.background = PANEL_BG[tone] || (dark ? colors.black : colors.white);
  p.style.color = dark ? colors.white : colors.black;
  return p;
}

// a phone home screen: generic system-style apps (no real brand), with the concept in the grid and the dock
const NEIGHBOURS = [['clock', 'الساعة', '#5b6b8c'], ['image', 'الصور', '#3c8d7c'], ['lock', 'الخزنة', '#7a5c99'],
  ['chat', 'الرسائل', '#3d7bd9'], ['user', 'جهات الاتصال', '#b0703c'], ['bell', 'التنبيهات', '#9a4f62'],
  ['timer', 'المؤقت', '#4f7f9a'], ['flag', 'المهام', '#6d8b3c'], ['send', 'البريد', '#5662b8'], ['shield', 'الأمان', '#46505f'],
  ['feather', 'الملاحظات', '#a07c2c']];

function phone(concept, colors, shape, small = null) {
  const app = (glyph, label, color) => {
    const disc = h('span', { class: `brand-phone__app brand-phone__app--${shape}` }, icon(glyph));
    disc.style.background = color;
    return h('span', { class: 'brand-phone__cell' }, disc, h('small', { text: label }));
  };
  const ours = h('span', { class: 'brand-phone__cell' },
    appIcon(concept, 46, { shape, bg: colors.black, ink: colors.white }), h('small', { text: appName() }));
  const grid = [...NEIGHBOURS.slice(0, 6).map((n) => app(...n)), ours, ...NEIGHBOURS.slice(6, 9).map((n) => app(...n))];
  return h('div', { class: 'brand-phone', 'aria-label': 'محاكاة شاشة هاتف رئيسية' },
    h('div', { class: 'brand-phone__status' },
      h('span', { class: 'brand-phone__left' }, h('span', { text: '9:41' }), small ? mark(small, 13, '#fff', { mono: true }) : null),
      h('span', { text: '5G ▮▮▮' })),
    h('div', { class: 'brand-phone__grid' }, ...grid),
    h('div', { class: 'brand-phone__dock' }, ...NEIGHBOURS.slice(9).map((n) => app(...n)),
      h('span', { class: 'brand-phone__cell' }, appIcon(concept, 46, { shape, bg: colors.black, ink: colors.white }))));
}

function conceptCard(c, i, colors) {
  // 16–96 together (they decide legibility), 192 and 512 in their own row that scrolls sideways on a phone
  const icons = (tone, list) => list.map((px) => labelled(
    appIcon(c, px, tone === 'dark' ? { bg: colors.black, ink: colors.white } : { bg: colors.white, ink: colors.black }), `${px}`));
  const sizes = (tone) => h('div', {}, wrap(...icons(tone, SIZES.slice(0, 5))),
    fitted(appIcon(c, 512, tone === 'dark' ? { bg: colors.black, ink: colors.white } : { bg: colors.white, ink: colors.black }), '512 · مصغّرة لتناسب العرض'));
  const loose = (tone) => wrap(...[96, 48, 24, 16].map((px) => labelled(mark(c, px, tone === 'dark' ? colors.white : colors.black), `${px}`)));
  return h('section', { class: 'admin-card glass brand-concept', id: `concept-${c.id}`, 'aria-labelledby': `concept-${c.id}-title` },
    h('header', { class: 'brand-concept__head' },
      h('span', { class: 'brand-concept__num', text: String(i + 1) }),
      h('div', {}, h('h2', { id: `concept-${c.id}-title`, text: c.title }), h('p', { class: 'admin-meta', text: c.idea }))),
    h('ul', { class: 'brand-notes' }, ...c.notes.map((n) => h('li', { text: n }))),

    h('h3', { text: 'أيقونة التطبيق بحجمها الحقيقي: 16 · 24 · 48 · 96 · 192، و512 مصغّرة' }),
    panel('wall-dark', colors, sizes('dark')),
    panel('wall-light', colors, sizes('light')),

    h('h3', { text: 'الرمز وحده على خلفية داكنة وفاتحة (للعناوين والـfavicon)' }),
    h('div', { class: 'brand-pair' }, panel('dark', colors, loose('dark')), panel('light', colors, loose('light'))),

    h('h3', { text: 'أيقونة Android (adaptive): دائرة، مربع مستدير، squircle، ونسخة Android 13 أحادية' }),
    panel('wall-dark', colors, wrap(
      labelled(appIcon(c, 96, { shape: 'circle', bg: colors.black, ink: colors.white }), 'دائرة'),
      labelled(appIcon(c, 96, { shape: 'rounded', bg: colors.black, ink: colors.white }), 'مربع مستدير'),
      labelled(appIcon(c, 96, { shape: 'squircle', bg: colors.black, ink: colors.white }), 'squircle'),
      labelled(appIcon(c, 96, { shape: 'circle', bg: '#DCE3F2', ink: '#1F2F4F', mono: true }), 'Android 13 (themed)'),
      labelled(appIcon(c, 96, { shape: 'circle', bg: '#2A2F3A', ink: '#C9D6F2', mono: true }), 'themed داكن'))),

    h('h3', { text: 'نسخة بلون واحد' }),
    h('div', { class: 'brand-pair' },
      panel('light', colors, wrap(labelled(mark(c, 96, colors.black, { mono: true }), 'أسود'), labelled(mark(c, 24, colors.black, { mono: true }), '24'))),
      panel('dark', colors, wrap(labelled(mark(c, 96, colors.white, { mono: true }), 'أبيض'), labelled(mark(c, 24, colors.white, { mono: true }), '24 (إشعار)')))),

    h('h3', { text: 'مع الاسم (wordmark)' }),
    panel('dark', colors, h('div', { class: 'brand-lockup' }, mark(c, 44, colors.white), wordmark(colors, colors.white))),
    panel('light', colors, h('div', { class: 'brand-lockup' }, mark(c, 44, colors.black), wordmark(colors, colors.black))),

    h('h3', { text: 'على شاشة هاتف رئيسية' }),
    h('div', { class: 'brand-phones' }, phone(c, colors, 'circle'), phone(c, colors, 'squircle')));
}

// ------------------------------------------------------------------ the chosen logo: versions A and B

const pick = (large, small, px) => (px <= 24 ? small : large); // 16–24 px: the small version

function notificationMock(small, colors) {
  const tinted = (px, ink) => mark(small, px, ink, { mono: true });
  return h('div', { class: 'brand-notif' },
    h('div', { class: 'brand-notif__bar' },
      h('span', { class: 'brand-phone__left' }, h('span', { text: '9:41' }), tinted(16, '#fff'), tinted(16, '#fff')),
      h('span', { text: '5G ▮▮▮' })),
    h('div', { class: 'brand-notif__card' },
      h('div', { class: 'brand-notif__head' }, tinted(18, '#E5232D'), h('small', { text: `${appName()} · الآن` })),
      h('b', { text: 'رسالة جديدة' }),
      h('span', { text: `لديك رسالة جديدة على ${appName()}` })),
    h('div', { class: 'brand-strip brand-strip--wrap' },
      labelled(tinted(24, '#fff'), '24 أبيض'), labelled(tinted(36, '#fff'), '36'), labelled(tinted(48, '#fff'), '48')));
}

function chosenCard(d, colors) {
  const V = d.chosen.variants.map((v) => ({ ...v, L: { ...v.large, variant: v.id }, S: { ...v.small, variant: v.id } }));
  const on = (tone) => (tone === 'dark' ? { bg: colors.black, ink: colors.white } : { bg: colors.white, ink: colors.black });
  const inkOf = (tone) => (tone === 'dark' ? colors.white : colors.black);
  // A on the left, B on the right (a phone stacks them, A first); each column says which one it is
  // one column per version (A on the left, B on the right when there are two); a single version needs no tag
  const both = (fn, tight = false) => h('div', { class: `brand-ab${tight && V.length > 1 ? ' brand-ab--tight' : ''}` },
    ...V.map((v) => h('div', { class: 'brand-ab__col' }, V.length > 1 ? h('span', { class: 'brand-ab__tag', text: `النسخة ${v.label}` }) : null, fn(v))));
  const icon2 = (v, px, tone, small = px <= 24) => labelled(appIcon(small ? v.S : v.L, px, on(tone)), `${px}${small ? ' · صغيرة' : ''}`);
  const sizes = (v, tone) => h('div', {},
    wrap(...[16, 24, 48, 96].map((px) => icon2(v, px, tone))),
    wrap(icon2(v, 192, tone)), fitted(appIcon(v.L, 512, on(tone)), '512 · مصغّرة لتناسب العرض'));
  const compare = (v) => wrap(...[16, 24].flatMap((px) => [
    labelled(appIcon(v.L, px, on('dark')), `${px} كبيرة`), labelled(appIcon(v.S, px, on('dark')), `${px} صغيرة`)]));
  const loose = (v, tone) => wrap(...[96, 48, 24, 16].map((px) => labelled(mark(pick(v.L, v.S, px), px, inkOf(tone)), `${px}`)));
  const android = (v, px) => wrap(
    labelled(appIcon(v.L, px, { shape: 'circle', bg: colors.black, ink: colors.white }), 'دائرة'),
    labelled(appIcon(v.L, px, { shape: 'rounded', bg: colors.black, ink: colors.white }), 'مربع مستدير'),
    labelled(appIcon(v.L, px, { shape: 'squircle', bg: colors.black, ink: colors.white }), 'squircle'),
    labelled(appIcon(v.L, px, { shape: 'circle', bg: '#DCE3F2', ink: '#1F2F4F', mono: true }), 'Android 13'),
    labelled(appIcon(v.L, px, { shape: 'circle', bg: '#2A2F3A', ink: '#C9D6F2', mono: true }), 'Android 13 داكن'));
  const mono = (v, tone) => wrap(labelled(mark(v.L, 96, inkOf(tone), { mono: true }), tone === 'dark' ? 'أبيض' : 'أسود'),
    labelled(mark(v.S, 24, inkOf(tone), { mono: true }), '24'));
  const lockup = (v, tone, anim = false) => h('div', { class: 'brand-stack' },
    panel(tone, colors, h('div', { class: `brand-lockup${anim ? ' brand-anim' : ''}` }, mark(v.L, anim ? 96 : 48, inkOf(tone)), wordmark(colors, inkOf(tone)))),
    panel(tone, colors, h('div', { class: `brand-lockup brand-lockup--header${anim ? ' brand-anim' : ''}` }, mark(v.S, 24, inkOf(tone)), wordmark(colors, inkOf(tone), 18))));

  return h('section', { class: 'admin-card glass brand-concept', id: 'concept-chosen', 'aria-labelledby': 'concept-chosen-title' },
    h('header', { class: 'brand-concept__head' },
      h('span', { class: 'brand-concept__num', text: '✓' }),
      h('div', {}, h('h2', { id: 'concept-chosen-title', text: d.chosen.title }),
        h('p', { class: 'admin-meta', text: `${d.chosen.idea} رسمتان: الكبيرة من 48px فما فوق، والصغيرة لـ16 و24px ولأيقونة الإشعارات. النقطة متوهجة، ومسطحة بلون واحد في أيقونة الإشعارات ونسخة Android 13 (شرط النظام). لا يُعكس الشعار في الصفحات العربية.` }))),
    both((v) => panel('wall-dark', colors, h('div', { class: 'brand-hero' },
      labelled(appIcon(v.L, 120, on('dark')), 'الكبيرة'), labelled(appIcon(v.S, 56, on('dark')), 'الصغيرة'))), true),
    h('ul', { class: 'brand-notes' },
      ...V.map((v) => h('li', {}, h('b', { text: `${v.title}: ` }), v.notes)),
      h('li', { text: `الكبيرة: ${d.chosen.notes.large} (${d.chosen.use.large})` }),
      h('li', { text: `الصغيرة: ${d.chosen.notes.small} (${d.chosen.use.small})` })),

    h('h3', { text: 'كل حجم بنسخته: 16 و24 (الصغيرة) · 48 · 96 · 192 · 512 (الكبيرة)' }),
    both((v) => panel('wall-dark', colors, sizes(v, 'dark'))),
    both((v) => panel('wall-light', colors, sizes(v, 'light'))),

    h('h3', { text: 'مقارنة في الأحجام الصغيرة: الرسمة الكبيرة ثم الصغيرة' }),
    both((v) => panel('wall-dark', colors, compare(v))),

    h('h3', { text: 'الرمز وحده على خلفية داكنة وفاتحة (رأس الصفحات والـfavicon)' }),
    both((v) => panel('dark', colors, loose(v, 'dark'))),
    both((v) => panel('light', colors, loose(v, 'light'))),

    h('h3', { text: 'أيقونة Android (adaptive): دائرة، مربع مستدير، squircle، وAndroid 13 أحادية — 96 ثم 48' }),
    both((v) => panel('wall-dark', colors, android(v, 96))),
    both((v) => panel('wall-light', colors, android(v, 48))),

    h('h3', { text: 'أيقونة الإشعارات: الرسمة الصغيرة بيضاء مسطحة (بلا توهج)' }),
    both((v) => panel('wall-dark', colors, notificationMock(v.S, colors))),

    h('h3', { text: 'بلون واحد (بلا توهج)' }),
    both((v) => h('div', { class: 'brand-stack' }, panel('light', colors, mono(v, 'light')), panel('dark', colors, mono(v, 'dark')))),

    h('h3', { text: 'مع الاسم: الشعار الكامل، ورأس صفحة التطبيق (24px)' }),
    both((v) => h('div', { class: 'brand-stack' }, lockup(v, 'dark'), lockup(v, 'light'))),

    h('h3', { text: 'توهج النقطة داخل التطبيق: نبض خفيف وبطيء (يتوقف مع «تقليل الحركة» في الهاتف)' }),
    both((v) => h('div', { class: 'brand-stack' }, lockup(v, 'dark', true), lockup(v, 'light', true))),

    h('h3', { text: 'على الشاشة الرئيسية (مع أيقونة الإشعار في شريط الحالة)' }),
    both((v) => h('div', { class: 'brand-phones' }, phone(v.L, colors, 'squircle', v.S))),
    both((v) => h('div', { class: 'brand-phones' }, phone(v.L, colors, 'circle', v.S))));
}

// ------------------------------------------------------------------ «الشارات» (V6 phase 19)

const BADGE_SIZES = [14, 16, 18, 20, 24, 34, 56];
// the app's own page colours (appearance.SURFACES) and two wallpapers; each panel takes its theme's badge colours
const BADGE_TONES = [['dark', 'الوضع الداكن'], ['light', 'الوضع الفاتح'], ['wall-dark', 'خلفية صورة داكنة'], ['wall-light', 'خلفية صورة فاتحة']];

function sizedStar(px) {
  const s = starMark();
  s.style.width = `${px}px`;
  s.style.height = `${px}px`;
  return s;
}

function badgesCard(d) {
  const b = d.badges;
  const tonePanel = ([tone, title]) => {
    const mode = tone.endsWith('dark') ? 'dark' : 'light';
    const p = h('div', { class: 'brand-panel brand-badges__panel', 'data-tone': tone });
    p.style.background = PANEL_BG[tone] || d.surfaces[mode].bg;
    p.style.color = mode === 'dark' ? '#eef0f8' : '#1a1c2a';
    p.style.setProperty('--star', b[mode].star.color);
    p.style.setProperty('--gold', b[mode].gold.color);
    const row = (make, name, kind) => h('div', { class: 'brand-badges__row', 'data-badge': kind },
      h('small', { class: 'brand-badges__name', text: name }),
      wrap(...BADGE_SIZES.map((px) => labelled(make(px), `${px}`))));
    p.append(h('b', { class: 'brand-badges__title', text: title }),
      row(sizedStar, 'النجمة الزرقاء', 'star'),
      row((px) => goldMark({ size: px, interactive: false }), 'الشارة الذهبية (معدنية من 28px)', 'gold'),
      h('div', { class: 'brand-badges__names' }, nameLine(appName(), null, 'brand-badges__who', false, true),
        nameLine('اسم موثّق', null, '', true)));
    return p;
  };
  // the measured contrast (the rule: ≥ 3:1 on the page and the cards, and of the white check on the badge)
  const num = (v) => h('td', { dir: 'ltr', text: `${v}:1` });
  const contrast = h('div', { class: 'viz-table glass' }, h('table', {},
    h('thead', {}, h('tr', {}, ...['', 'اللون', 'على الصفحة', 'على البطاقات', 'العلامة البيضاء'].map((c) => h('th', { text: c })))),
    h('tbody', {}, ...['dark', 'light'].flatMap((mode) => [['gold', 'الذهبية'], ['star', 'الزرقاء']].map(([key, name]) => {
      const v = b[mode][key];
      return h('tr', {}, h('th', { text: `${name} · ${mode === 'dark' ? 'داكن' : 'فاتح'}` }),
        h('td', { dir: 'ltr', text: v.color }), num(v.page), num(v.card), num(v.check));
    })))));
  return h('section', { class: 'admin-card glass brand-badges', id: 'brand-badges', 'aria-labelledby': 'brand-badges-title' },
    h('h2', { id: 'brand-badges-title', text: 'الشارات' }),
    h('p', { class: 'admin-meta', text: 'النجمة الزرقاء (التوثيق والعضوية) والشارة الذهبية (الحساب الرسمي): نفس الشكل (12 فصًّا) ونفس الحجم والمحاذاة بجانب الاسم (18px). الذهبية من 28px فما فوق معدنية بتدرّج ولمعة خفيفة، وأصغر من ذلك مسطحة بلون واحد على شبكة بكسل خاصة بحجمها (16 · 18 · 20 · 24). اضغط الشارة الذهبية بجانب الاسم لترى ما يظهر للمستخدم.' }),
    h('div', { class: 'brand-badges__grid' }, ...BADGE_TONES.map(tonePanel)),
    h('h3', { text: 'التباين المقيس (الحد الأدنى 3:1)' }),
    contrast);
}

function swatch(name, hex, note) {
  const chip = h('span', { class: 'brand-swatch__chip' });
  chip.style.background = hex;
  return h('div', { class: 'brand-swatch' }, chip, h('b', { text: name }), h('code', { dir: 'ltr', text: hex }), note ? h('small', { text: note }) : null);
}

function accentDemo(title, acc, mode, surfaces) {
  const v = acc[mode];
  const box = h('div', { class: 'brand-accent' });
  box.style.background = surfaces[mode].bg;
  box.style.color = mode === 'dark' ? '#eef0f8' : '#1a1c2a';
  const btn = h('span', { class: 'brand-accent__btn', text: 'نشر' });
  btn.style.background = `linear-gradient(135deg, ${v.accent}, ${v['accent-2']})`;
  btn.style.color = v['on-accent'];
  const link = h('span', { class: 'brand-accent__link', text: 'رابط بلون التمييز' });
  link.style.color = v['accent-text'];
  const dot = h('span', { class: 'brand-word__dot' });
  dot.style.background = `linear-gradient(135deg, ${v.accent}, ${v['accent-2']})`;
  const [first, rest] = wordmarkParts();
  const word = h('span', { class: 'brand-word', dir: 'ltr' }, first, dot, rest || null);
  word.style.fontSize = '20px';
  box.append(h('small', { text: `${title} · ${mode === 'dark' ? 'داكن' : 'فاتح'}` }), word, btn, link,
    h('small', { dir: 'ltr', text: `${v.accent} → ${v['accent-2']} · text ${v['on-accent']} (${v.on_contrast}:1)` }));
  return box;
}

export function renderBrand(main) {
  main.replaceChildren(spinner());
  call('GET', '/api/admin/brand').then((d) => {
    const { colors, contrast: k } = d;
    main.replaceChildren(
      h('section', { class: 'admin-card glass brand-intro' },
        h('h2', { text: 'الشعار المعتمد: ثلاث شموع' }),
        h('p', { class: 'admin-meta', text: 'مطبَّق في التطبيق ولوحة القيادة وأيقونات Android وPWA والبريد. كل الأيقونات تُولَّد من رسمتي brand/ بالأمر python brand/build.py.' }),
        h('div', { class: 'brand-swatches' },
          swatch('أسود', colors.black), swatch('أبيض', colors.white),
          swatch('أحمر العلامة', colors.red, `تباين ${k.red_on_black}:1 على الأسود، ${k.red_on_white}:1 على الأبيض`)),
      ),
      badgesCard(d),
      chosenCard(d, colors),
      h('details', { class: 'admin-card glass brand-earlier' },
        h('summary', { text: 'المفاهيم السابقة (للمقارنة)' }),
        ...d.concepts.map((c, i) => conceptCard(c, i, colors))),
      h('section', { class: 'admin-card glass' },
        h('h2', { text: 'اقتراح توحيد ألوان الواجهة (غير مطبَّق)' }),
        h('p', { class: 'admin-meta', text: `لون التمييز الافتراضي الحالي «${d.accent_now.label}» (مرجاني ← وردي) يقترب من الأحمر دون أن يطابقه، فيبدو بجانب نقطة الشعار كلونين متنافسين. الاقتراح: أن يصير الافتراضي «${d.accent_proposed.label}» المبني على أحمر العلامة، مع بقاء الألوان الأخرى (الأزرق والنعناعي…) اختيارًا للمستخدم في «المظهر». يبقى Liquid Glass كما هو: الأحمر يظهر في النقطة والأزرار فقط.` }),
        h('div', { class: 'brand-pair' },
          accentDemo('الحالي', d.accent_now, 'dark', d.surfaces), accentDemo('المقترح', d.accent_proposed, 'dark', d.surfaces)),
        h('div', { class: 'brand-pair' },
          accentDemo('الحالي', d.accent_now, 'light', d.surfaces), accentDemo('المقترح', d.accent_proposed, 'light', d.surfaces)),
        h('p', { class: 'admin-meta', text: `أحمر العلامة على خلفية التطبيق: ${k.red_on_dark_app}:1 داكن، ${k.red_on_light_app}:1 فاتح. لنص الروابط يُفتَّح أو يُغمَّق تلقائيًا إلى 4.5:1 (كما في «المظهر»).` })));
  }).catch((err) => { main.replaceChildren(); handleError(err); });
}
