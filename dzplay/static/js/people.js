// People: search by name or DZ-ID (Profile page only), message a person, confirm age.
import { api } from './api.js';
import { icon } from './icons.js';
import * as store from './store.js';
import { autoGrow, h, idChip, nameLine, personAvatar, sheet, toast } from './ui.js';

const ID_RE = /^dz-?[2-9a-hj-np-z]{6}$/i;

/** One-time 18+ confirmation (accounts created before V4). Resolves true when confirmed. */
export function confirmAge() {
  return new Promise((resolve) => {
    let ok = false;
    sheet((panel, close) => {
      const box = h('input', { type: 'checkbox' });
      const go = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, 'تأكيد');
      box.addEventListener('change', () => { go.disabled = !box.checked; });
      go.addEventListener('click', async () => {
        go.disabled = true;
        try {
          await api.post('/api/me/confirm-age', { confirm: true });
          if (store.state.me) store.state.me.age_confirmed = true;
          ok = true;
          close();
        } catch (err) { toast(err.message, 'error'); go.disabled = false; }
      });
      panel.append(
        h('h2', { text: 'تأكيد العمر' }),
        h('p', { text: 'الرسائل المباشرة والمكالمات متاحة للبالغين فقط.' }),
        h('label', { class: 'choice' }, box, h('span', { text: 'أؤكد أن عمري 18 سنة أو أكثر وأوافق على شروط الاستخدام.' })),
        h('div', { class: 'actions' }, go, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')),
      );
    }, () => resolve(ok));
  });
}

/** Open the chat with a person, or write them a first message (a message request). */
export async function messagePerson(publicId, navigate, known = null) {
  let card = known;
  try {
    if (!card || card.can_message === undefined) card = await api.get(`/api/people/${encodeURIComponent(publicId)}`);
  } catch (err) {
    toast(err.status === 404 ? 'هذا الحساب غير متاح.' : err.message, 'error');
    return;
  }
  if (card.conversation_id) { navigate(`#/chat/${card.conversation_id}`); return; }
  if (!card.can_message) { toast('هذا الشخص لا يستقبل رسائل مباشرة.', 'error'); return; }
  if (store.state.me && store.state.me.age_confirmed === false && !(await confirmAge())) return;
  const max = store.state.config.max_message_length;
  sheet((panel, close) => {
    const text = h('textarea', { class: 'input', rows: '3', dir: 'auto', maxlength: String(max), placeholder: 'اكتب أول رسالة…', 'aria-label': 'أول رسالة' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, icon('send'), 'إرسال طلب المراسلة');
    autoGrow(text, 160);
    text.addEventListener('input', () => { go.disabled = !text.value.trim(); });
    go.addEventListener('click', async () => {
      go.disabled = true;
      try {
        const conv = await store.sendDirect(card.public_id, text.value.trim());
        close();
        navigate(`#/chat/${conv.id}`);
      } catch (err) {
        if (err.code === 'age_required' && await confirmAge()) { go.disabled = false; return; }
        toast(err.message, 'error', 4500);
        go.disabled = false;
      }
    });
    panel.append(
      h('div', { class: 'peer-info peer-info--row' }, personAvatar(card.name, { size: 'sm' }), h('div', {}, nameLine(card.name, card.gender), h('small', { dir: 'ltr', text: card.public_id }))),
      h('p', { text: 'ستصله رسالتك كطلب مراسلة. لا تستطيع إرسال أكثر من بضع رسائل حتى يقبل أو يرد.' }),
      h('div', { class: 'field' }, text),
      h('div', { class: 'actions' }, go),
    );
  });
}

/** Search box with results (name, gender, DZ-ID, "مراسلة", "عرض الملف"). */
export function searchBox(navigate) {
  const input = h('input', {
    class: 'input', type: 'search', dir: 'auto', maxlength: '40', enterkeyhint: 'search',
    placeholder: 'اسم أو معرّف مثل DZ-7K4P2M', 'aria-label': 'ابحث عن شخص بالاسم أو المعرّف',
  });
  const go = h('button', { class: 'icon-btn', type: 'submit', 'aria-label': 'بحث' }, icon('search'));
  const results = h('ul', { class: 'people-list', 'aria-live': 'polite' });
  const more = h('button', { class: 'btn btn--ghost btn--block', type: 'button', hidden: true }, 'عرض المزيد');
  const status = h('p', { class: 'people-status' });
  let query = '';
  let page = 0;
  let timer = null;

  function row(p) {
    return h('li', { class: 'person' },
      personAvatar(p.name),
      h('div', { class: 'person__body' }, nameLine(p.name, p.gender, 'person__name'), h('small', { class: 'person__id', dir: 'ltr', text: p.public_id })),
      h('div', { class: 'person__actions' },
        h('button', { class: 'btn btn--primary btn--sm', type: 'button', onclick: () => messagePerson(p.public_id, navigate) }, 'مراسلة'),
        h('button', { class: 'btn btn--ghost btn--sm', type: 'button', onclick: () => navigate(p.profile_ref ? `#/u/${p.profile_ref}` : `#/id/${p.public_id}`) }, 'عرض الملف'),
      ));
  }

  async function run(next = false) {
    const q = input.value.trim();
    if (!next) {
      if (q.length < 2) { results.replaceChildren(); status.textContent = q ? 'اكتب حرفين على الأقل.' : ''; more.hidden = true; return; }
      query = q; page = 0;
    } else page += 1;
    status.textContent = 'جارٍ البحث…';
    try {
      const data = await api.get(`/api/people/search?q=${encodeURIComponent(query)}&page=${page}`);
      if (!next) results.replaceChildren();
      results.append(...data.results.map(row));
      more.hidden = !data.has_more;
      status.textContent = results.children.length ? ''
        : ID_RE.test(query) ? 'لا يوجد حساب متاح بهذا المعرّف.' : 'لا نتائج. الأشخاص باسم dzplay يُعثر عليهم بالمعرّف فقط.';
    } catch (err) {
      status.textContent = err.message;
      more.hidden = true;
    }
  }

  input.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(() => run(), 700); });
  more.addEventListener('click', () => run(true));
  const form = h('form', { class: 'people-search', role: 'search' }, input, go);
  form.addEventListener('submit', (e) => { e.preventDefault(); clearTimeout(timer); run(); });
  return h('section', { class: 'people glass' },
    h('h2', { class: 'people__title' }, icon('plusUser'), 'ابحث عن أشخاص'),
    h('p', { class: 'people__hint', text: 'ابحث بالاسم أو بالمعرّف. لا يظهر البريد أبدًا.' }),
    form, status, results, more);
}

export { idChip };
