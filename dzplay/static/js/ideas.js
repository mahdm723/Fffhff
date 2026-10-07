// Public ideas: post cards, reactions, private comments (author-only) and reports.
// Post text is only ever inserted with textContent.
import { api } from './api.js';
import { icon } from './icons.js';
import { openViewer } from './media-pick.js';
import * as store from './store.js';
import { REPORT_REASONS, autoGrow, confirmSheet, formatListTime, h, nameLine, personAvatar, sheet, toast } from './ui.js';

const CLAMP_CHARS = 420;

// ------------------------------------------------------------------ helpers
const ideaEvents = new EventTarget();
/** Notifies views that a post changed elsewhere (reaction, deletion, comments seen). */
export function onIdeaChange(fn) {
  const handler = (e) => fn(e.detail);
  ideaEvents.addEventListener('change', handler);
  return () => ideaEvents.removeEventListener('change', handler);
}
function emitChange(detail) { ideaEvents.dispatchEvent(new CustomEvent('change', { detail })); }

function count(n) {
  return n >= 10000 ? `${Math.round(n / 1000)}k` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n);
}

// ------------------------------------------------------------------ card
export function postCard(post, { navigate, onRemoved } = {}) {
  let state = { ...post };
  const card = h('article', { class: 'post-card glass', 'data-post': post.id });

  const authorBtn = h('button', {
    class: 'post-card__author', type: 'button', 'aria-label': `عرض ملف ${state.author.name}`,
    onclick: () => navigate && navigate(state.mine ? '#/profile' : `#/u/${state.author.ref}`),
  },
  personAvatar(state.author.name, { size: 'sm' }),
  h('span', { class: 'post-card__who' },
    nameLine(state.author.name, state.author.gender, 'post-card__name', state.author.verified),
    h('span', { class: 'post-card__time', text: formatListTime(state.created_at) }),
  ));
  const moreBtn = h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'خيارات المنشور' }, icon('more'));
  moreBtn.addEventListener('click', () => postMenu());

  const body = h('p', { class: 'post-card__body', text: state.content });
  const long = state.content.length > CLAMP_CHARS || state.content.split('\n').length > 9;
  let expandBtn = null;
  if (long) {
    body.classList.add('is-clamped');
    expandBtn = h('button', { class: 'post-card__expand', type: 'button' }, 'اقرأ المزيد');
    expandBtn.addEventListener('click', () => { body.classList.remove('is-clamped'); expandBtn.remove(); });
  }

  const likeBtn = h('button', { class: 'react react--like', type: 'button' });
  const dislikeBtn = h('button', { class: 'react react--dislike', type: 'button' });
  const commentBtn = h('button', { class: 'react react--comment', type: 'button' });
  likeBtn.addEventListener('click', () => toggle('like'));
  dislikeBtn.addEventListener('click', () => toggle('dislike'));
  commentBtn.addEventListener('click', () => (state.mine ? ownerComments() : writeComment()));

  function paint() {
    likeBtn.replaceChildren(icon('thumbUp'), h('span', { text: count(state.likes) }), h('span', { class: 'sr-only', text: 'إعجاب' }));
    dislikeBtn.replaceChildren(icon('thumbDown'), h('span', { text: count(state.dislikes) }), h('span', { class: 'sr-only', text: 'عدم إعجاب' }));
    likeBtn.setAttribute('aria-pressed', String(state.my_reaction === 'like'));
    dislikeBtn.setAttribute('aria-pressed', String(state.my_reaction === 'dislike'));
    likeBtn.disabled = dislikeBtn.disabled = !state.can_react;
    if (state.mine) {
      const c = state.comments || { count: 0, unseen: 0 };
      commentBtn.replaceChildren(...[icon('comment'), h('span', { text: 'التعليقات' }),
        c.count ? h('span', { class: `chip ${c.unseen ? 'chip--hot' : ''}`, text: c.unseen ? `${c.unseen} جديد` : String(c.count) }) : null]
        .filter(Boolean)); // replaceChildren would print "null"
      commentBtn.setAttribute('aria-label', `التعليقات على منشورك${c.count ? `: ${c.count}` : ''}`);
    } else {
      commentBtn.replaceChildren(icon('comment'), h('span', { text: 'تعليق خاص' }));
    }
  }

  async function toggle(kind) {
    if (!state.can_react) return;
    const prev = { likes: state.likes, dislikes: state.dislikes, my_reaction: state.my_reaction };
    const next = state.my_reaction === kind ? null : kind;
    // optimistic update
    if (prev.my_reaction === 'like') state.likes -= 1;
    if (prev.my_reaction === 'dislike') state.dislikes -= 1;
    if (next === 'like') state.likes += 1;
    if (next === 'dislike') state.dislikes += 1;
    state.my_reaction = next;
    paint();
    (next === 'like' ? likeBtn : dislikeBtn).classList.add('pop');
    setTimeout(() => { likeBtn.classList.remove('pop'); dislikeBtn.classList.remove('pop'); }, 300);
    try {
      const res = await api.put(`/api/posts/${encodeURIComponent(state.id)}/reaction`, { reaction: next });
      Object.assign(state, res);
    } catch (err) {
      Object.assign(state, prev);
      toast(err.message, 'error');
    }
    paint();
    emitChange({ id: state.id, post: { ...state } });
  }

  function writeComment() {
    sheet((panel, close) => {
      const max = store.state.config.max_comment_length;
      const ta = h('textarea', { class: 'input', rows: '4', maxlength: String(max + 50), placeholder: 'اكتب تعليقك لصاحب الفكرة…', 'aria-label': 'تعليقك' });
      const send = h('button', { class: 'btn btn--primary btn--block', type: 'button', disabled: true }, icon('send'), 'إرسال التعليق');
      ta.addEventListener('input', () => { send.disabled = !ta.value.trim() || ta.value.length > max; });
      autoGrow(ta, 220);
      send.addEventListener('click', async () => {
        send.disabled = true;
        try {
          await api.post(`/api/posts/${encodeURIComponent(state.id)}/comments`, { content: ta.value.trim() });
          close();
          toast('أُرسل تعليقك إلى صاحب الفكرة فقط.');
        } catch (err) { toast(err.message, 'error'); send.disabled = false; }
      });
      panel.append(
        h('h2', { text: 'تعليق خاص' }),
        h('p', { class: 'sheet__note' }, icon('eyeOff'), 'لن يرى تعليقك أحد غير صاحب الفكرة.'),
        h('div', { class: 'field' }, ta),
        h('div', { class: 'actions' }, send),
      );
    });
  }

  function ownerComments() {
    sheet(async (panel, close) => {
      const list = h('ul', { class: 'comment-list' }, h('li', { class: 'comment-list__empty', text: 'جارٍ التحميل…' }));
      panel.classList.add('sheet--tall');
      panel.append(
        h('h2', { text: 'التعليقات على منشورك' }),
        h('p', { class: 'sheet__note' }, icon('lock'), 'هذه التعليقات خاصة بك. لا يراها أحد غيرك.'),
        list,
        h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إغلاق')),
      );
      try {
        const data = await api.get(`/api/posts/${encodeURIComponent(state.id)}/comments`);
        const seen = state.comments ? state.comments.unseen : 0;
        state.comments = { count: data.comments.length, unseen: 0 };
        paint();
        if (seen) {
          emitChange({ id: state.id, post: { ...state }, seenComments: seen });
          if (store.state.me) store.state.me.unseen_comments = Math.max(0, (store.state.me.unseen_comments || 0) - seen);
          document.dispatchEvent(new CustomEvent('dz:badges'));
        }
        if (!data.comments.length) {
          list.replaceChildren(h('li', { class: 'comment-list__empty', text: 'لا توجد تعليقات بعد. ستظهر هنا عندما يكتب لك أحدهم.' }));
          return;
        }
        list.replaceChildren(...data.comments.map((c) => commentItem(c, list)));
      } catch (err) {
        list.replaceChildren(h('li', { class: 'comment-list__empty', text: err.message }));
      }
    });
  }

  function commentItem(c, list) {
    const li = h('li', { class: 'comment' },
      personAvatar(c.author, { size: 'sm' }),
      h('div', { class: 'comment__main' },
        h('div', { class: 'comment__head' },
          nameLine(c.author, null, 'comment__name'),
          h('span', { class: 'comment__time', text: formatListTime(c.created_at) }),
        ),
        h('p', { class: 'comment__body', text: c.content }),
      ),
    );
    const menu = h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'خيارات التعليق' }, icon('more'));
    menu.addEventListener('click', () => commentMenu(c, li, list));
    li.append(menu);
    return li;
  }

  function commentMenu(c, li, list) {
    sheet((panel, close) => {
      panel.append(
        h('h2', { text: 'خيارات التعليق' }),
        h('div', { class: 'actions' },
          h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); reportSheet({ commentId: c.id }); } }, icon('flag'), 'الإبلاغ عن التعليق'),
          h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: async () => {
            close();
            const ok = await confirmSheet({ title: 'حظر صاحب التعليق؟', text: 'لن يستطيع التعليق على أفكارك أو مراسلتك.', confirm: 'حظر', danger: true });
            if (!ok) return;
            try { await api.post(`/api/comments/${encodeURIComponent(c.id)}/block`); toast('تم الحظر.'); } catch (err) { toast(err.message, 'error'); }
          } }, icon('block'), 'حظر صاحب التعليق'),
          h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: async () => {
            close();
            try {
              await api.del(`/api/comments/${encodeURIComponent(c.id)}`);
              li.remove();
              state.comments = { count: Math.max(0, (state.comments?.count || 1) - 1), unseen: 0 };
              paint();
              if (!list.children.length) list.append(h('li', { class: 'comment-list__empty', text: 'لا توجد تعليقات.' }));
            } catch (err) { toast(err.message, 'error'); }
          } }, icon('trash'), 'حذف التعليق'),
        ),
      );
    });
  }

  function postMenu() {
    sheet((panel, close) => {
      const items = state.mine
        ? [h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: async () => {
          close();
          const ok = await confirmSheet({ title: 'حذف هذه الفكرة؟', text: 'سيُحذف المنشور وكل تفاعلاته وتعليقاته نهائيًا.', confirm: 'حذف', danger: true });
          if (!ok) return;
          try {
            await api.del(`/api/posts/${encodeURIComponent(state.id)}`);
            card.remove();
            emitChange({ id: state.id, removed: true });
            if (onRemoved) onRemoved(state.id);
            toast('حُذفت الفكرة.');
          } catch (err) { toast(err.message, 'error'); }
        } }, icon('trash'), 'حذف المنشور')]
        : [h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); reportSheet({ postId: state.id }); } }, icon('flag'), 'الإبلاغ عن المنشور')];
      panel.append(h('h2', { text: 'خيارات المنشور' }), h('div', { class: 'actions' }, ...items,
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
    });
  }

  // V5: one picture (signed URL bound to this session; full screen on tap)
  let media = null;
  if (state.media && state.media.url) {
    const m = state.media;
    const img = h('img', { src: m.url, alt: 'صورة مرفقة بالفكرة', loading: 'lazy', decoding: 'async',
      width: m.width || null, height: m.height || null });
    media = h('button', { class: 'post-card__media', type: 'button', 'aria-label': 'عرض الصورة بملء الشاشة',
      onclick: () => openViewer(m.url, { alt: 'صورة مرفقة بالفكرة' }) }, img);
  }
  const review = state.status === 'pending' ? 'بانتظار مراجعة الصورة — لا يراها غيرك الآن'
    : state.status === 'hidden' ? 'مخفية مؤقتًا للمراجعة بعد بلاغات' : null;

  paint();
  if (!state.content) body.hidden = true;
  card.append(...[
    h('header', { class: 'post-card__head' }, authorBtn, moreBtn),
    review ? h('p', { class: 'post-card__review' }, icon('clock'), review) : null,
    body,
    expandBtn,
    media,
    h('footer', { class: 'post-card__actions' }, likeBtn, dislikeBtn, h('span', { class: 'post-card__spacer' }), commentBtn),
  ].filter(Boolean));
  card.update = (p) => { state = { ...state, ...p }; paint(); };
  return card;
}

export function reportSheet({ postId = null, commentId = null }) {
  sheet((panel, close) => {
    const details = h('textarea', { class: 'input', rows: '3', maxlength: '500', placeholder: 'تفاصيل إضافية (اختياري)' });
    const choices = REPORT_REASONS.map(([value, label], i) =>
      h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'reason', value, checked: i === 0 }), h('span', { text: label })));
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال البلاغ');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      const reason = panel.querySelector('input[name="reason"]:checked')?.value;
      const path = commentId ? `/api/comments/${encodeURIComponent(commentId)}/report` : `/api/posts/${encodeURIComponent(postId)}/report`;
      try {
        await api.post(path, { reason, details: details.value.trim() || null });
        close();
        toast('شكرًا لك. تم إرسال البلاغ وسيُراجع.');
      } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
    });
    panel.append(
      h('h2', { text: commentId ? 'الإبلاغ عن تعليق' : 'الإبلاغ عن منشور' }),
      h('p', { text: 'البلاغات سرية. نحتفظ فقط بالنص المُبلّغ عنه لمراجعته.' }),
      ...choices,
      h('div', { class: 'field' }, details),
      h('div', { class: 'actions' }, submit),
    );
  });
}

/** Infinite list helper: calls `load()` when the sentinel scrolls into view. */
export function infiniteSentinel(load) {
  const el = h('div', { class: 'feed-sentinel', 'aria-hidden': 'true' });
  const io = new IntersectionObserver((entries) => { if (entries.some((e) => e.isIntersecting)) load(); }, { rootMargin: '600px 0px' });
  io.observe(el);
  el.stop = () => io.disconnect();
  return el;
}

/** A person's posts (newest first) with "load more". Used by own profile and public previews. */
export function profilePosts(ref, { navigate, emptyText }) {
  const wrap = h('div', { class: 'feed' });
  const more = h('button', { class: 'btn btn--ghost btn--block', type: 'button', hidden: true }, 'عرض المزيد');
  let before = null;
  async function load() {
    more.disabled = true;
    try {
      const qs = before ? `?before=${encodeURIComponent(before)}` : '';
      const data = await api.get(`/api/profiles/${encodeURIComponent(ref)}/posts${qs}`);
      if (!before && !data.posts.length) wrap.append(h('p', { class: 'feed-end', text: emptyText }));
      for (const p of data.posts) wrap.append(postCard(p, { navigate }));
      before = data.next_before;
      more.hidden = !before;
    } catch (err) {
      toast(err.message, 'error');
    }
    more.disabled = false;
  }
  more.addEventListener('click', load);
  load();
  return h('div', {}, wrap, more);
}
