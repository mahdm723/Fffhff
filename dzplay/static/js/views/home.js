// Home: two full-screen panes side by side — Reels (default) and Ideas — switched by a
// horizontal swipe or the "Reels | الأفكار" indicator at the top.
//
// Swipe conflicts: the pager is a native horizontal scroll-snap container. A photo carousel
// inside a reel is a nested horizontal scroller, so the browser gives a swipe to the carousel
// while it still has photos in that direction; once it is on its last photo, the next swipe
// scrolls the pager to Ideas (scroll latching). Vertical swipes go to the reels list.
import { api } from '../api.js';
import { icon } from '../icons.js';
import { infiniteSentinel, onIdeaChange, postCard } from '../ideas.js';
import { autoGrow, h, newClientId, toast, wordmark } from '../ui.js';
import { renderReels } from './reels.js';

let currentPane = 'reels'; // Reels when the app opens; remembered while it stays open

const DRAFT_KEY = 'dz:idea-draft';
const STALE_MS = 5 * 60 * 1000;

// Kept between tab switches so returning to Home doesn't reshuffle or lose the scroll position.
const feedState = { posts: [], cursor: null, done: false, loadedAt: 0, scrollY: 0 };

function readDraft() { try { return localStorage.getItem(DRAFT_KEY) || ''; } catch { return ''; } }
function writeDraft(v) { try { v ? localStorage.setItem(DRAFT_KEY, v) : localStorage.removeItem(DRAFT_KEY); } catch { /* ignore */ } }

export function resetFeedCache() {
  Object.assign(feedState, { posts: [], cursor: null, done: false, loadedAt: 0, scrollY: 0 });
}

export function renderHome(page, ctx) {
  page.classList.add('page--home');
  const reelsPane = h('section', { class: 'home-pane home-pane--reels', id: 'pane-reels', 'aria-label': 'Reels' });
  const ideasPane = h('section', { class: 'home-pane home-pane--ideas', id: 'pane-ideas', 'aria-label': 'الأفكار' });
  const pager = h('div', { class: 'home-pager' }, reelsPane, ideasPane);
  const tab = (id, label, pane) => h('button', {
    type: 'button', role: 'tab', class: 'home-switch__tab', 'aria-controls': pane.id, dataset: { pane: id },
    onclick: () => show(id, true),
  }, label);
  const tabs = [tab('reels', 'Reels', reelsPane), tab('ideas', 'الأفكار', ideasPane)];
  const switcher = h('div', { class: 'home-switch', role: 'tablist', 'aria-label': 'الصفحة الرئيسية' },
    h('div', { class: 'home-switch__inner glass glass--blur' }, ...tabs, h('span', { class: 'home-switch__bar', 'aria-hidden': 'true' })));
  page.replaceChildren(pager, switcher);

  const reels = renderReels(reelsPane, ctx);
  const cleanupIdeas = renderIdeasPane(ideasPane, ctx);

  function mark(id) {
    currentPane = id;
    tabs.forEach((t) => t.setAttribute('aria-selected', String(t.dataset.pane === id)));
    switcher.dataset.pane = id;
    page.dataset.pane = id;
    reels.setVisible(id === 'reels');
  }
  function show(id, smooth) {
    (id === 'reels' ? reelsPane : ideasPane).scrollIntoView({ inline: 'start', block: 'nearest', behavior: smooth ? 'smooth' : 'auto' });
    mark(id);
  }
  // Which pane is on screen after a swipe (works whatever the RTL scrollLeft convention is).
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting && e.intersectionRatio > 0.55) mark(e.target === reelsPane ? 'reels' : 'ideas');
  }, { root: pager, threshold: [0.55] });
  io.observe(reelsPane);
  io.observe(ideasPane);
  requestAnimationFrame(() => show(currentPane, false));

  return () => {
    io.disconnect();
    reels.destroy();
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
  const composer = h('section', { class: 'idea-composer glass glass--lift', 'aria-labelledby': 'idea-title' },
    h('div', { class: 'idea-composer__head' },
      h('span', { class: 'idea-composer__icon' }, icon('bulb')),
      h('div', {},
        h('h1', { id: 'idea-title', class: 'idea-composer__title', text: 'شارك فكرة' }),
        h('p', { class: 'idea-composer__sub', text: 'يقرؤها الجميع، ويظهر اسمك dzplay فقط.' }),
      ),
    ),
    textarea,
    h('div', { class: 'idea-composer__bar' }, counter, publish),
  );

  const update = () => {
    counter.textContent = textarea.value.length ? `${textarea.value.length} / ${max}` : '';
    counter.classList.toggle('is-over', textarea.value.length > max);
    publish.disabled = !textarea.value.trim() || textarea.value.length > max;
    composer.classList.toggle('is-active', document.activeElement === textarea || !!textarea.value);
    writeDraft(textarea.value);
  };
  textarea.addEventListener('input', update);
  textarea.addEventListener('focus', update);
  textarea.addEventListener('blur', update);
  autoGrow(textarea, 320);
  update();

  publish.addEventListener('click', async () => {
    const content = textarea.value.trim();
    if (!content) return;
    publish.disabled = true;
    publish.textContent = '…';
    try {
      const created = await api.post('/api/posts', { content, client_id: newClientId() });
      textarea.value = '';
      update();
      textarea.blur();
      feedState.posts.unshift(created);
      list.prepend(postCard(created, { navigate, onRemoved: removed }));
      clearEmpty();
      toast('نُشرت فكرتك ✨');
    } catch (err) {
      toast(err.message, 'error', 4500);
    }
    publish.textContent = 'نشر';
    update();
  });

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

  page.replaceChildren(
    h('header', { class: 'topbar' }, wordmark()),
    composer,
    h('div', { class: 'section-head' },
      h('h2', { class: 'section-title', text: 'أفكار من الآخرين' }),
      refreshBtn,
    ),
    list,
    status,
  );

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
    sentinel.stop();
    offChange();
    page.removeEventListener('scroll', onScroll);
  };
}
