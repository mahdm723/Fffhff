// V6 phase 8: appearance — light / dark / system, an accent colour, normal / large text. Saved on the account
// (synced across devices) with a local copy that js/theme-boot.js applies before the first paint.
import { api } from './api.js';
import { h, sheet, toast } from './ui.js';

const KEY = 'dz.appearance';
const MODES = [['system', 'حسب الجهاز'], ['light', 'فاتح'], ['dark', 'داكن']];
const FONTS = [['normal', 'عادي'], ['large', 'كبير']];

export function applyAppearance(a) {
  if (!a) return;
  const r = document.documentElement;
  if (a.mode === 'light' || a.mode === 'dark') r.dataset.theme = a.mode; else delete r.dataset.theme;
  if (a.accent) r.dataset.accent = a.accent; else delete r.dataset.accent;
  if (a.font === 'large') r.dataset.font = 'large'; else delete r.dataset.font;
  try { localStorage.setItem(KEY, JSON.stringify(a)); } catch { /* private mode */ }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(r).getPropertyValue('--bg').trim() || meta.content;
  const app = window.DZPLAYAndroid; // Android 3.0.0+: the system bar icons follow the chosen theme
  if (app && app.setTheme) { try { app.setTheme(r.dataset.theme || 'system'); } catch { /* older app */ } }
}

export function appearanceSheet(me, onSaved) {
  let cur = { ...(me.appearance || { mode: 'system', accent: 'ember', font: 'normal' }) };
  const before = { ...cur };
  let saved = false;
  const choices = (me.appearance_choices || []);
  sheet((panel, close) => {
    const segs = (items, field, label) => h('div', { class: 'seg', role: 'radiogroup', 'aria-label': label },
      ...items.map(([id, text]) => {
        const b = h('button', { type: 'button', class: 'seg__btn', role: 'radio', 'aria-checked': String(cur[field] === id), 'data-id': id }, text);
        b.addEventListener('click', () => { cur[field] = id; paint(); });
        return b;
      }));
    const modeRow = segs(MODES, 'mode', 'الوضع');
    const fontRow = segs(FONTS, 'font', 'حجم الخط');
    const swatches = h('div', { class: 'swatches', role: 'radiogroup', 'aria-label': 'اللون' },
      ...choices.map((c) => {
        const b = h('button', { type: 'button', class: 'swatch', role: 'radio', 'aria-label': c.label, title: c.label, 'data-id': c.id });
        b.style.background = `linear-gradient(135deg, ${c.swatch[0]}, ${c.swatch[1]})`;
        b.addEventListener('click', () => { cur.accent = c.id; paint(); });
        return b;
      }));
    function paint() {
      for (const row of [modeRow, fontRow, swatches]) {
        for (const b of row.querySelectorAll('[data-id]')) {
          const field = row === modeRow ? 'mode' : row === fontRow ? 'font' : 'accent';
          b.setAttribute('aria-checked', String(cur[field] === b.dataset.id));
        }
      }
      applyAppearance(cur); // live preview
    }
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'حفظ');
    save.addEventListener('click', async () => {
      save.disabled = true;
      try {
        const res = await api.put('/api/me/appearance', cur);
        cur = res.appearance;
        saved = true;
        applyAppearance(cur);
        if (onSaved) onSaved(cur);
        toast('حُفظ المظهر ✅');
        close();
      } catch (err) { toast(err.message, 'error'); save.disabled = false; }
    });
    panel.append(
      h('h2', { text: 'المظهر' }),
      h('p', { class: 'muted', text: 'يُحفظ في حسابك ويُطبَّق على كل أجهزتك.' }),
      h('h3', { class: 'sheet__sub', text: 'الوضع' }), modeRow,
      h('h3', { class: 'sheet__sub', text: 'اللون' }), swatches,
      h('h3', { class: 'sheet__sub', text: 'حجم الخط' }), fontRow,
      h('div', { class: 'actions' }, save));
    paint();
  }, () => { if (!saved) applyAppearance(before); }); // closed without saving: back to the saved look
}
