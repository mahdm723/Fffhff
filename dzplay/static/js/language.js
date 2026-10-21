// V6 phase 12: «اللغة» — Arabic, French, English. Saved on the account (all devices) and here; the app reloads
// in the new language and direction at once. Also offered on the sign-in screen (before an account exists).
import { api } from './api.js';
import { LANGS, NAMES, lang, setLang, t } from './i18n.js';
import { h, sheet, toast } from './ui.js';

export async function switchLanguage(code, { signedIn = true } = {}) {
  if (code === lang()) return;
  if (signedIn) await api.put('/api/me/language', { lang: code });
  await setLang(code);
  location.reload();
}

export function languageSheet({ signedIn = true } = {}) {
  sheet((panel, close) => {
    const row = h('div', { class: 'seg seg--stack', role: 'radiogroup', 'aria-label': t('اللغة') },
      ...LANGS.map((code) => {
        const b = h('button', { type: 'button', class: 'seg__btn', role: 'radio', 'aria-checked': String(code === lang()),
          lang: code, dir: code === 'ar' ? 'rtl' : 'ltr', 'data-id': code }, NAMES[code]);
        b.addEventListener('click', async () => {
          if (code === lang()) { close(); return; }
          row.querySelectorAll('button').forEach((x) => { x.disabled = true; });
          try { await switchLanguage(code, { signedIn }); } catch (err) {
            toast(err.message, 'error');
            row.querySelectorAll('button').forEach((x) => { x.disabled = false; });
          }
        });
        return b;
      }));
    panel.append(h('h2', { text: t('اللغة') }), row);
  });
}

/** The account's language wins after sign-in; an account without one takes the language in use. */
export async function syncAccountLanguage(me) {
  if (!me) return false;
  if (me.language && LANGS.includes(me.language)) {
    if (me.language !== lang()) { await setLang(me.language); location.reload(); return true; }
    return false;
  }
  try { await api.put('/api/me/language', { lang: lang() }); } catch { /* next time */ }
  return false;
}
