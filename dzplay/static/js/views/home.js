// Home: «السوق | الأفكار» — two panes side by side; swipe or tap the switch at the top (V6 phase 2).
// Ideas is the default; the last pane used is remembered on this phone.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { infiniteSentinel, onIdeaChange, postCard } from '../ideas.js';
import { PickError, chooseFile, prepareImage, uploadBlob, uploadConfig, waitReady } from '../media-pick.js';
import { autoGrow, h, newClientId, toast, wordmark } from '../ui.js';
import { renderMarket } from './market.js';

const DRAFT_KEY = 'dz:idea-draft';
const PANE_KEY = 'dz:home-pane';
const STALE_MS = 5 * 60 * 1000;

// Kept between tab switches so returning to Home doesn't reshuffle or lose the scroll position.
const feedState = { posts: [], cursor: null, done: false, loadedAt: 0, scrollY: 0 };

function readDraft() { try { return localStorage.getItem(DRAFT_KEY) || ''; } catch { return ''; } }
function writeDraft(v) { try { v ? localStorage.setItem(DRAFT_KEY, v) : localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ } }

export function resetFeedCache() {
  Object.assign(feedState, { posts: [], cursor: null, done: false, loadedAt: 0, scrollY: 0 });
}

function readPane() { try { return localStorage.getItem(PANE_KEY) === 'market' ? 'market' : 'ideas'; } catch { return 'ideas'; } }
function writePane(v) { try { localStorage.setItem(PANE_KEY, v); } catch { /* ignore */ } }

export function renderHome(page, ctx) {
  page.classList.add('page--home');
  const marketPane = h('section', { class: 'home-pane home-pane--market', id: 'pane-market', 'aria-label': 'السوق' });
  const ideasPane = h('section', { class: 'home-pane home-pane--ideas', id: 'pane-ideas', 'aria-label': 'الأفكار' });
  const pager = h('div', { class: 'home-pager' }, marketPane, ideasPane);
  const tab = (id, label, pane) => h('button', {
    type: 'button', role: 'tab', class: 'home-switch__tab', 'aria-controls': pane.id, dataset: { pane: id },
    onclick: () => show(id, true),
  }, label);
  const tabs = [tab('market', 'السوق', marketPane), tab('ideas', 'الأفكار', ideasPane)];
  const switcher = h('div', { class: 'home-switch', role: 'tablist', 'aria-label': 'الصفحة الرئيسية' },
    h('div', { class: 'home-switch__inner glass glass--blur' }, ...tabs, h('span', { class: 'home-switch__bar', 'aria-hidden': 'true' })));
  page.replaceChildren(pager, switcher);

  const market = renderMarket(marketPane);
  const cleanupIdeas = renderIdeasPane(ideasPane, ctx);
  let current = readPane();

  function mark(id) {
    current = id;
    tabs.forEach((t) => t.setAttribute('aria-selected', String(t.dataset.pane === id)));
    switcher.dataset.pane = id;
    page.dataset.pane = id;
    market.setVisible(id === 'market');
    writePane(id);
  }
  function show(id, smooth) {
    (id === 'market' ? marketPane : ideasPane).scrollIntoView({ inline: 'start', block: 'nearest', behavior: smooth ? 'smooth' : 'auto' });
    mark(id);
  }
  // Which pane is on screen after a swipe (works whatever the RTL scrollLeft convention is).
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting && e.intersectionRatio > 0.55) mark(e.target === marketPane ? 'market' : 'ideas');
  }, { root: pager, threshold: [0.55] });
  io.observe(marketPane);
  io.observe(ideasPane);
  requestAnimationFrame(() => show(current, false));

  return () => {
    io.disconnect();
    market.destroy();
    cleanupIdeas();
    page.classList.remove('page--home');
    delete page.dataset.pane;
  };
}

function renderIdeasPane(page, { config, navigate }) {
  const max = config.max_post_length;
  const savedY = feedState.scrollY; // read before the router's scroll-to-top reaches our listener
  const list = h('div', { class: 'feed', 'aria-live': 'polite' });
  const status = h('div', { class: 'feed-status' });
  let loading = false;
  let sentinel = null;

  // ------------------------------------------------------------ composer
  const textarea = h('textarea', {
    id: 'idea-compose', rows: '3', 'aria-label': 'فكرتك', placeholder: 'اكتب فكرة تريد مشاركتها مع الآخرين…', maxlength: String(max + 200),
  });
  textarea.value = readDraft();
  const counter = h('span', { class: 'counter' });
  const publish = h('button', { class: 'btn btn--primary btn--sm', type: 'button' }, 'نشر');
  // V5: one picture per idea (IDEA_IMAGE_LIMIT_PER_24H), checked + compressed on the phone first.
  const imageBtn = h('button', { class: 'icon-btn icon-btn--plain idea-composer__img', type: 'button', 'aria-label': 'إضافة صورة', hidden: true }, icon('image'));
  const imageNote = h('span', { class: 'idea-composer__note', hidden: true });
  const mediaBox = h('div', { class: 'idea-media', hidden: true });
  let picked = null; // { blob, preview }
  let quota = null;
  let quotaTimer = null;
  let busy = false;

  function paintQuota() {
    clearInterval(quotaTimer);
    if (!quota || !quota.enabled) { imageBtn.hidden = true; imageNote.hidden = true; return; }
    if (!quota.member) { // V6 phase 4: picture posts are a membership feature
      imageBtn.hidden = true;
      imageNote.hidden = false;
      imageNote.replaceChildren(h('button', { class: 'link-btn', type: 'button', onclick: () => navigate('#/membership') }, 'الصور للأعضاء'));
      return;
    }
    imageBtn.hidden = false;
    const tickNote = () => {
      const left = quota.next_at ? Date.parse(quota.next_at) - Date.now() : 0;
      if (quota.remaining > 0 || left <= 0) {
        imageBtn.disabled = busy;
        imageNote.hidden = true;
        if (quota.remaining <= 0 && left <= 0) { quota.remaining = 1; quota.next_at = null; }
        clearInterval(quotaTimer);
        return;
      }
      imageBtn.disabled = true;
      const s = Math.ceil(left / 1000);
      const hh = String(Math.floor(s / 3600)).padStart(2, '0');
      const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
      const ss = String(s % 60).padStart(2, '0');
      imageNote.hidden = false;
      imageNote.textContent = `الصورة التالية بعد ${hh}:${mm}:${ss}`;
    };
    tickNote();
    if (quota.remaining <= 0) quotaTimer = setInterval(tickNote, 1000);
  }
  async function refreshQuota() {
    try { const cfg = await uploadConfig(true); quota = cfg.available ? cfg.idea : null; } catch { quota = null; }
    paintQuota();
  }

  function clearPicked() {
    if (picked && picked.preview) URL.revokeObjectURL(picked.preview);
    picked = null;
    mediaBox.hidden = true;
    mediaBox.replaceChildren();
    update();
  }
  function showPicked(stage = '') {
    const img = picked.preview ? h('img', { src: picked.preview, alt: 'الصورة المختارة' }) : h('div', { class: 'idea-media__ph' }, icon('image'));
    const remove = h('button', { class: 'idea-media__x', type: 'button', 'aria-label': 'إزالة الصورة', disabled: busy, onclick: clearPicked }, icon('close'));
    mediaBox.replaceChildren(...[img, remove, stage ? h('div', { class: 'idea-media__stage', text: stage }) : null].filter(Boolean));
    mediaBox.hidden = false;
  }
  imageBtn.addEventListener('click', async () => {
    const file = await chooseFile('image/jpeg,image/png,image/webp,image/heic,image/heif');
    if (!file) return;
    busy = true;
    imageBtn.disabled = true;
    try {
      const cfg = await uploadConfig();
      if (picked) clearPicked();
      picked = { blob: null, preview: null };
      showPicked('جارٍ فحص الصورة…');
      picked = await prepareImage(file, cfg, { onStage: () => showPicked('جارٍ فحص الصورة…') });
      busy = false;
      showPicked();
    } catch (err) {
      busy = false;
      picked = null;
      mediaBox.hidden = true;
      toast(err instanceof PickError ? err.message : (err.message || 'تعذّر تجهيز الصورة.'), 'error', 4500);
    }
    paintQuota();
    update();
  });
  const composer = h('section', { class: 'idea-composer glass glass--lift', 'aria-labelledby': 'idea-title' },
    h('div', { class: 'idea-composer__head' },
      h('span', { class: 'idea-composer__icon' }, icon('bulb')),
      h('div', {},
        h('h1', { id: 'idea-title', class: 'idea-composer__title', text: 'شارك فكرة' }),
        h('p', { class: 'idea-composer__sub', text: 'يقرؤها الجميع، ويظهر معها اسمك الذي اخترته.' }),
      ),
    ),
    textarea,
    mediaBox,
    h('div', { class: 'idea-composer__bar' }, imageBtn, imageNote, counter, publish),
  );

  const update = () => {
    counter.textContent = textarea.value.length ? `${textarea.value.length} / ${max}` : '';
    counter.classList.toggle('is-over', textarea.value.length > max);
    const hasImage = !!(picked && picked.blob);
    publish.disabled = busy || (!textarea.value.trim() && !hasImage) || textarea.value.length > max;
    composer.classList.toggle('is-active', document.activeElement === textarea || !!textarea.value || hasImage);
    writeDraft(textarea.value);
  };
  textarea.addEventListener('input', update);
  textarea.addEventListener('focus', update);
  textarea.addEventListener('blur', update);
  autoGrow(textarea, 320);
  update();

  publish.addEventListener('click', async () => {
    const content = textarea.value.trim();
    const withImage = !!(picked && picked.blob);
    if (!content && !withImage) return;
    publish.disabled = true;
    publish.textContent = '…';
    busy = true;
    try {
      let mediaId = null;
      if (withImage) {
        await api.post('/api/uploads/precheck', { purpose: 'idea', caption: content || null });
        showPicked('جارٍ الرفع 0%');
        const up = await uploadBlob(picked.blob, {
          purpose: 'idea', onProgress: (f) => showPicked(f < 1 ? `جارٍ الرفع ${Math.round(f * 100)}%` : 'جارٍ الفحص على الخادم…'),
        });
        showPicked('جارٍ الفحص على الخادم…');
        await waitReady(up.id);
        mediaId = up.id;
      }
      const created = await api.post('/api/posts', { content, client_id: newClientId(), media_id: mediaId });
      busy = false;
      if (withImage) { clearPicked(); refreshQuota(); }
      textarea.value = '';
      update();
      textarea.blur();
      feedState.posts.unshift(created);
      list.prepend(postCard(created, { navigate, onRemoved: removed }));
      clearEmpty();
      toast(created.status === 'pending' ? 'أُرسلت فكرتك، وستظهر بعد مراجعة الصورة.' : 'نُشرت فكرتك ✨');
    } catch (err) {
      busy = false;
      if (picked) showPicked();
      toast(err.message, 'error', 4500);
      if (err.code === 'idea_image_limit') { clearPicked(); refreshQuota(); }
    }
    publish.textContent = 'نشر';
    update();
  });
  refreshQuota();

  // ------------------------------------------------------------ feed
  const refreshBtn = h('button', { class: 'icon-btn glass', type: 'button', 'aria-label': 'أفكار أخرى' }, icon('refresh'));
  refreshBtn.addEventListener('click', () => { resetFeedCache(); list.replaceChildren(); loadMore(true); });

  function removed(id) { feedState.posts = feedState.posts.filter((p) => p.id !== id); }

  function clearEmpty() { const e = list.querySelector('.empty'); if (e) e.remove(); }

  function showEmpty() {
    list.replaceChildren(h('div', { class: 'empty glass' },
      icon('bulb'),
      h('h2', { text: 'لا توجد أفكار بعد' }),
      h('p', { text: 'كن أول من يشارك فكرة مع الجميع.' }),
      h('button', { class: 'btn btn--primary', type: 'button', onclick: () => textarea.focus() }, 'اكتب أول فكرة'),
    ));
  }

  async function loadMore(first = false) {
    if (loading || (feedState.done && !first)) return;
    loading = true;
    status.replaceChildren(h('span', { class: 'spinner', 'aria-label': 'جارٍ التحميل' }));
    try {
      const qs = feedState.cursor ? `?cursor=${encodeURIComponent(feedState.cursor)}` : '';
      const data = await api.get(`/api/posts/feed${qs}`);
      const known = new Set(feedState.posts.map((p) => p.id));
      const fresh = data.posts.filter((p) => !known.has(p.id));
      feedState.posts.push(...fresh);
      feedState.cursor = data.next_cursor;
      feedState.done = !data.next_cursor;
      feedState.loadedAt = Date.now();
      for (const p of fresh) list.append(postCard(p, { navigate, onRemoved: removed }));
      if (!feedState.posts.length) showEmpty();
      status.replaceChildren(feedState.done && feedState.posts.length
        ? h('p', { class: 'feed-end', text: 'شاهدت كل الأفكار الحالية. اضغط ↻ لترتيب جديد.' }) : '');
    } catch (err) {
      status.replaceChildren(h('button', { class: 'btn btn--ghost', type: 'button', onclick: () => loadMore() }, 'إعادة المحاولة'));
      if (!err.isNetwork) toast(err.message, 'error');
    } finally {
      loading = false;
    }
  }

  const bell = h('button', { class: 'icon-btn glass bell-btn', type: 'button', 'aria-label': 'الإشعارات', onclick: () => navigate('#/notifications') }, icon('bell'));
  page.replaceChildren(
    h('header', { class: 'topbar' }, wordmark(), bell),
    composer,
    h('div', { class: 'section-head' },
      h('h2', { class: 'section-title', text: 'أفكار من الآخرين' }),
      refreshBtn,
    ),
    list,
    status,
  );
  requestAnimationFrame(() => document.dispatchEvent(new CustomEvent('dz:badges'))); // paint the bell's count

  const fresh = feedState.posts.length && Date.now() - feedState.loadedAt < STALE_MS;
  if (fresh) {
    for (const p of feedState.posts) list.append(postCard(p, { navigate, onRemoved: removed }));
    requestAnimationFrame(() => { page.scrollTop = savedY; });
  } else {
    resetFeedCache();
    loadMore(true);
  }
  sentinel = infiniteSentinel(() => { if (feedState.posts.length) loadMore(); });
  page.append(sentinel);

  const offChange = onIdeaChange(({ id, post, removed: gone }) => {
    const i = feedState.posts.findIndex((p) => p.id === id);
    if (i < 0) return;
    if (gone) feedState.posts.splice(i, 1);
    else feedState.posts[i] = post;
  });
  const onScroll = () => { feedState.scrollY = page.scrollTop; };
  page.addEventListener('scroll', onScroll, { passive: true });

  return () => {
    clearInterval(quotaTimer);
    if (picked && picked.preview) URL.revokeObjectURL(picked.preview);
    sentinel.stop();
    offChange();
    page.removeEventListener('scroll', onScroll);
  };
}
