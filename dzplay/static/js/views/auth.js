import { api } from '../api.js';
import { createAntibot } from '../antibot.js';
import { privacySheet } from '../privacy.js';
import { canOfferAndroidApp, h, wordmark } from '../ui.js';

let gsiLoading = null;
function loadGoogleScript() {
  if (window.google?.accounts?.id) return Promise.resolve();
  if (!gsiLoading) {
    gsiLoading = new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = 'https://accounts.google.com/gsi/client';
      s.async = true;
      s.onload = resolve;
      s.onerror = () => { gsiLoading = null; reject(new Error('gsi')); };
      document.head.append(s);
    });
  }
  return gsiLoading;
}

export function renderAuth(root, { config, onAuthenticated }) {
  let mode = 'login';
  const antibotEnabled = !!config.antibot_enabled;

  const tabs = h('div', { class: 'segmented', role: 'tablist' });
  const formSlot = h('div');
  const googleSlot = h('div', { class: 'google-slot' });

  function tabButton(id, label) {
    return h('button', { type: 'button', role: 'tab', 'aria-selected': String(mode === id), onclick: () => { mode = id; draw(); } }, label);
  }

  function draw() {
    tabs.replaceChildren(tabButton('login', 'تسجيل الدخول'), tabButton('register', 'حساب جديد'));
    tabs.hidden = mode === 'reset';
    formSlot.replaceChildren(mode === 'reset' ? buildReset() : buildForm());
  }

  // ------------------------------------------------------------ forgot password (3 steps, same page)
  function buildReset() {
    const wrap = h('div', { class: 'reset' });
    const back = h('button', { type: 'button', class: 'link-btn reset__back', onclick: () => { mode = 'login'; draw(); } }, 'العودة لتسجيل الدخول');
    const field = (id, label, attrs) => h('div', { class: 'field' }, h('label', { for: id, text: label }),
      h('input', { class: 'input', id, dir: 'ltr', required: true, ...attrs }));
    const fail = (box, err) => {
      box.textContent = err.message || 'تعذّر الاتصال. حاول مرة أخرى.';
      if (err.code === 'rate_limited' && err.retryAfter > 90) box.textContent += ` (حاول بعد ${Math.ceil(err.retryAfter / 60)} دقيقة تقريبًا)`;
    };

    // step 1 — e-mail + anti-bot
    const emailF = field('reset-email', 'البريد الإلكتروني', { type: 'email', autocomplete: 'email', inputmode: 'email', maxlength: '254' });
    const emailIn = emailF.querySelector('input');
    const antibot1 = createAntibot('reset', antibotEnabled);
    const err1 = h('div', { class: 'form-error', role: 'alert' });
    const send1 = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, 'إرسال الطلب');
    const sent = h('div', { class: 'reset__sent glass', role: 'status', hidden: true });
    const step1 = h('form', { novalidate: true, class: 'reset__step' },
      h('h2', { class: 'reset__title', text: 'استعادة الحساب' }),
      h('p', { class: 'reset__lead', text: 'أدخل بريدك. يراجع فريق DZPLAY الطلب، ثم يصلك رمز على بريدك الإلكتروني.' }),
      emailF, antibot1.el, err1, send1);

    // step 2 — the code from the e-mail
    const codeF = field('reset-code', 'الرمز الذي وصلك على البريد', {
      type: 'text', inputmode: 'text', autocomplete: 'one-time-code', autocapitalize: 'characters', spellcheck: 'false', maxlength: '12',
    });
    const err2 = h('div', { class: 'form-error', role: 'alert' });
    const send2 = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, 'تحقق من الرمز');
    const step2 = h('form', { novalidate: true, class: 'reset__step', hidden: true },
      codeF, h('p', { class: 'reset__hint',
        text: `الرمز صالح ${config.reset_code_hours || 24} ساعة ولمرة واحدة. بعد ${config.reset_max_attempts || 5} محاولات خاطئة يُلغى ويلزمك طلب رمز جديد.` }),
      err2, send2);

    // step 3 — new password
    let resetToken = null;
    const pw = field('reset-password', 'كلمة المرور الجديدة', { type: 'password', autocomplete: 'new-password', maxlength: '128' });
    const pw2 = field('reset-password2', 'تأكيد كلمة المرور', { type: 'password', autocomplete: 'new-password', maxlength: '128' });
    const antibot3 = createAntibot('reset', antibotEnabled);
    const err3 = h('div', { class: 'form-error', role: 'alert' });
    const send3 = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, 'تغيير كلمة المرور والدخول');
    const step3 = h('form', { novalidate: true, class: 'reset__step', hidden: true },
      h('h2', { class: 'reset__title', text: 'كلمة مرور جديدة' }),
      h('p', { class: 'reset__lead', text: 'بعد التغيير يُسجَّل خروجك من كل الأجهزة الأخرى.' }),
      pw, pw2, antibot3.el, err3, send3);

    step1.addEventListener('submit', async (e) => {
      e.preventDefault();
      err1.textContent = '';
      if (!emailIn.value.trim()) { err1.textContent = 'أدخل بريدك الإلكتروني.'; return; }
      send1.disabled = true;
      try {
        const solution = antibotEnabled ? await antibot1.ensure() : null;
        const res = await api.post('/api/auth/reset/request', { email: emailIn.value.trim(), antibot: solution });
        sent.textContent = res.message;
        sent.hidden = false;
        step2.hidden = false;
        send1.textContent = 'إعادة إرسال الطلب';
        codeF.querySelector('input').focus();
      } catch (err) {
        fail(err1, err);
      }
      antibot1.reset();
      send1.disabled = false;
    });

    step2.addEventListener('submit', async (e) => {
      e.preventDefault();
      err2.textContent = '';
      const code = codeF.querySelector('input').value.trim();
      if (!code) { err2.textContent = 'أدخل الرمز.'; return; }
      send2.disabled = true;
      try {
        const res = await api.post('/api/auth/reset/verify', { email: emailIn.value.trim(), code });
        resetToken = res.reset_token;
        step1.hidden = true; step2.hidden = true; sent.hidden = true;
        step3.hidden = false;
        pw.querySelector('input').focus();
      } catch (err) {
        fail(err2, err);
      }
      send2.disabled = false;
    });

    step3.addEventListener('submit', async (e) => {
      e.preventDefault();
      err3.textContent = '';
      const a = pw.querySelector('input').value;
      const b = pw2.querySelector('input').value;
      if (a.length < config.password_min_length) { err3.textContent = `كلمة المرور يجب أن تكون ${config.password_min_length} أحرف على الأقل.`; return; }
      if (a !== b) { err3.textContent = 'كلمتا المرور غير متطابقتين.'; return; }
      send3.disabled = true;
      try {
        const solution = antibotEnabled ? await antibot3.ensure() : null;
        const me = await api.post('/api/auth/reset/complete', { reset_token: resetToken, password: a, password_confirm: b, antibot: solution });
        onAuthenticated(me);
      } catch (err) {
        fail(err3, err);
        antibot3.reset();
        send3.disabled = false;
        if (err.code === 'reset_expired') { mode = 'reset'; setTimeout(draw, 2500); }
      }
    });

    wrap.append(step1, sent, step2, step3, back);
    requestAnimationFrame(() => emailIn.focus());
    return wrap;
  }

  function buildForm() {
    const isRegister = mode === 'register';
    const antibot = createAntibot(isRegister ? 'register' : 'login', antibotEnabled);
    const email = h('input', { class: 'input', type: 'email', id: 'email', name: 'email', autocomplete: 'email', dir: 'ltr', required: true, inputmode: 'email', maxlength: '254' });
    const password = h('input', { class: 'input', type: 'password', id: 'password', name: 'password', autocomplete: isRegister ? 'new-password' : 'current-password', dir: 'ltr', required: true, maxlength: '128' });
    const confirm = isRegister ? h('input', { class: 'input', type: 'password', id: 'password_confirm', name: 'password_confirm', autocomplete: 'new-password', dir: 'ltr', required: true, maxlength: '128' }) : null;
    // Honeypot: invisible to people, tempting for form-filling bots.
    const honeypot = h('input', { class: 'hp', type: 'text', name: 'website', tabindex: '-1', autocomplete: 'off', 'aria-hidden': 'true' });
    const error = h('div', { class: 'form-error', role: 'alert' });
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'submit' }, isRegister ? 'إنشاء الحساب' : 'دخول');

    const forgot = !isRegister && config.password_reset_enabled
      ? h('button', { type: 'button', class: 'link-btn auth__forgot', onclick: () => { mode = 'reset'; draw(); } }, 'نسيت كلمة السر؟')
      : null;
    const form = h('form', { novalidate: true },
      h('div', { class: 'field' }, h('label', { for: 'email', text: 'البريد الإلكتروني' }), email),
      h('div', { class: 'field' }, h('label', { for: 'password', text: 'كلمة المرور' }), password),
      confirm && h('div', { class: 'field' }, h('label', { for: 'password_confirm', text: 'تأكيد كلمة المرور' }), confirm),
      honeypot,
      antibot.el,
      error,
      submit,
      forgot,
    );

    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      error.textContent = '';
      if (!email.value.trim() || !password.value) { error.textContent = 'أدخل البريد الإلكتروني وكلمة المرور.'; return; }
      if (isRegister && password.value !== confirm.value) { error.textContent = 'كلمتا المرور غير متطابقتين.'; return; }
      if (isRegister && password.value.length < config.password_min_length) {
        error.textContent = `كلمة المرور يجب أن تكون ${config.password_min_length} أحرف على الأقل.`; return;
      }
      submit.disabled = true;
      const label = submit.textContent;
      try {
        let solution = null;
        if (antibotEnabled) {
          submit.textContent = 'جارٍ التحقق…';
          solution = await antibot.ensure();
        }
        submit.textContent = '…';
        const body = { email: email.value.trim(), password: password.value, antibot: solution };
        const me = isRegister
          ? await api.post('/api/auth/register', { ...body, password_confirm: confirm.value, website: honeypot.value || null })
          : await api.post('/api/auth/login', body);
        onAuthenticated(me);
      } catch (err) {
        error.textContent = err.message || 'تعذّر الاتصال. حاول مرة أخرى.';
        if (err.retryAfter && err.code === 'rate_limited') {
          const mins = Math.ceil(err.retryAfter / 60);
          if (err.retryAfter > 90) error.textContent += ` (حاول بعد ${mins} دقيقة تقريبًا)`;
        }
        antibot.reset(); // every challenge is single-use
        submit.disabled = false;
        submit.textContent = label;
      }
    });
    return form;
  }

  async function setupGoogle() {
    if (!config.google_client_id) return;
    try {
      await loadGoogleScript();
      const { nonce } = await api.get('/api/auth/google/nonce');
      window.google.accounts.id.initialize({
        client_id: config.google_client_id,
        nonce,
        ux_mode: 'popup',
        auto_select: false,
        callback: async ({ credential }) => {
          try {
            const me = await api.post('/api/auth/google', { credential });
            onAuthenticated(me);
          } catch (err) {
            const box = formSlot.querySelector('.form-error');
            if (box) box.textContent = err.message;
            setupGoogle(); // fresh nonce for the next attempt
          }
        },
      });
      googleSlot.replaceChildren();
      window.google.accounts.id.renderButton(googleSlot, {
        theme: matchMedia('(prefers-color-scheme: dark)').matches ? 'filled_black' : 'outline',
        size: 'large', shape: 'pill', text: 'continue_with', locale: 'ar', width: Math.min(360, root.clientWidth - 44),
      });
    } catch {
      googleSlot.replaceChildren(h('p', { class: 'auth__foot', text: 'تعذّر تحميل تسجيل الدخول عبر Google.' }));
    }
  }

  root.replaceChildren(
    h('main', { class: 'auth' },
      h('div', { class: 'auth__brand' },
        wordmark(true),
        h('p', { class: 'auth__tagline', text: 'شارك مشاعرك مع شخص آخر' }),
      ),
      tabs,
      formSlot,
      config.google_client_id ? h('div', { class: 'divider', text: 'أو' }) : null,
      config.google_client_id ? googleSlot : null,
      h('p', { class: 'auth__foot' }, 'لن يرى المستخدمون الآخرون بريدك أو أي معلومة عنك. الجميع هنا يظهر باسم dzplay فقط. ',
        'يطّلع فريق الإدارة على الحسابات والمحتوى للإشراف وحماية المستخدمين. ',
        h('button', { type: 'button', class: 'link-btn', onclick: privacySheet }, 'سياسة الخصوصية')),
      canOfferAndroidApp(config)
        ? h('a', { class: 'btn btn--ghost btn--block app-download', href: config.android_apk_url, download: 'DZPLAY.apk' }, 'تحميل تطبيق أندرويد')
        : null,
    ),
  );
  draw();
  setupGoogle();
}
