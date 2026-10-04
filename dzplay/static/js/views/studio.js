// V5: «استوديو DZPLAY» — a chat-like window where verified creators send short videos to the Reels feed.
// 📎 pick → preview → caption + options → send (progress bubble) → status messages (review → published /
// rejected with the reason). A video leaves this window after a few days (the reel itself stays).
import { api } from '../api.js';
import { icon } from '../icons.js';
import { PickError, chooseFile, openVideo, prepareVideo, uploadBlob, uploadConfig, waitReady } from '../media-pick.js';
import { autoGrow, confirmSheet, formatListTime, h, sheet, toast } from '../ui.js';

const STATUS_TEXT = {
  pending: '⏳ وصل الفيديو، وهو قيد المراجعة. سيصلك الرد هنا.',
  approved: '✅ نُشر الفيديو في Reels.',
  rejected: '❌ لم يُقبل الفيديو.',
  removed: '🗑 حُذف الفيديو بقرار الإشراف.',
};

function seg(options, value, onChange) {
  const wrap = h('div', { class: 'seg', role: 'radiogroup' });
  const paint = (v) => wrap.replaceChildren(...options.map(([id, label, disabled]) => h('button', {
    type: 'button', role: 'radio', class: `seg__opt ${id === v ? 'is-on' : ''}`, 'aria-checked': String(id === v), disabled: !!disabled,
    onclick: () => { onChange(id); paint(id); },
  }, label)));
  paint(value);
  wrap.repaint = paint;
  return wrap;
}

export function renderStudio(page, { navigate }) {
  const thread = h('div', { class: 'studio__thread', 'aria-live': 'polite' });
  const quotaLine = h('p', { class: 'studio__quota muted' });
  const draft = h('div', { class: 'studio__draft', hidden: true });
  const clipBtn = h('button', { class: 'icon-btn glass studio__clip', type: 'button', 'aria-label': 'اختيار فيديو' }, icon('clip'));
  let data = null;
  let picked = null;
  let named = true;
  let onProfile = true;
  let busy = false;
  let timer = null;

  function paintQuota() {
    clearInterval(timer);
    if (!data) return;
    const q = data.quota;
    const tick = () => {
      const left = q.next_at ? Date.parse(q.next_at) - Date.now() : 0;
      if (q.remaining > 0 || left <= 0) {
        quotaLine.textContent = `يمكنك نشر ${q.remaining > 0 ? q.remaining : q.limit} فيديو اليوم · حتى ${data.limits.max_seconds} ثانية`;
        clipBtn.disabled = busy;
        clearInterval(timer);
        return;
      }
      const s = Math.ceil(left / 1000);
      quotaLine.textContent = `الفيديو التالي بعد ${String(Math.floor(s / 3600)).padStart(2, '0')}:${String(Math.floor((s % 3600) / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}`;
      clipBtn.disabled = true;
    };
    tick();
    if (q.remaining <= 0) timer = setInterval(tick, 1000);
  }

  function reelMenu(r) {
    sheet((panel, close) => {
      const act = async (body) => {
        close();
        try { await api.patch(`/api/studio/reels/${encodeURIComponent(r.id)}`, body); load(); } catch (err) { toast(err.message, 'error'); }
      };
      panel.append(h('h2', { text: 'خيارات الفيديو' }), h('div', { class: 'actions' },
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => act({ show_author: !r.show_author }) },
          r.show_author ? 'نشر بدون اسم' : 'إظهار اسمي على الفيديو'),
        r.show_author ? h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => act({ show_on_profile: !r.show_on_profile }) },
          r.show_on_profile ? 'عدم عرضه في ملفي الشخصي' : 'عرضه في ملفي الشخصي') : null,
        h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: async () => {
          close();
          if (!(await confirmSheet({ title: 'حذف الفيديو؟', text: 'سيُحذف من Reels نهائيًا.', confirm: 'حذف', danger: true }))) return;
          try { await api.del(`/api/studio/reels/${encodeURIComponent(r.id)}`); load(); toast('حُذف الفيديو.'); } catch (err) { toast(err.message, 'error'); }
        } }, icon('trash'), 'حذف'),
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
    });
  }

  function bubbleFor(r) {
    const thumb = r.poster
      ? h('button', { class: 'studio__thumb', type: 'button', 'aria-label': 'تشغيل الفيديو', onclick: () => openVideo(r.src, { poster: r.poster }) },
        h('img', { src: r.poster, alt: '' }), h('span', { class: 'studio__play' }, icon('play')))
      : h('div', { class: 'studio__thumb studio__thumb--gone' }, icon('videoOff'));
    const flags = [r.show_author ? 'باسمك' : 'بدون اسم', r.show_author && r.show_on_profile ? 'في ملفك' : null].filter(Boolean).join(' · ');
    const mine = h('div', { class: 'studio__msg studio__msg--mine' }, thumb,
      r.caption ? h('p', { dir: 'auto', text: r.caption }) : null,
      h('small', { class: 'muted', text: `${flags} · ${formatListTime(r.created_at)}` }),
      h('button', { class: 'link-btn', type: 'button', onclick: () => reelMenu(r) }, 'خيارات'));
    const reply = h('div', { class: `studio__msg studio__msg--bot studio__msg--${r.status}` },
      h('p', { text: STATUS_TEXT[r.status] || r.status_label }),
      r.note && r.status !== 'approved' ? h('p', { class: 'muted', dir: 'auto', text: `السبب: ${r.note}` }) : null,
      r.status === 'approved' ? h('small', { class: 'muted', text: `👍 ${r.likes} · 👁 ${r.views} · 💬 ${r.comments}` }) : null);
    return [mine, reply];
  }

  function drawThread() {
    const intro = h('div', { class: 'studio__msg studio__msg--bot' },
      h('p', { text: `مرحبًا في استوديو DZPLAY. أرسل فيديو قصيرًا (${data.limits.min_seconds}–${data.limits.max_seconds} ثانية).` }),
      h('p', { class: 'muted', text: data.limits.approval
        ? 'يراجع الفريق كل فيديو قبل نشره في Reels. يبقى الفيديو في هذه النافذة بضعة أيام ثم يختفي منها (يبقى منشورًا).'
        : 'يُنشر الفيديو مباشرة في Reels بعد فحصه.' }));
    const items = [...data.reels].reverse().flatMap(bubbleFor);
    thread.replaceChildren(intro, ...items);
    thread.scrollTop = thread.scrollHeight;
  }

  async function load() {
    try { data = await api.get('/api/studio'); } catch (err) {
      thread.replaceChildren(h('div', { class: 'studio__msg studio__msg--bot' }, h('p', { text: err.message })));
      clipBtn.disabled = true;
      return;
    }
    drawThread();
    paintQuota();
  }

  function clearDraft() {
    if (picked && picked.preview) URL.revokeObjectURL(picked.preview);
    picked = null;
    draft.hidden = true;
    draft.replaceChildren();
    bar.hidden = false;
  }

  function showDraft(stage = '') {
    const caption = h('textarea', { class: 'input', id: 'studio-caption', rows: '2', maxlength: '2000', dir: 'auto', placeholder: 'وصف الفيديو (اختياري)' });
    caption.value = draft.dataset.caption || '';
    caption.addEventListener('input', () => { draft.dataset.caption = caption.value; });
    autoGrow(caption, 140);
    const profileSeg = seg([['yes', 'عرضه في ملفي الشخصي', !named], ['no', 'عدم عرضه', false]], named && onProfile ? 'yes' : 'no',
      (v) => { onProfile = v === 'yes'; });
    const nameSeg = seg([['named', 'إظهار اسمي على الفيديو'], ['anon', 'نشر بدون اسم']], named ? 'named' : 'anon', (v) => {
      named = v === 'named';
      if (!named) onProfile = false;
      profileSeg.repaint(named && onProfile ? 'yes' : 'no');
      showDraft(stage);
    });
    const send = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: busy }, icon('send'), 'إرسال');
    send.addEventListener('click', () => submit(caption.value.trim()));
    draft.replaceChildren(...[
      h('div', { class: 'studio__preview' },
        picked.preview
          ? h('video', { src: picked.preview, muted: true, playsinline: true, controls: true, preload: 'metadata' })
          : h('div', { class: 'idea-media__ph' }, icon('video'), h('span', { text: 'لا يمكن معاينة هذا الفيديو هنا، لكن يمكنك إرساله.' })),
        h('button', { class: 'idea-media__x', type: 'button', 'aria-label': 'إلغاء', disabled: busy, onclick: clearDraft }, icon('close'))),
      stage ? h('p', { class: 'studio__stage', text: stage }) : null,
      busy ? null : caption, busy ? null : nameSeg, busy ? null : profileSeg, busy ? null : send,
    ].filter(Boolean)); // replaceChildren would print "null"
    draft.hidden = false;
    bar.hidden = true; // the 📎 bar would cover the send button
  }

  clipBtn.addEventListener('click', async () => {
    const file = await chooseFile('video/mp4,video/quicktime,video/webm');
    if (!file) return;
    try {
      const cfg = await uploadConfig();
      clearDraft();
      busy = true;
      clipBtn.disabled = true;
      picked = await prepareVideo(file, cfg);
      busy = false;
      draft.dataset.caption = '';
      showDraft();
    } catch (err) {
      busy = false;
      clearDraft();
      toast(err instanceof PickError ? err.message : (err.message || 'تعذّر تجهيز الفيديو.'), 'error', 4500);
    }
    paintQuota();
  });

  async function submit(caption) {
    if (!picked || busy) return;
    busy = true;
    try {
      await api.post('/api/uploads/precheck', { purpose: 'reel', caption: caption || null });
      showDraft('جارٍ الرفع 0%');
      const up = await uploadBlob(picked.blob, { purpose: 'reel', onProgress: (f) => showDraft(f < 1 ? `جارٍ الرفع ${Math.round(f * 100)}%` : 'جارٍ تجهيز الفيديو وفحصه على الخادم…') });
      showDraft('جارٍ تجهيز الفيديو وفحصه على الخادم…');
      await waitReady(up.id);
      await api.post('/api/studio/reels', { media_id: up.id, caption: caption || null, show_author: named, show_on_profile: named && onProfile });
      busy = false;
      clearDraft();
      load();
    } catch (err) {
      busy = false;
      showDraft();
      toast(err instanceof PickError ? err.message : (err.message || 'تعذّر الإرسال.'), 'error', 5000);
    }
  }

  const bar = h('div', { class: 'studio__bar', 'data-kb-block': '' }, clipBtn, h('span', { class: 'muted', text: 'اضغط 📎 لاختيار فيديو من هاتفك' }));
  page.replaceChildren(
    h('header', { class: 'topbar topbar--back' },
      h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'رجوع', onclick: () => navigate('#/profile') }, icon('back')),
      h('h1', { class: 'page-title', text: 'استوديو DZPLAY' })),
    quotaLine,
    thread,
    draft,
    bar,
  );
  load();
  const onStudio = () => load();
  document.addEventListener('dz:studio', onStudio);
  return () => { clearInterval(timer); clearDraft(); document.removeEventListener('dz:studio', onStudio); };
}
