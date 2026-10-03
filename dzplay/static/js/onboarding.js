// After sign-in: new Google accounts must answer gender + 18+ (like e-mail registration);
// accounts from before V4 get one gentle, skippable gender question.
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
  if (me.needs_onboarding) { shownThisSession = true; mandatory(onMe, onLogout); }
  else if (me.needs_gender) { shownThisSession = true; gentle(onMe); }
}

function mandatory(onMe, onLogout) {
  sheet((panel, close) => {
    const adult = h('input', { type: 'checkbox' });
    const err = h('div', { class: 'form-error', role: 'alert' });
    const go = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'متابعة');
    go.addEventListener('click', async () => {
      err.textContent = '';
      const gender = picked(panel, 'ob-gender');
      if (!gender) { err.textContent = 'اختر: رجل، أنثى، أو أفضّل عدم الذكر.'; return; }
      if (!adult.checked) { err.textContent = 'يجب أن يكون عمرك 18 سنة أو أكثر لاستخدام DZPLAY.'; return; }
      go.disabled = true;
      try {
        const me = await api.post('/api/me/onboarding', { gender, age_confirmed: true });
        onMe(me);
        close();
      } catch (e) { err.textContent = e.message; go.disabled = false; }
    });
    panel.append(
      h('h2', { text: 'أكمل حسابك' }),
      h('p', { text: 'سؤالان فقط قبل البدء. يمكنك إخفاء الجنس باختيار "أفضّل عدم الذكر".' }),
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
      h('h2', { text: 'جديد في DZPLAY' }),
      h('p', { text: 'أصبح لكل حساب اسم يختاره ومعرّف DZ. هل تريد إضافة جنسك؟ يظهر كأيقونة صغيرة بجانب اسمك، ولا يظهر إن اخترت "أفضّل عدم الذكر".' }),
      genderPicker('g-gender'),
      h('div', { class: 'actions' }, save, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'لاحقًا')),
    );
  }, () => { if (!answered) api.post('/api/me/gender-later').catch(() => {}); });
}
