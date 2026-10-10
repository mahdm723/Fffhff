// After sign-in: new Google accounts must answer name + gender + 18+ (like e-mail registration);
// V6: accounts that never chose a name must choose one (they cannot post or write before);
// accounts from before V4 get one gentle, skippable gender question.
import { appName } from './brand.js';
import { api } from './api.js';
import { h, sheet, toast } from './ui.js';

export const GENDER_CHOICES = [['male', 'رجل'], ['female', 'أنثى'], ['unspecified', 'أفضّل عدم الذكر']];

export function genderPicker(name, selected = null) {
  return h('div', { class: 'gender-pick', role: 'radiogroup', 'aria-label': 'الجنس' },
    ...GENDER_CHOICES.map(([value, label]) => h('label', { class: 'gender-pick__opt' },
      h('input', { type: 'radio', name, value, checked: value === selected }), h('span', { text: label }))));
}

const picked = (panel, name) => panel.querySelector(`input[name="${name}"]:checked`)?.value || null;

let shownThisSession = false;

export function runOnboarding(me, { onMe, onLogout }) {
  if (!me || shownThisSession) return;
  if (me.needs_onboarding) { shownThisSession = true; mandatory(onMe, onLogout, me.name_required); }
  else if (me.name_required) { shownThisSession = true; chooseName(onMe, onLogout); }
  else if (me.needs_gender) { shownThisSession = true; gentle(onMe); }
}

export function nameInput(id = 'display-name') {
  return h('input', { class: 'input', id, dir: 'auto', maxlength: '20', autocomplete: 'nickname', required: true,
    placeholder: 'مثال: Yacine أو أمينة' });
}

function chooseName(onMe, onLogout) {
  sheet((panel, close) => {
    const input = nameInput('ob-name');
    const err = h('div', { class: 'form-error', role: 'alert' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'حفظ الاسم');
    go.addEventListener('click', async () => {
      err.textContent = '';
      const name = input.value.trim().replace(/\s+/g, ' ');
      if (!name) { err.textContent = 'اكتب اسمًا يظهر للآخرين.'; return; }
      go.disabled = true;
      try { onMe(await api.patch('/api/me/profile', { display_name: name })); close(); toast('مرحبًا ' + name + ' 👋'); }
      catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    panel.append(
      h('h2', { text: 'اختر اسمك' }),
      h('p', { text: `أصبح لكل حساب في ${appName()} اسم يظهر مع أفكارك وتعليقاتك ورسائلك. لا يظهر بريدك أبدًا.` }),
      h('div', { class: 'field' }, h('label', { for: 'ob-name', text: 'الاسم الظاهر' }), input,
        h('small', { class: 'field__hint', text: 'من 3 إلى 20 حرفًا: حروف عربية أو لاتينية وأرقام ومسافة و _ فقط.' })),
      err,
      h('div', { class: 'actions' }, go,
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); onLogout(); } }, 'تسجيل الخروج')),
    );
  }, null, { dismissible: false });
}

function mandatory(onMe, onLogout, needsName = false) {
  sheet((panel, close) => {
    const nameField = needsName ? nameInput('ob-name') : null;
    const adult = h('input', { type: 'checkbox' });
    const err = h('div', { class: 'form-error', role: 'alert' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'متابعة');
    go.addEventListener('click', async () => {
      err.textContent = '';
      const gender = picked(panel, 'ob-gender');
      const name = nameField ? nameField.value.trim().replace(/\s+/g, ' ') : null;
      if (nameField && !name) { err.textContent = 'اكتب اسمًا يظهر للآخرين.'; return; }
      if (!gender) { err.textContent = 'اختر: رجل، أنثى، أو أفضّل عدم الذكر.'; return; }
      if (!adult.checked) { err.textContent = `يجب أن يكون عمرك 18 سنة أو أكثر لاستخدام ${appName()}.`; return; }
      go.disabled = true;
      try {
        const me = await api.post('/api/me/onboarding', { gender, age_confirmed: true, ...(name ? { display_name: name } : {}) });
        onMe(me);
        close();
      } catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    panel.append(
      h('h2', { text: 'أكمل حسابك' }),
      h('p', { text: 'أسئلة قليلة قبل البدء. يمكنك إخفاء الجنس باختيار "أفضّل عدم الذكر".' }),
      nameField ? h('div', { class: 'field' }, h('label', { for: 'ob-name', text: 'الاسم الظاهر' }), nameField) : '',
      genderPicker('ob-gender'),
      h('label', { class: 'choice' }, adult, h('span', { text: 'أؤكد أن عمري 18 سنة أو أكثر وأوافق على شروط الاستخدام.' })),
      err,
      h('div', { class: 'actions' }, go,
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); onLogout(); } }, 'تسجيل الخروج')),
    );
  }, null, { dismissible: false });
}

function gentle(onMe) {
  let answered = false;
  sheet((panel, close) => {
    const save = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'حفظ');
    save.addEventListener('click', async () => {
      const gender = picked(panel, 'g-gender');
      if (!gender) { toast('اختر أحد الخيارات أو اضغط "لاحقًا".'); return; }
      save.disabled = true;
      try {
        onMe(await api.patch('/api/me/profile', { gender }));
        answered = true;
        close();
      } catch (e) { toast(e.message, 'error'); save.disabled = false; }
    });
    panel.append(
      h('h2', { text: `جديد في ${appName()}` }),
      h('p', { text: 'أصبح لكل حساب اسم يختاره ومعرّف DZ. هل تريد إضافة جنسك؟ يظهر كأيقونة صغيرة بجانب اسمك، ولا يظهر إن اخترت "أفضّل عدم الذكر".' }),
      genderPicker('g-gender'),
      h('div', { class: 'actions' }, save, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'لاحقًا')),
    );
  }, () => { if (!answered) api.post('/api/me/gender-later').catch(() => {}); });
}
