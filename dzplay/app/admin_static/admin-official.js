// Admin panel — V6 phase 19: «الحساب الرسمي» (gold badge). Post as the official account (text + an optional picture,
// checked like every picture) with your 2FA code; its posts with «تثبيت» / «إلغاء التثبيت» / «حذف». A comment or a
// reply as the official account: «المحتوى» → an idea → «رد رسمي». The bot accepts /post too. Every action is audited.
import { appName } from '/js/brand.js';
import { goldMark, h, toast } from '/js/ui.js';
import { BASE, attempt, call, chip, confirmDanger, emptyState, fmt, sectionHead, spinner, when } from './admin-common.js';

const PIN = [['6', '6 ساعات'], ['24', 'يوم'], ['72', '3 أيام'], ['168', 'أسبوع']];

async function uploadPicture(file, onState) {
  const res = await fetch(`${BASE}/api/admin/official/uploads`, {
    method: 'POST', credentials: 'same-origin', cache: 'no-store', body: file,
    headers: { 'X-DZ-Requested': '1', 'Content-Type': 'application/octet-stream' },
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new Error((data && data.error && data.error.message) || 'تعذّر رفع الصورة.');
  const { id } = data.upload;
  onState('جارٍ فحص الصورة…');
  for (let i = 0; i < 240; i += 1) {
    await new Promise((r) => { setTimeout(r, 500); });
    const st = (await call('GET', `/api/admin/official/uploads/${id}`)).upload;
    if (st.state === 'ready') return id;
    if (st.state === 'rejected' || st.state === 'failed') throw new Error(st.error || 'رُفضت الصورة.');
  }
  throw new Error('انتهت مهلة فحص الصورة.');
}

export function renderOfficial(main) {
  const list = h('div', { class: 'admin-list' }, spinner());
  const meta = h('p', { class: 'admin-meta' });
  const text = h('textarea', { class: 'input', id: 'official-text', rows: '4', dir: 'auto', maxlength: '5000', placeholder: 'نص المنشور' });
  const file = h('input', { class: 'input', id: 'official-file', type: 'file', accept: 'image/jpeg,image/png,image/webp' });
  const code = h('input', {
    class: 'input', id: 'official-code', dir: 'ltr', inputmode: 'numeric', autocomplete: 'one-time-code', maxlength: '6',
    placeholder: '123456', 'aria-label': 'رمز التحقق بخطوتين',
  });
  const status = h('p', { class: 'admin-meta', 'aria-live': 'polite' });
  const publish = h('button', { type: 'button', class: 'btn btn--primary btn--block' }, 'نشر باسم الحساب الرسمي');

  publish.addEventListener('click', async () => {
    if (!text.value.trim() && !file.files.length) { toast('اكتب نصًا أو اختر صورة.', 'error'); return; }
    if (!/^[0-9]{6}$/.test(code.value.trim())) { toast('أدخل رمز التحقق من تطبيق المصادقة.', 'error'); code.focus(); return; }
    publish.disabled = true;
    try {
      let mediaId = null;
      if (file.files.length) {
        status.textContent = 'جارٍ رفع الصورة…';
        mediaId = await uploadPicture(file.files[0], (s) => { status.textContent = s; });
      }
      status.textContent = '';
      if (await attempt(() => call('POST', '/api/admin/official/posts', { content: text.value.trim(), media_id: mediaId, code: code.value.trim() }))) {
        toast('نُشر باسم الحساب الرسمي.');
        text.value = '';
        file.value = '';
        code.value = '';
        load();
      }
    } catch (err) {
      toast(err.message || 'تعذّر رفع الصورة.', 'error');
    }
    status.textContent = '';
    publish.disabled = false;
  });

  function row(p) {
    const hours = h('select', { class: 'input admin-select', 'aria-label': 'مدة التثبيت' }, ...PIN.map(([v, l]) => h('option', { value: v, text: l })));
    hours.value = '24';
    const pin = async (n) => {
      if (await attempt(() => call('POST', `/api/admin/official/posts/${p.id}/pin`, { hours: n }))) { toast(n ? 'ثُبّت في أعلى الأفكار.' : 'أُلغي التثبيت.'); load(); }
    };
    return h('article', { class: 'admin-card glass' },
      h('div', { class: 'admin-line' }, h('b', { text: when(p.created_at) }),
        p.media ? chip('🖼 صورة') : '', p.pinned_until ? chip(`📌 مثبّت حتى ${when(p.pinned_until)}`, 'chip--warn') : ''),
      p.content ? h('p', { class: 'admin-text', dir: 'auto', text: p.content }) : '',
      h('p', { class: 'admin-meta', text: `👍 ${fmt(p.likes)} · 💬 ${fmt(p.comments)}` }),
      h('div', { class: 'admin-actions' },
        ...(p.pinned_until ? [h('button', { type: 'button', class: 'btn btn--sm btn--ghost', onclick: () => pin(0) }, 'إلغاء التثبيت')]
          : [hours, h('button', { type: 'button', class: 'btn btn--sm btn--primary', onclick: () => pin(Number(hours.value)) }, 'تثبيت')]),
        h('button', {
          type: 'button', class: 'btn btn--sm btn--danger',
          onclick: async () => {
            if (!(await confirmDanger('حذف المنشور الرسمي؟', 'يُحذف مع صورته وتعليقاته. لا تراجع.', 'حذف'))) return;
            if (await attempt(() => call('DELETE', `/api/admin/official/posts/${p.id}`))) { toast('حُذف.'); load(); }
          },
        }, 'حذف')));
  }

  async function load() {
    const d = await attempt(() => call('GET', '/api/admin/official'));
    if (!d) return;
    meta.textContent = `المعرّف العام: ${d.account.public_id || '—'} · منشورات آخر 24 ساعة: ${fmt(d.account.posts_today)}`;
    list.replaceChildren(...(d.posts.length ? d.posts.map(row) : [emptyState('لا منشورات بعد.')]));
  }

  main.replaceChildren(
    sectionHead('الحساب الرسمي'),
    h('section', { class: 'admin-group glass admin-official' },
      h('div', { class: 'admin-official__who' }, goldMark({ size: 34, interactive: false }), h('b', { dir: 'ltr', text: appName() })),
      h('p', { class: 'admin-meta' }, 'ينشر باسم المنصة بالشارة الذهبية. لا كلمة مرور له ولا دخول: النشر من هنا برمز التحقق، أو من بوت Telegram بالأمر ',
        h('bdi', { dir: 'ltr', text: '/post' }), '. لا يستقبل رسائل، وخارج العضوية والمكافآت.'),
      meta),
    h('section', { class: 'admin-group glass admin-official' },
      h('h3', { class: 'admin-group__title', text: 'منشور جديد' }),
      text,
      h('label', { class: 'admin-meta', for: 'official-file', text: 'صورة (اختيارية، تُفحص كأي صورة):' }), file,
      h('label', { class: 'admin-meta', for: 'official-code', text: 'رمز التحقق بخطوتين:' }), code,
      status, publish),
    h('h3', { text: 'منشوراته' }),
    list);
  load();
}
