import { api } from '../api.js';
import { createAntibot } from '../antibot.js';
import { h, wordmark } from '../ui.js';

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
    formSlot.replaceChildren(buildForm());
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

    const form = h('form', { novalidate: true },
      h('div', { class: 'field' }, h('label', { for: 'email', text: 'البريد الإلكتروني' }), email),
      h('div', { class: 'field' }, h('label', { for: 'password', text: 'كلمة المرور' }), password),
      confirm && h('div', { class: 'field' }, h('label', { for: 'password_confirm', text: 'تأكيد كلمة المرور' }), confirm),
      honeypot,
      antibot.el,
      error,
      submit,
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
      h('p', { class: 'auth__foot', text: 'لن يرى أحد بريدك أو أي معلومة عنك. الجميع هنا يظهر باسم dzplay فقط.' }),
    ),
  );
  draw();
  setupGoogle();
}
