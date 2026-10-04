// V5: pick a picture on the phone, check it HERE first, compress it, upload it, view it full screen.
//
// Checks on the device (the server repeats every one of them): the type from the file's first bytes
// (never its name), size and dimensions from the server's limits, an on-device NSFW model (NSFWJS,
// MIT — loaded only at this moment, from our own server), and the caption word filter (server precheck).
// The picture is resized and re-encoded on a canvas before it leaves the phone: smaller upload, and
// the camera's EXIF/GPS data is already gone (the server strips everything again anyway).
import { api, ApiError } from './api.js';
import { icon } from './icons.js';
import { h } from './ui.js';

const MB = 1024 * 1024;
let cfgCache = null;
let cfgAt = 0;

export async function uploadConfig(force = false) {
  if (!force && cfgCache && Date.now() - cfgAt < 30000) return cfgCache;
  cfgCache = await api.get('/api/uploads/config');
  cfgAt = Date.now();
  return cfgCache;
}
export function forgetUploadConfig() { cfgCache = null; }

// ------------------------------------------------------------------ type from the first bytes
const HEIF = ['heic', 'heix', 'hevc', 'hevx', 'heim', 'heis', 'mif1', 'msf1'];
function ascii(b, from, to) { return String.fromCharCode(...b.slice(from, to)); }
export function sniff(b) {
  if (b[0] === 0xff && b[1] === 0xd8 && b[2] === 0xff) return { kind: 'image', type: 'jpeg' };
  if (b[0] === 0x89 && ascii(b, 1, 4) === 'PNG') return { kind: 'image', type: 'png' };
  if (ascii(b, 0, 4) === 'RIFF' && ascii(b, 8, 12) === 'WEBP') return { kind: 'image', type: 'webp' };
  if (b[0] === 0x1a && b[1] === 0x45 && b[2] === 0xdf && b[3] === 0xa3) return { kind: 'video', type: 'webm' };
  if (ascii(b, 4, 8) === 'ftyp') {
    const brand = ascii(b, 8, 12);
    if (brand === 'avif' || brand === 'avis') return null;
    if (HEIF.includes(brand)) return { kind: 'image', type: 'heic' };
    if (brand === 'qt  ') return { kind: 'video', type: 'mov' };
    return { kind: 'video', type: 'mp4' };
  }
  if (['moov', 'mdat', 'wide', 'free', 'skip'].includes(ascii(b, 4, 8))) return { kind: 'video', type: 'mov' };
  return null;
}

async function head(file) { return new Uint8Array(await file.slice(0, 32).arrayBuffer()); }

export class PickError extends Error {}

// ------------------------------------------------------------------ choose a file
export function chooseFile(accept) {
  return new Promise((resolve) => {
    const input = h('input', { type: 'file', accept, hidden: true });
    let done = false;
    const finish = (file) => { if (done) return; done = true; input.remove(); resolve(file || null); };
    input.addEventListener('change', () => finish(input.files && input.files[0]));
    input.addEventListener('cancel', () => finish(null));
    document.body.append(input);
    input.click();
  });
}

// ------------------------------------------------------------------ NSFW model (lazy)
let nsfwModel = null;
function loadScript(src) {
  return new Promise((resolve, reject) => {
    if (document.querySelector(`script[src="${src}"]`) && window.nsfwjs) { resolve(); return; }
    const s = h('script', { src, async: true });
    s.onload = () => resolve();
    s.onerror = () => reject(new Error('script'));
    document.head.append(s);
  });
}
async function nsfw() {
  if (nsfwModel) return nsfwModel;
  await loadScript('/vendor/nsfw/nsfwjs.min.js');
  nsfwModel = await window.nsfwjs.load('/vendor/nsfw/model/model.json', { size: 224, type: 'layers' });
  return nsfwModel;
}
/** Probabilities for a canvas/image: { porn, hentai, sexy, ... } (0..1). */
export async function nsfwScores(source) {
  const model = await nsfw();
  const preds = await model.classify(source, 5);
  const out = {};
  for (const p of preds) out[p.className.toLowerCase()] = p.probability;
  return out;
}
function refused(scores, cfg) {
  const bad = (scores.porn || 0) + (scores.hentai || 0);
  return bad >= cfg.nsfw.block || (cfg.nsfw.sexy < 1 && (scores.sexy || 0) >= cfg.nsfw.sexy);
}

// ------------------------------------------------------------------ pictures
async function decode(file) {
  if (window.createImageBitmap) {
    try { return await createImageBitmap(file, { imageOrientation: 'from-image' }); } catch { /* fall back */ }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = new Image();
    img.decoding = 'async';
    img.src = url;
    await img.decode();
    return img;
  } finally { URL.revokeObjectURL(url); }
}

function canvasFor(src, maxSide) {
  const w = src.width || src.naturalWidth;
  const ht = src.height || src.naturalHeight;
  const scale = Math.min(1, maxSide / Math.max(w, ht));
  const c = document.createElement('canvas');
  c.width = Math.max(1, Math.round(w * scale));
  c.height = Math.max(1, Math.round(ht * scale));
  const ctx = c.getContext('2d');
  ctx.fillStyle = '#fff';
  ctx.fillRect(0, 0, c.width, c.height); // transparent PNGs: white, like the server
  ctx.drawImage(src, 0, 0, c.width, c.height);
  return c;
}

const toBlob = (canvas, type, q) => new Promise((resolve) => canvas.toBlob(resolve, type, q));

/** Checks and compresses a picture. Returns { blob, width, height, preview } or throws PickError (Arabic). */
export async function prepareImage(file, cfg, { onStage } = {}) {
  if (!file) throw new PickError('لم تُختر صورة.');
  const found = sniff(await head(file));
  if (!found || found.kind !== 'image') throw new PickError('هذا الملف ليس صورة مدعومة (JPEG أو PNG أو WebP أو HEIC).');
  if (!cfg.image_types.includes(found.type)) throw new PickError('نوع الصورة غير مسموح.');
  if (file.size > cfg.image_max_mb * MB * 4) throw new PickError(`الصورة كبيرة جدًا (الحد ${cfg.image_max_mb} MB).`);
  let bitmap;
  try { bitmap = await decode(file); } catch { bitmap = null; }
  if (!bitmap) {
    // HEIC that this browser cannot open: sent as is (the server converts and checks it).
    if (found.type === 'heic' && file.size <= cfg.image_max_mb * MB) {
      return { blob: file, width: 0, height: 0, preview: null };
    }
    throw new PickError('تعذّر فتح الصورة. جرّب صورة أخرى.');
  }
  const w = bitmap.width || bitmap.naturalWidth;
  const ht = bitmap.height || bitmap.naturalHeight;
  if (Math.min(w, ht) < cfg.image_min_side) throw new PickError('الصورة صغيرة جدًا.');
  if (Math.max(w, ht) > cfg.image_max_side) throw new PickError('أبعاد الصورة كبيرة جدًا.');
  const canvas = canvasFor(bitmap, cfg.device_max_side);
  if (bitmap.close) bitmap.close();
  if (cfg.nsfw && cfg.nsfw.device) {
    onStage && onStage('scan');
    let scores = null;
    try { scores = await nsfwScores(canvasFor(canvas, 224)); } catch { scores = null; } // model unavailable: the server checks
    if (scores && refused(scores, cfg)) throw new PickError('لا يمكن نشر هذه الصورة: تبدو مخالفة لإرشادات المجتمع.');
  }
  const blob = await toBlob(canvas, 'image/jpeg', cfg.device_quality);
  if (!blob) throw new PickError('تعذّر تجهيز الصورة.');
  if (blob.size > cfg.image_max_mb * MB) throw new PickError(`الصورة كبيرة جدًا (الحد ${cfg.image_max_mb} MB).`);
  return { blob, width: canvas.width, height: canvas.height, preview: URL.createObjectURL(blob) };
}

// ------------------------------------------------------------------ upload
/** POST the file with progress. Resolves { id } (processing on the server). */
export function uploadBlob(blob, { purpose, conversationId = null, onProgress } = {}) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const q = `purpose=${encodeURIComponent(purpose)}${conversationId ? `&conversation_id=${encodeURIComponent(conversationId)}` : ''}`;
    xhr.open('POST', `/api/uploads?${q}`);
    xhr.withCredentials = true;
    xhr.setRequestHeader('X-DZ-Requested', '1');
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    xhr.setRequestHeader('Accept', 'application/json');
    xhr.upload.onprogress = (e) => { if (e.lengthComputable && onProgress) onProgress(e.loaded / e.total); };
    xhr.onload = () => {
      let data = null;
      try { data = JSON.parse(xhr.responseText); } catch { /* empty */ }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.upload) { resolve(data.upload); return; }
      const err = (data && data.error) || {};
      reject(new ApiError(xhr.status, err.code || 'error', err.message || 'تعذّر رفع الملف.', err.retry_after ?? null));
    };
    xhr.onerror = () => reject(new ApiError(0, 'network', 'لا يوجد اتصال بالإنترنت.'));
    xhr.send(blob);
  });
}

/** Wait until the server finished checking + storing the upload. Resolves the status, throws when refused. */
export async function waitReady(id, { timeoutMs = 10 * 60 * 1000 } = {}) {
  const until = Date.now() + timeoutMs;
  let delay = 500;
  while (Date.now() < until) {
    const { upload } = await api.get(`/api/uploads/${encodeURIComponent(id)}`);
    if (upload.state === 'ready') return upload;
    if (upload.state === 'rejected' || upload.state === 'failed') throw new PickError(upload.error || 'الملف مرفوض.');
    await new Promise((r) => setTimeout(r, delay));
    delay = Math.min(2000, delay + 250);
  }
  throw new PickError('استغرقت المعالجة وقتًا طويلًا. حاول مرة أخرى.');
}

// ------------------------------------------------------------------ full-screen viewer
/**
 * Full-screen picture. `src` is a URL or a Blob URL. Options: countdown (seconds) with onExpire,
 * secure (Android app: no screenshots while open), onReport.
 */
export function openViewer(src, { alt = 'صورة', countdown = null, onExpire = null, secure = false, onReport = null, onClose = null } = {}) {
  const native = window.DZPLAYAndroid;
  if (secure && native && native.setSecure) { try { native.setSecure(true); } catch { /* old app */ } }
  const img = h('img', { class: 'viewer__img', src, alt, draggable: 'false' });
  const timer = h('span', { class: 'viewer__timer', hidden: countdown == null });
  const closeBtn = h('button', { class: 'viewer__btn', type: 'button', 'aria-label': 'إغلاق' }, icon('close'));
  const report = onReport ? h('button', { class: 'viewer__btn viewer__btn--text', type: 'button' }, icon('flag'), 'إبلاغ') : null;
  const overlay = h('div', { class: 'viewer', role: 'dialog', 'aria-modal': 'true', 'aria-label': alt },
    h('div', { class: 'viewer__bar' }, closeBtn, timer, report),
    img);
  let left = countdown;
  let tick = null;
  let closed = false;
  const close = () => {
    if (closed) return;
    closed = true;
    clearInterval(tick);
    document.removeEventListener('keydown', onKey);
    overlay.remove();
    img.removeAttribute('src');
    if (secure && native && native.setSecure) { try { native.setSecure(false); } catch { /* ignore */ } }
    if (onClose) onClose();
  };
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  const paint = () => { timer.replaceChildren(icon('timer'), h('span', { text: `${Math.max(0, left)} ث` })); };
  if (countdown != null) {
    paint();
    tick = setInterval(() => {
      left -= 1;
      paint();
      if (left <= 0) { close(); if (onExpire) onExpire(); }
    }, 1000);
  }
  closeBtn.addEventListener('click', close);
  if (report) report.addEventListener('click', () => { close(); onReport(); });
  overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  overlay.addEventListener('contextmenu', (e) => e.preventDefault());
  document.addEventListener('keydown', onKey);
  document.body.append(overlay);
  closeBtn.focus();
  return close;
}

// ------------------------------------------------------------------ videos (creator studio)
function videoMeta(url) {
  return new Promise((resolve, reject) => {
    const v = document.createElement('video');
    v.preload = 'metadata';
    v.muted = true;
    v.playsInline = true;
    v.onloadedmetadata = () => resolve(v);
    v.onerror = () => reject(new Error('video'));
    v.src = url;
  });
}
function seek(v, t) {
  return new Promise((resolve) => {
    const done = () => { v.removeEventListener('seeked', done); resolve(); };
    v.addEventListener('seeked', done);
    v.currentTime = t;
    setTimeout(done, 3000);
  });
}

/** Checks a video on the phone: type (first bytes), size, duration, a few frames through the NSFW model.
 *  The file itself is sent as is (the server re-encodes it). Returns { blob, duration, preview }. */
export async function prepareVideo(file, cfg, { onStage } = {}) {
  if (!file) throw new PickError('لم يُختر فيديو.');
  const found = sniff(await head(file));
  if (!found || found.kind !== 'video' || !cfg.video_types.includes(found.type)) {
    throw new PickError('هذا الملف ليس فيديو مدعومًا (MP4 أو MOV أو WebM).');
  }
  if (file.size > cfg.reel.max_mb * MB) throw new PickError(`الفيديو كبير جدًا (الحد ${cfg.reel.max_mb} MB).`);
  const preview = URL.createObjectURL(file);
  let v;
  try { v = await videoMeta(preview); } catch {
    // This phone cannot decode the format (e.g. HEVC): send it anyway, the server checks and re-encodes it.
    URL.revokeObjectURL(preview);
    return { blob: file, duration: 0, preview: null };
  }
  const duration = v.duration || 0;
  if (duration > cfg.reel.max_seconds + 0.5) { URL.revokeObjectURL(preview); throw new PickError(`مدة الفيديو أطول من ${cfg.reel.max_seconds} ثانية.`); }
  if (duration && duration < cfg.reel.min_seconds) { URL.revokeObjectURL(preview); throw new PickError('الفيديو قصير جدًا.'); }
  if (cfg.nsfw && cfg.nsfw.device && v.videoWidth) {
    onStage && onStage('scan');
    const n = Math.max(1, cfg.nsfw.frames || 4);
    try {
      for (let i = 0; i < n; i += 1) {
        await seek(v, (duration * (i + 0.5)) / n);
        const scores = await nsfwScores(canvasFor(v, 224));
        if (refused(scores, cfg)) { URL.revokeObjectURL(preview); throw new PickError('لا يمكن نشر هذا الفيديو: يبدو مخالفًا لإرشادات المجتمع.'); }
      }
    } catch (err) { if (err instanceof PickError) throw err; /* model unavailable: the server checks */ }
  }
  v.removeAttribute('src');
  return { blob: file, duration, preview };
}

/** Full-screen video player (studio previews, profile reels). */
export function openVideo(src, { poster = null } = {}) {
  const video = h('video', { class: 'viewer__video', src, poster, controls: true, autoplay: true, playsinline: true });
  const closeBtn = h('button', { class: 'viewer__btn', type: 'button', 'aria-label': 'إغلاق' }, icon('close'));
  const overlay = h('div', { class: 'viewer', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'فيديو' },
    h('div', { class: 'viewer__bar' }, closeBtn), video);
  const close = () => { video.pause(); video.removeAttribute('src'); video.load(); overlay.remove(); };
  closeBtn.addEventListener('click', close);
  document.body.append(overlay);
  return close;
}
