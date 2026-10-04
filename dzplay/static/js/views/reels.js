// Reels: full-screen vertical feed of platform videos and photo posts (order chosen by the server).
// Videos autoplay when they come into view and pause when they leave; only the current reel and
// the next few keep their media loaded, the rest is released to save memory.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { feedState, loadMore, prefetchAround, touch } from '../reels-prefetch.js';
import { REPORT_REASONS, formatListTime, h, nameLine, personAvatar, sheet, toast } from '../ui.js';

const MUTE_KEY = 'dz:reels-muted';
const VIEW_AFTER_MS = 1500;
const compact = new Intl.NumberFormat('ar-DZ', { notation: 'compact' });
const count = (n) => (n ? compact.format(n) : '0');
const viewed = new Set(); // reels counted as seen in this app session
let lastIndex = 0; // where the user was, kept between visits to Home

function readMuted() { try { return sessionStorage.getItem(MUTE_KEY) !== '0'; } catch { return true; } }
function writeMuted(v) { try { sessionStorage.setItem(MUTE_KEY, v ? '1' : '0'); } catch { /* ignore */ } }

export function renderReels(pane, { config }) {
  let muted = readMuted();
  let visible = true;
  let active = -1;
  let viewTimer = null;
  const items = []; // { reel, el, video, setSrc, paint }
  const list = h('div', { class: 'reels', 'aria-label': 'Reels' });
  const status = h('div', { class: 'reels__status' });
  pane.replaceChildren(list, status);

  // ------------------------------------------------------------ one reel
  function reelItem(reel, index) {
    const first = reel.media[0] || {};
    const portrait = first.width && first.height ? first.height > first.width : true;
    const el = h('article', { class: `reel reel--${reel.kind} ${portrait ? 'reel--portrait' : ''}`, dataset: { i: String(index) },
      'aria-label': `مقطع ${index + 1}` });
    const feedback = h('div', { class: 'reel__feedback', 'aria-hidden': 'true' });
    let video = null;
    let stage;

    const flash = (name) => {
      feedback.replaceChildren(icon(name));
      feedback.classList.remove('is-on');
      void feedback.offsetWidth; // restart the animation
      feedback.classList.add('is-on');
    };

    if (reel.kind === 'video') {
      video = h('video', { class: 'reel__video', playsinline: true, loop: true, preload: 'none', poster: first.poster, 'aria-label': 'فيديو' });
      video.muted = muted;
      video.setAttribute('webkit-playsinline', '');
      stage = h('div', { class: 'reel__stage', role: 'button', tabindex: '0', 'aria-label': 'تشغيل أو إيقاف' }, video);
      const toggle = () => {
        if (video.paused) { video.play().catch(() => {}); flash('play'); } else { video.pause(); flash('pause'); }
      };
      stage.addEventListener('click', toggle);
      stage.addEventListener('keydown', (e) => { if (e.key === ' ' || e.key === 'Enter') { e.preventDefault(); toggle(); } });
    } else {
      const dots = h('div', { class: 'reel__dots', 'aria-hidden': reel.media.length < 2 ? 'true' : null },
        ...(reel.media.length > 1 ? reel.media.map((_m, i) => h('span', { class: i === 0 ? 'is-on' : '' })) : []));
      const carousel = h('div', { class: 'reel__carousel', tabindex: '0', 'aria-label': `${reel.media.length} صور` },
        ...reel.media.map((m, i) => h('div', { class: 'reel__slide' },
          h('img', { 'data-src': m.src, alt: '', decoding: 'async', draggable: 'false', width: m.width, height: m.height,
            'aria-label': `صورة ${i + 1} من ${reel.media.length}` }))));
      carousel.addEventListener('scroll', () => {
        const w = carousel.clientWidth || 1;
        const n = Math.round(Math.abs(carousel.scrollLeft) / w);
        dots.querySelectorAll('span').forEach((d, i) => d.classList.toggle('is-on', i === n));
      }, { passive: true });
      stage = h('div', { class: 'reel__stage' }, carousel, dots);
    }

    // actions
    const likeN = h('span', { class: 'reel__n' });
    const dislikeN = h('span', { class: 'reel__n' });
    const commentN = h('span', { class: 'reel__n' });
    const likeBtn = h('button', { class: 'reel__act', type: 'button', 'aria-label': 'أعجبني' }, icon('thumbUp'), likeN);
    const dislikeBtn = h('button', { class: 'reel__act', type: 'button', 'aria-label': 'لم يعجبني' }, icon('thumbDown'), dislikeN);
    const commentBtn = h('button', { class: 'reel__act', type: 'button', 'aria-label': 'التعليقات' }, icon('comment'), commentN);
    const muteBtn = video ? h('button', { class: 'reel__act reel__act--mute', type: 'button' }) : null;
    const paint = () => {
      likeN.textContent = count(reel.likes);
      dislikeN.textContent = count(reel.dislikes);
      commentN.textContent = count(reel.comments);
      likeBtn.setAttribute('aria-pressed', String(reel.my_reaction === 'like'));
      dislikeBtn.setAttribute('aria-pressed', String(reel.my_reaction === 'dislike'));
      if (muteBtn) {
        muteBtn.replaceChildren(icon(muted ? 'volumeOff' : 'volume'));
        muteBtn.setAttribute('aria-label', muted ? 'تشغيل الصوت' : 'كتم الصوت');
      }
    };
    likeBtn.addEventListener('click', () => react(reel, 'like', paint));
    dislikeBtn.addEventListener('click', () => react(reel, 'dislike', paint));
    commentBtn.addEventListener('click', () => openComments(reel, config, paint));
    if (muteBtn) muteBtn.addEventListener('click', () => setMuted(!muted));

    // caption: two lines, then "المزيد" / "أقل"
    let cap = null;
    if (reel.caption) {
      const text = h('p', { class: 'reel__text', dir: 'auto', text: reel.caption });
      const more = h('button', { class: 'reel__more', type: 'button', hidden: true, 'aria-expanded': 'false' }, 'المزيد');
      cap = h('div', { class: 'reel__caption' }, text, more);
      more.addEventListener('click', (e) => {
        e.stopPropagation();
        const open = !cap.classList.contains('is-open');
        cap.classList.toggle('is-open', open);
        more.textContent = open ? 'أقل' : 'المزيد';
        more.setAttribute('aria-expanded', String(open));
        if (!open) text.scrollTop = 0;
      });
      requestAnimationFrame(() => { more.hidden = text.scrollHeight <= text.clientHeight + 2; });
    }

    // V5: a creator reel published with the name: name + star, linked to the profile
    const author = reel.author ? h('button', { class: 'reel__author', type: 'button', 'aria-label': `ملف ${reel.author.name}`,
      onclick: (e) => { e.stopPropagation(); location.hash = `#/u/${reel.author.ref}`; } },
    nameLine(reel.author.name, reel.author.gender, '', reel.author.verified)) : null;
    const reportBtn = reel.reportable ? h('button', { class: 'reel__act reel__act--mute', type: 'button', 'aria-label': 'إبلاغ',
      onclick: (e) => { e.stopPropagation(); reportReel(reel); } }, icon('flag')) : null;
    const bottom = author || cap ? h('div', { class: 'reel__bottom' }, ...[author, cap].filter(Boolean)) : null;
    el.append(...[stage, h('div', { class: 'reel__shade', 'aria-hidden': 'true' }), feedback,
      h('div', { class: 'reel__side' }, ...[likeBtn, dislikeBtn, commentBtn, muteBtn, reportBtn].filter(Boolean)), bottom].filter(Boolean));
    paint();

    const setSrc = (on) => {
      if (video) {
        if (on && !video.getAttribute('src')) { video.src = first.src; video.preload = 'auto'; touch(first.src); }
        if (!on && video.getAttribute('src')) { video.pause(); video.removeAttribute('src'); video.load(); }
      } else {
        el.querySelectorAll('img[data-src]').forEach((img) => {
          if (on && !img.getAttribute('src')) { img.src = img.dataset.src; touch(img.dataset.src); }
        });
      }
    };
    return { reel, el, video, setSrc, paint };
  }

  // ------------------------------------------------------------ playback window
  function setMuted(v) {
    muted = v;
    writeMuted(v);
    for (const it of items) { if (it.video) it.video.muted = v; it.paint(); }
  }

  function play(it) {
    if (!it || !it.video || !visible || document.hidden) return;
    it.video.muted = muted;
    const p = it.video.play();
    if (p) p.catch(() => { if (!muted) { setMuted(true); it.video.play().catch(() => {}); } });
  }

  function activate(i) {
    if (i === active || !items[i]) return;
    active = i;
    lastIndex = i;
    const ahead = feedState().settings.ahead;
    items.forEach((it, j) => {
      const near = j >= i - 1 && j <= i + ahead;
      it.setSrc(near);
      if (j !== i && it.video) it.video.pause();
    });
    play(items[i]);
    prefetchAround(i);
    clearTimeout(viewTimer);
    const reel = items[i].reel;
    viewTimer = setTimeout(() => {
      if (viewed.has(reel.id) || !visible) return;
      viewed.add(reel.id);
      api.post(`/api/reels/${encodeURIComponent(reel.id)}/view`).catch(() => {});
    }, VIEW_AFTER_MS);
    if (i >= items.length - 3) more();
  }

  const io = new IntersectionObserver((entries) => {
    for (const e of entries) {
      if (e.isIntersecting && e.intersectionRatio >= 0.6) activate(Number(e.target.dataset.i));
    }
  }, { root: list, threshold: [0.6] });

  function append(reels) {
    for (const reel of reels) {
      const it = reelItem(reel, items.length);
      items.push(it);
      list.append(it.el);
      io.observe(it.el);
    }
  }

  function showEmpty() {
    status.replaceChildren(h('div', { class: 'reels__empty' },
      icon('reels'), h('h2', { text: 'لا توجد مقاطع بعد' }), h('p', { text: 'سيظهر هنا محتوى DZPLAY قريبًا. اسحب لتصفح الأفكار.' })));
  }

  let loadingMore = false;
  async function more() {
    if (loadingMore) return;
    loadingMore = true;
    try {
      const fresh = await loadMore();
      append(fresh);
      if (!items.length) showEmpty();
    } catch (err) {
      if (!items.length) {
        status.replaceChildren(h('div', { class: 'reels__empty' }, h('p', { text: err.message }),
          h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => { status.replaceChildren(); more(); } }, 'إعادة المحاولة')));
      }
    } finally {
      loadingMore = false;
    }
  }

  // ------------------------------------------------------------ start
  const f = feedState();
  if (f.reels.length) {
    append(f.reels);
    const start = Math.min(lastIndex, items.length - 1);
    requestAnimationFrame(() => {
      if (start > 0) items[start].el.scrollIntoView({ block: 'start' });
      activate(start);
    });
  } else {
    status.replaceChildren(h('div', { class: 'reels__loading' }, h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' })));
    more().then(() => { if (items.length) { status.replaceChildren(); activate(0); } });
  }

  const onVisibility = () => {
    const it = items[active];
    if (!it || !it.video) return;
    if (document.hidden) it.video.pause(); else play(it);
  };
  document.addEventListener('visibilitychange', onVisibility);

  return {
    setVisible(v) {
      visible = v;
      const it = items[active];
      if (it && it.video) { if (v) play(it); else it.video.pause(); }
    },
    destroy() {
      io.disconnect();
      clearTimeout(viewTimer);
      document.removeEventListener('visibilitychange', onVisibility);
      for (const it of items) it.setSrc(false);
    },
  };
}

// ------------------------------------------------------------------ reactions

async function react(reel, type, paint) {
  const before = { likes: reel.likes, dislikes: reel.dislikes, my_reaction: reel.my_reaction };
  const next = reel.my_reaction === type ? null : type;
  if (reel.my_reaction) reel[reel.my_reaction === 'like' ? 'likes' : 'dislikes'] -= 1;
  if (next) reel[next === 'like' ? 'likes' : 'dislikes'] += 1;
  reel.my_reaction = next;
  paint();
  try {
    Object.assign(reel, await api.put(`/api/reels/${encodeURIComponent(reel.id)}/reaction`, { reaction: next }));
  } catch (err) {
    Object.assign(reel, before);
    toast(err.message, 'error');
  }
  paint();
}

// ------------------------------------------------------------------ public comments

function reportReel(reel) {
  sheet((panel, close) => {
    const choices = REPORT_REASONS.map(([value, label], i) =>
      h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'reel-reason', value, checked: i === 0 }), h('span', { text: label })));
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال البلاغ');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await api.post(`/api/reels/${encodeURIComponent(reel.id)}/report`, { reason: panel.querySelector('input[name="reel-reason"]:checked').value });
        close();
        toast('شكرًا لك. سيراجع الفريق هذا الفيديو.');
      } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
    });
    panel.append(h('h2', { text: 'الإبلاغ عن فيديو' }), ...choices, h('div', { class: 'actions' }, submit));
  });
}

function authorLine(c) {
  const name = h('b', { class: 'rc-name' }, nameLine(c.author.name, c.author.gender, '', c.author.verified));
  return c.author.official
    ? h('span', { class: 'rc-author' }, name, h('span', { class: 'official-badge' }, icon('verified'), 'رسمي'))
    : h('span', { class: 'rc-author' }, name);
}

function reportComment(c) {
  sheet((panel, close) => {
    let reason = null;
    const send = h('button', { class: 'btn btn--danger btn--block', type: 'button', disabled: true }, 'إرسال البلاغ');
    panel.append(
      h('h2', { text: 'الإبلاغ عن تعليق' }),
      h('p', { text: 'سيراجع فريق DZPLAY هذا التعليق.' }),
      ...REPORT_REASONS.map(([value, label]) => h('label', { class: 'choice' },
        h('input', { type: 'radio', name: 'rc-reason', value, onchange: () => { reason = value; send.disabled = false; } }), label)),
      h('div', { class: 'actions' }, send, h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء')),
    );
    send.addEventListener('click', async () => {
      send.disabled = true;
      try {
        await api.post(`/api/reel-comments/${encodeURIComponent(c.id)}/report`, { reason });
        toast('شكرًا، وصل بلاغك.');
        close();
      } catch (err) {
        toast(err.message, 'error');
        send.disabled = false;
      }
    });
  });
}

function openComments(reel, config, paintReel) {
  sheet((panel, close) => {
    panel.classList.add('sheet--tall', 'reel-comments');
    const max = config.max_reel_comment_length || 300;
    const listEl = h('div', { class: 'rc-list', 'aria-live': 'polite' });
    const countEl = h('span', { class: 'rc-count' });
    const moreBtn = h('button', { class: 'btn btn--ghost btn--sm rc-more', type: 'button', hidden: true }, 'المزيد');
    const textarea = h('textarea', { rows: '1', maxlength: String(max), placeholder: 'اكتب تعليقًا عامًا…', 'aria-label': 'تعليقك', dir: 'auto' });
    const send = h('button', { class: 'send-btn', type: 'submit', 'aria-label': 'إرسال', disabled: true }, icon('send'));
    let cursor = null;
    let total = reel.comments;

    const paintCount = () => { countEl.textContent = count(total); reel.comments = total; paintReel(); };

    const item = (c) => {
      const row = h('div', { class: `rc-item ${c.author.official ? 'rc-item--official' : ''}` },
        c.author.official ? h('div', { class: 'avatar avatar--sm avatar--official', 'aria-hidden': 'true' }, icon('verified')) : personAvatar(c.author.name, { size: 'sm' }),
        h('div', { class: 'rc-body' },
          h('div', { class: 'rc-meta' }, authorLine(c), h('time', { class: 'rc-time', datetime: c.created_at, text: formatListTime(c.created_at) })),
          h('p', { class: 'rc-text', dir: 'auto', text: c.content })),
        c.mine
          ? h('button', { class: 'icon-btn icon-btn--plain rc-action', type: 'button', 'aria-label': 'حذف تعليقي', onclick: async () => {
            try {
              await api.del(`/api/reel-comments/${encodeURIComponent(c.id)}`);
              row.remove(); total = Math.max(0, total - 1); paintCount();
            } catch (err) { toast(err.message, 'error'); }
          } }, icon('trash'))
          : h('button', { class: 'icon-btn icon-btn--plain rc-action', type: 'button', 'aria-label': 'إبلاغ', onclick: () => reportComment(c) }, icon('flag')));
      return row;
    };

    async function load() {
      moreBtn.disabled = true;
      try {
        const qs = cursor ? `?cursor=${encodeURIComponent(cursor)}` : '';
        const data = await api.get(`/api/reels/${encodeURIComponent(reel.id)}/comments${qs}`);
        if (!cursor && !data.comments.length) listEl.replaceChildren(h('p', { class: 'rc-empty', text: 'لا توجد تعليقات بعد. كن أول من يعلّق.' }));
        listEl.append(...data.comments.map(item));
        cursor = data.next_cursor;
        total = data.total;
        paintCount();
        moreBtn.hidden = !cursor;
      } catch (err) {
        toast(err.message, 'error');
      }
      moreBtn.disabled = false;
    }
    moreBtn.addEventListener('click', load);

    const fit = () => {
      textarea.style.height = 'auto';
      textarea.style.height = `${Math.min(textarea.scrollHeight, 120)}px`;
      send.disabled = !textarea.value.trim();
    };
    textarea.addEventListener('input', fit);
    const form = h('form', { class: 'rc-composer glass', onsubmit: async (e) => {
      e.preventDefault();
      const content = textarea.value.trim();
      if (!content) return;
      send.disabled = true;
      try {
        const { comment } = await api.post(`/api/reels/${encodeURIComponent(reel.id)}/comments`, { content });
        const empty = listEl.querySelector('.rc-empty');
        if (empty) empty.remove();
        listEl.prepend(item(comment));
        listEl.scrollTop = 0;
        textarea.value = '';
        total += 1;
        paintCount();
      } catch (err) {
        toast(err.message, 'error', 4500);
      }
      fit();
    } }, textarea, send);
    // keep the send tap from blurring the field first (would close the keyboard and lose the tap)
    send.addEventListener('pointerdown', (e) => e.preventDefault());

    panel.append(
      h('div', { class: 'rc-head' }, h('h2', { text: 'التعليقات' }), countEl),
      h('p', { class: 'rc-note', text: 'تعليقات المقاطع عامة: يراها الجميع مع اسمك الظاهر.' }),
      listEl, moreBtn, form,
    );
    paintCount();
    load();
  });
}
