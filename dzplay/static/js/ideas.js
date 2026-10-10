// Public ideas: post cards, reactions, public comments with replies (V6), likers and reports.
// Post text is only ever inserted with textContent.
import { appName } from './brand.js';
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
  personAvatar(state.author.name, { size: 'sm', url: state.author.avatar_url }),
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
  commentBtn.addEventListener('click', () => openComments());
  const likersBtn = h('button', { class: 'post-card__likers', type: 'button' });
  likersBtn.addEventListener('click', () => openLikers());

  function paint() {
    likeBtn.replaceChildren(icon('thumbUp'), h('span', { text: count(state.likes) }), h('span', { class: 'sr-only', text: 'إعجاب' }));
    dislikeBtn.replaceChildren(icon('thumbDown'), h('span', { text: count(state.dislikes) }), h('span', { class: 'sr-only', text: 'عدم إعجاب' }));
    likeBtn.setAttribute('aria-pressed', String(state.my_reaction === 'like'));
    dislikeBtn.setAttribute('aria-pressed', String(state.my_reaction === 'dislike'));
    likeBtn.disabled = dislikeBtn.disabled = !state.can_react;
    const c = state.comments || { count: 0, unseen: 0 };
    commentBtn.replaceChildren(...[icon('comment'), h('span', { text: c.count ? `تعليقات (${count(c.count)})` : 'تعليق' }),
      state.mine && c.unseen ? h('span', { class: 'chip chip--hot', text: `${c.unseen} جديد` }) : null]
      .filter(Boolean)); // replaceChildren would print "null"
    commentBtn.setAttribute('aria-label', `التعليقات${c.count ? `: ${c.count}` : ''}`);
    likersBtn.hidden = !state.likes;
    likersBtn.textContent = state.likes === 1 ? 'أعجب شخصًا واحدًا' : `أعجب ${count(state.likes)} أشخاص`;
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

  // V6 phase 4: public comments, one level of replies, newest threads at the bottom (chat-like reading order).
  function openComments(focusId = null) {
    sheet(async (panel, close) => {
      const max = store.state.config.max_comment_length;
      const list = h('ul', { class: 'comment-list' }, h('li', { class: 'comment-list__empty', text: 'جارٍ التحميل…' }));
      const more = h('button', { class: 'btn btn--ghost btn--block', type: 'button', hidden: true }, 'تعليقات أقدم…');
      const replyNote = h('div', { class: 'comment-reply', hidden: true });
      const ta = h('textarea', { class: 'input', rows: '1', maxlength: String(max + 50), placeholder: 'اكتب تعليقًا…', 'aria-label': 'تعليقك' });
      const send = h('button', { class: 'icon-btn send-btn', type: 'button', disabled: true, 'aria-label': 'إرسال التعليق' }, icon('send'));
      let replyTo = null;
      let next = null;
      ta.addEventListener('input', () => { send.disabled = !ta.value.trim() || ta.value.length > max; });
      autoGrow(ta, 160);
      panel.classList.add('sheet--tall', 'comments-sheet');

      const go = (hash) => { close(); if (navigate) navigate(hash); };
      function setReply(c) {
        replyTo = c;
        replyNote.hidden = !c;
        if (c) {
          replyNote.replaceChildren(h('span', { text: `ردّ على ${c.author.name}` }),
            h('button', { class: 'link-btn', type: 'button', onclick: () => setReply(null) }, 'إلغاء'));
          ta.focus();
        }
      }
      function item(c, thread) {
        const who = c.author.public_id
          ? h('button', { class: 'comment__who', type: 'button', onclick: () => go(`#/id/${c.author.public_id}`) },
            personAvatar(c.author.name, { size: 'sm', url: c.author.avatar_url }))
          : h('span', { class: 'comment__who' }, personAvatar(c.author.name, { size: 'sm' }));
        const nameEl = c.author.public_id
          ? h('button', { class: 'comment__name-btn', type: 'button', onclick: () => go(`#/id/${c.author.public_id}`) },
            nameLine(c.author.name, c.author.gender, 'comment__name', c.author.verified))
          : nameLine(c.author.name, null, 'comment__name');
        const head = h('div', { class: 'comment__head' }, nameEl,
          c.team ? h('span', { class: 'chip chip--team', text: c.official ? 'رسمي' : `فريق ${appName()}` }) : '',
          h('span', { class: 'comment__time', text: formatListTime(c.created_at) }));
        const actions = h('div', { class: 'comment__actions' },
          c.can_reply ? h('button', { class: 'link-btn', type: 'button', onclick: () => setReply(c) }, 'رد') : '',
          c.private ? h('span', { class: 'comment__private' }, icon('lock'), 'تعليق خاص قديم') : '',
          h('button', { class: 'icon-btn icon-btn--plain comment__more', type: 'button', 'aria-label': 'خيارات التعليق',
            onclick: () => commentMenu(c, li, thread) }, icon('more')));
        const li = h('li', { class: `comment ${c.parent_id ? 'comment--reply' : ''}`, 'data-comment': c.id }, who,
          h('div', { class: 'comment__main' }, head,
            h('p', { class: 'comment__body' },
              c.reply_to && c.parent_id ? h('bdi', { class: 'comment__at', text: `@${c.reply_to}` }) : '', c.reply_to && c.parent_id ? ' ' : '',
              h('bdi', { text: c.content })),
            actions));
        return li;
      }
      function threadEl(c) {
        const replies = h('ul', { class: 'comment-replies' }, ...(c.replies || []).map((r) => item(r, null)));
        const wrap = h('li', { class: 'comment-thread', 'data-thread': c.id });
        wrap.append(item(c, wrap), replies);
        wrap.replies = replies;
        return wrap;
      }
      async function load(first) {
        try {
          const qs = next ? `?before=${encodeURIComponent(next)}` : '';
          const data = await api.get(`/api/posts/${encodeURIComponent(state.id)}/comments${qs}`);
          if (first) list.replaceChildren();
          list.append(...data.comments.map(threadEl));
          next = data.next;
          more.hidden = !data.has_more;
          if (first && state.mine && state.comments && state.comments.unseen) {
            const seen = state.comments.unseen;
            state.comments = { ...state.comments, unseen: 0 };
            paint();
            emitChange({ id: state.id, post: { ...state }, seenComments: seen });
            if (store.state.me) store.state.me.unseen_comments = Math.max(0, (store.state.me.unseen_comments || 0) - seen);
            document.dispatchEvent(new CustomEvent('dz:badges'));
          }
          if (!list.children.length) {
            list.replaceChildren(h('li', { class: 'comment-list__empty', text: 'لا توجد تعليقات بعد. كن أول من يعلّق.' }));
          }
          if (focusId) {
            const el = list.querySelector(`[data-comment="${CSS.escape(focusId)}"]`);
            if (el) { el.classList.add('is-focus'); el.scrollIntoView({ block: 'center' }); }
            focusId = null;
          }
        } catch (err) {
          list.replaceChildren(h('li', { class: 'comment-list__empty', text: err.message }));
        }
      }
      more.addEventListener('click', () => load(false));
      send.addEventListener('click', async () => {
        const content = ta.value.trim();
        if (!content) return;
        send.disabled = true;
        try {
          const { comment } = await api.post(`/api/posts/${encodeURIComponent(state.id)}/comments`,
            { content, parent_id: replyTo ? replyTo.id : null });
          ta.value = '';
          const empty = list.querySelector('.comment-list__empty');
          if (empty) empty.remove();
          if (comment.parent_id) {
            const thread = list.querySelector(`[data-thread="${CSS.escape(comment.parent_id)}"]`);
            if (thread) thread.replies.append(item(comment, null));
          } else list.append(threadEl({ ...comment, replies: [] }));
          setReply(null);
          state.comments = { ...(state.comments || { unseen: 0 }), count: ((state.comments && state.comments.count) || 0) + 1 };
          paint();
          emitChange({ id: state.id, post: { ...state } });
          const el = list.querySelector(`[data-comment="${CSS.escape(comment.id)}"]`);
          if (el) el.scrollIntoView({ block: 'nearest' });
        } catch (err) { toast(err.message, 'error'); }
        send.disabled = !ta.value.trim();
      });
      panel.append(
        h('div', { class: 'comments-sheet__head' }, h('h2', { text: 'التعليقات' }),
          h('button', { class: 'icon-btn icon-btn--plain', type: 'button', 'aria-label': 'إغلاق', onclick: close }, icon('close'))),
        h('div', { class: 'comments-sheet__body' }, list, more),
        h('div', { class: 'comments-sheet__composer' }, replyNote, h('div', { class: 'comment-composer' }, ta, send)));
      load(true);
    });
  }

  function commentMenu(c, li, thread) {
    sheet((panel, close) => {
      const buttons = [];
      if (!c.mine) {
        buttons.push(h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); reportSheet({ commentId: c.id }); } }, icon('flag'), 'الإبلاغ عن التعليق'));
      }
      if (state.mine && !c.mine && !c.team) {
        buttons.push(h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: async () => {
          close();
          const ok = await confirmSheet({ title: 'حظر صاحب التعليق؟', text: 'لن يستطيع التعليق على أفكارك أو مراسلتك.', confirm: 'حظر', danger: true });
          if (!ok) return;
          try { await api.post(`/api/comments/${encodeURIComponent(c.id)}/block`); (thread || li).remove(); toast('تم الحظر.'); } catch (err) { toast(err.message, 'error'); }
        } }, icon('block'), 'حظر صاحب التعليق'));
      }
      if (c.can_delete) {
        buttons.push(h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: async () => {
          close();
          const ok = await confirmSheet({ title: 'حذف التعليق؟', text: 'يختفي عند الجميع.', confirm: 'حذف', danger: true });
          if (!ok) return;
          try {
            await api.del(`/api/comments/${encodeURIComponent(c.id)}`);
            (thread || li).remove();
            state.comments = { ...(state.comments || { unseen: 0 }), count: Math.max(0, ((state.comments && state.comments.count) || 1) - 1) };
            paint();
            emitChange({ id: state.id, post: { ...state } });
          } catch (err) { toast(err.message, 'error'); }
        } }, icon('trash'), 'حذف التعليق'));
      }
      panel.append(h('h2', { text: 'خيارات التعليق' }), h('div', { class: 'actions' }, ...buttons,
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')));
    });
  }

  // V6 phase 4: who liked (never who disliked: that stays a number)
  function openLikers() {
    sheet(async (panel, close) => {
      const list = h('ul', { class: 'likers' }, h('li', { class: 'comment-list__empty', text: 'جارٍ التحميل…' }));
      const more = h('button', { class: 'btn btn--ghost btn--block', type: 'button', hidden: true }, 'المزيد');
      let cursor = null;
      async function load(first) {
        try {
          const d = await api.get(`/api/posts/${encodeURIComponent(state.id)}/likers${cursor ? `?cursor=${cursor}` : ''}`);
          if (first) list.replaceChildren();
          list.append(...d.likers.map((p) => h('li', {}, h('button', { class: 'user-row', type: 'button',
            onclick: () => { close(); if (navigate) navigate(`#/id/${p.public_id}`); } },
          personAvatar(p.name, { url: p.avatar_url }),
          h('span', { class: 'user-row__text' }, nameLine(p.name, p.gender, 'user-row__name', p.verified),
            h('small', { class: 'user-row__id', dir: 'ltr', text: p.public_id }))))));
          cursor = d.next_cursor;
          more.hidden = !cursor;
          if (!list.children.length) list.replaceChildren(h('li', { class: 'comment-list__empty', text: 'لا إعجابات بعد.' }));
        } catch (err) { list.replaceChildren(h('li', { class: 'comment-list__empty', text: err.message })); }
      }
      more.addEventListener('click', () => load(false));
      panel.classList.add('sheet--tall');
      panel.append(h('h2', { text: 'أعجبتهم الفكرة' }), list, more,
        h('div', { class: 'actions' }, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إغلاق')));
      load(true);
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
    likersBtn,
  ].filter(Boolean));
  card.update = (p) => { state = { ...state, ...p }; paint(); };
  card.openComments = (focusId) => openComments(focusId);
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
