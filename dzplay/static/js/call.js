// 1:1 voice / video calls (WebRTC).
//
// Media is end-to-end DTLS-SRTP between the two browsers and always goes through our TURN
// server (iceTransportPolicy "relay"), so neither side ever learns the other's IP address.
// Signaling (SDP / ICE / audio↔video) goes over the app WebSocket; start / answer / end also
// exist over REST. Quality: Opus with FEC + DTX and the browser's echo cancellation, noise
// suppression and auto gain; video starts at 720p/30 and steps down to 480p / 360p / 240p
// from getStats every 2 s (audio always has priority); ICE restarts on network changes.
import { api } from './api.js';
import { icon } from './icons.js';
import { confirmAge } from './people.js';
import { startPrefetch, stopPrefetch } from './reels-prefetch.js';
import * as store from './store.js';
import { REPORT_REASONS, confirmSheet, h, nameLine, personAvatar, sheet, toast } from './ui.js';

const DEVICE = Math.random().toString(36).slice(2, 12); // this tab: other devices stop ringing when it answers
const STATS_MS = 2000;
const LEVELS = [ // outgoing video ladder (capture is 720p)
  { label: '720p', maxBitrate: 1_500_000, scale: 1, fps: 30 },
  { label: '480p', maxBitrate: 800_000, scale: 1.5, fps: 30 },
  { label: '360p', maxBitrate: 450_000, scale: 2, fps: 24 },
  { label: '240p', maxBitrate: 200_000, scale: 3, fps: 15 },
];
const native = () => window.DZPLAYAndroid || null;
const PERM_KEY = 'dz:call-perm';

let cur = null; // the one live call on this device

// ------------------------------------------------------------------ helpers
function sendSignal(type, extra = {}) {
  if (!cur) return false;
  const msg = { type, call_id: cur.id, ...extra };
  if (store.sendWS(msg)) return true;
  cur.outbox.push(msg); // socket reconnecting: flushed on reconnect
  return false;
}

function flushOutbox() {
  if (!cur || !cur.outbox.length) return;
  const queued = cur.outbox.splice(0);
  for (const m of queued) if (!store.sendWS(m)) { cur.outbox.push(m); }
}

function fmt(sec) {
  const s = Math.max(0, Math.floor(sec));
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${String(s % 60).padStart(2, '0')}` : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function mediaError(err, video) {
  if (err && (err.name === 'NotAllowedError' || err.name === 'SecurityError')) {
    return `لم يُسمح باستخدام ${video ? 'الكاميرا أو الميكروفون' : 'الميكروفون'}. فعّل الإذن من إعدادات المتصفح أو التطبيق ثم أعد المحاولة.`;
  }
  if (err && (err.name === 'NotFoundError' || err.name === 'OverconstrainedError')) {
    return video ? 'لم نجد كاميرا أو ميكروفون على هذا الجهاز.' : 'لم نجد ميكروفونًا على هذا الجهاز.';
  }
  if (err && err.name === 'NotReadableError') return 'الكاميرا أو الميكروفون مستخدم في تطبيق آخر.';
  return 'تعذّر تشغيل الميكروفون أو الكاميرا.';
}

const AUDIO = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
const videoConstraints = (facing = 'user') => ({ facingMode: facing, width: { ideal: 1280 }, height: { ideal: 720 }, frameRate: { ideal: 30, max: 30 } });

async function getMedia(video) {
  try {
    return await navigator.mediaDevices.getUserMedia({ audio: AUDIO, video: video ? videoConstraints() : false });
  } catch (err) {
    if (video && err && err.name !== 'NotAllowedError') {
      toast('تعذّر تشغيل الكاميرا، ستبدأ المكالمة بالصوت فقط.');
      return navigator.mediaDevices.getUserMedia({ audio: AUDIO, video: false });
    }
    throw err;
  }
}

/** First call on this device: say why the browser will ask for the microphone / camera. */
function explainPermissions(video) {
  let seen = false;
  try { seen = localStorage.getItem(PERM_KEY) === '1'; } catch { /* ignore */ }
  if (seen) return Promise.resolve(true);
  return confirmSheet({
    title: 'السماح بالمكالمات',
    text: `سيطلب منك ${native() ? 'التطبيق' : 'المتصفح'} الإذن باستخدام الميكروفون${video ? ' والكاميرا' : ''}. لا تُسجَّل المكالمات أبدًا، وتمر عبر خادم وسيط فلا يرى الطرف الآخر عنوانك.`,
    confirm: 'متابعة',
  }).then((ok) => {
    if (ok) { try { localStorage.setItem(PERM_KEY, '1'); } catch { /* ignore */ } }
    return ok;
  });
}

// Opus: in-band FEC (packet-loss resilience) + DTX (silence costs ~nothing), mono voice.
export function mungeOpus(sdp) {
  const m = sdp.match(/a=rtpmap:(\d+) opus\/48000/i);
  if (!m) return sdp;
  const pt = m[1];
  const want = { useinbandfec: '1', usedtx: '1', stereo: '0', maxaveragebitrate: '32000' };
  const re = new RegExp(`a=fmtp:${pt} ([^\\r\\n]*)`);
  if (!re.test(sdp)) return sdp.replace(m[0], `${m[0]}\r\na=fmtp:${pt} ${Object.entries(want).map(([k, v]) => `${k}=${v}`).join(';')}`);
  return sdp.replace(re, (_all, params) => {
    const p = new Map(params.split(';').filter(Boolean).map((kv) => kv.split('=')));
    for (const [k, v] of Object.entries(want)) p.set(k, v);
    return `a=fmtp:${pt} ${[...p].map(([k, v]) => `${k}=${v}`).join(';')}`;
  });
}

// Video: start at a sharper bitrate than Chromium's 300 kbit/s default (it then adapts either way).
export function mungeVideoStart(sdp, kbps = 600) {
  const pts = [...sdp.matchAll(/a=rtpmap:(\d+) (VP8|VP9|H264)\/90000/gi)].map((m) => m[1]);
  let out = sdp;
  for (const pt of pts) {
    const re = new RegExp(`a=fmtp:${pt} ([^\\r\\n]*)`);
    if (re.test(out)) {
      out = out.replace(re, (all, params) => (/x-google-start-bitrate/.test(params) ? all : `${all};x-google-start-bitrate=${kbps}`));
    } else {
      out = out.replace(new RegExp(`(a=rtpmap:${pt} [^\\r\\n]*)`), `$1\r\na=fmtp:${pt} x-google-start-bitrate=${kbps}`);
    }
  }
  return out;
}

const scrubRaddr = (s) => s.replace(/ raddr \S+ rport \d+/g, ' raddr 0.0.0.0 rport 0');

function preferCodecs(transceiver) {
  if (!transceiver || !transceiver.setCodecPreferences || !window.RTCRtpReceiver || !RTCRtpReceiver.getCapabilities) return;
  const caps = RTCRtpReceiver.getCapabilities('video');
  if (!caps) return;
  let first = /Android/i.test(navigator.userAgent) ? 'video/H264' : 'video/VP8';
  try { first = localStorage.getItem('dz:video-codec') || first; } catch { /* ignore */ } // field tests: VP8 / H264
  const rank = (c) => (c.mimeType === first ? 0 : /video\/(VP8|H264|VP9)/.test(c.mimeType) ? 1 : 2);
  try { transceiver.setCodecPreferences([...caps.codecs].sort((a, b) => rank(a) - rank(b))); } catch { /* keep defaults */ }
}

// ------------------------------------------------------------------ tones + wake lock
function makeTone(kind) {
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return () => {};
  let ctx;
  try { ctx = new Ctx(); } catch { return () => {}; }
  const beep = (freq, start, dur) => {
    const o = ctx.createOscillator();
    const g = ctx.createGain();
    o.frequency.value = freq;
    g.gain.setValueAtTime(0.0001, ctx.currentTime + start);
    g.gain.exponentialRampToValueAtTime(0.18, ctx.currentTime + start + 0.03);
    g.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + start + dur);
    o.connect(g).connect(ctx.destination);
    o.start(ctx.currentTime + start);
    o.stop(ctx.currentTime + start + dur + 0.05);
  };
  const play = () => {
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    if (kind === 'ring') { beep(880, 0, 0.35); beep(660, 0.45, 0.35); beep(880, 1.0, 0.35); beep(660, 1.45, 0.35); }
    else beep(425, 0, 1.0);
  };
  play();
  const t = setInterval(play, kind === 'ring' ? 3000 : 4000);
  return () => { clearInterval(t); ctx.close().catch(() => {}); };
}

async function keepAwake(on) {
  try { if (native() && native().keepScreenOn) native().keepScreenOn(!!on); } catch { /* ignore */ }
  try {
    if (on && navigator.wakeLock && !cur.wakeLock) cur.wakeLock = await navigator.wakeLock.request('screen');
    if (!on && cur && cur.wakeLock) { cur.wakeLock.release().catch(() => {}); cur.wakeLock = null; }
  } catch { /* not supported / not visible */ }
}

// ------------------------------------------------------------------ UI
function buildScreen() {
  // Remote picture and remote sound are separate elements: hiding / stopping the video (peer camera off)
  // must never silence the call. The <video> stays muted and in the page; the <audio> plays the voice.
  const remote = h('video', { class: 'call-remote', autoplay: true, playsinline: true, muted: true });
  remote.muted = true;
  const remoteAudio = h('audio', { class: 'call-audio', autoplay: true });
  const self = h('video', { class: 'call-self', autoplay: true, playsinline: true, muted: true });
  self.muted = true;
  const avatarBox = h('div', { class: 'call-face' });
  const name = h('div', { class: 'call-name' });
  const status = h('div', { class: 'call-status', role: 'status', 'aria-live': 'polite' });
  const quality = h('div', { class: 'call-quality', hidden: true, 'aria-label': 'جودة الاتصال' }, h('i'), h('i'), h('i'));
  const banner = h('div', { class: 'call-banner', hidden: true, role: 'status' });
  const controls = h('div', { class: 'call-controls' });
  const el = h('div', { class: 'call-screen', role: 'dialog', 'aria-modal': 'true', 'aria-label': 'مكالمة' },
    remote, remoteAudio, h('div', { class: 'call-shade' }),
    h('div', { class: 'call-top' }, avatarBox, name, h('div', { class: 'call-meta' }, status, quality), banner),
    self, controls);
  makeDraggable(self);
  document.body.append(el);
  document.body.classList.add('in-call');
  return { el, remote, remoteAudio, self, avatarBox, name, status, quality, banner, controls };
}

function makeDraggable(node) {
  let start = null; // { x, y, ox, oy, rect }
  let pos = { x: 0, y: 0 };
  node.addEventListener('pointerdown', (e) => {
    start = { x: e.clientX, y: e.clientY, ox: pos.x, oy: pos.y, rect: node.getBoundingClientRect() };
    node.setPointerCapture(e.pointerId);
  });
  node.addEventListener('pointermove', (e) => {
    if (!start) return;
    const { rect } = start;
    const dx = Math.max(8 - rect.left, Math.min(e.clientX - start.x, window.innerWidth - 8 - rect.right));
    const dy = Math.max(8 - rect.top, Math.min(e.clientY - start.y, window.innerHeight - 8 - rect.bottom));
    pos = { x: start.ox + dx, y: start.oy + dy };
    node.style.transform = `translate(${pos.x}px, ${pos.y}px)`;
  });
  const end = () => { start = null; };
  node.addEventListener('pointerup', end);
  node.addEventListener('pointercancel', end);
}

function ctrl(ic, label, onclick, cls = '') {
  const b = h('button', { class: `call-btn ${cls}`, type: 'button', 'aria-label': label, title: label, onclick }, icon(ic));
  return b;
}

function setStatus(text) { if (cur) cur.ui.status.textContent = text; }

function drawPeer() {
  const p = cur.peer || { name: 'dzplay', anonymous: true };
  cur.ui.avatarBox.replaceChildren(personAvatar(p.name, { size: 'xl', anonymous: p.anonymous }));
  cur.ui.name.replaceChildren(nameLine(p.name, p.gender));
}

function drawVideoState() {
  if (!cur) return;
  const remoteOn = !!cur.remoteVideo;
  const localOn = !!(cur.videoTrack && cur.videoTrack.enabled);
  cur.ui.el.classList.toggle('has-remote-video', remoteOn && cur.mediaUp);
  cur.ui.el.classList.toggle('has-self-video', localOn);
}

function drawControls() {
  if (!cur) return;
  const c = cur.ui.controls;
  if (cur.phase === 'incoming') {
    c.replaceChildren(...[
      h('div', { class: 'call-answer' },
        ctrl('phoneDown', 'رفض', () => decline(), 'call-btn--end'),
        h('span', { text: 'رفض' })),
      h('div', { class: 'call-answer' },
        ctrl(cur.kind === 'video' ? 'video' : 'phone', 'رد', () => acceptIncoming(cur.kind === 'video'), 'call-btn--accept'),
        h('span', { text: 'رد' })),
      cur.kind === 'video'
        ? h('div', { class: 'call-answer' }, ctrl('phone', 'رد بالصوت فقط', () => acceptIncoming(false), 'call-btn--ghost'), h('span', { text: 'صوت فقط' }))
        : null,
    ].filter(Boolean)); // replaceChildren would print "null"

    return;
  }
  const muted = cur.audioTrack && !cur.audioTrack.enabled;
  const camOn = !!(cur.videoTrack && cur.videoTrack.enabled);
  c.replaceChildren(...[
    ctrl(muted ? 'micOff' : 'mic', muted ? 'إلغاء كتم الميكروفون' : 'كتم الميكروفون', toggleMute, muted ? 'is-off' : ''),
    ctrl(camOn ? 'video' : 'videoOff', camOn ? 'إيقاف الكاميرا' : 'تشغيل الكاميرا', toggleCamera, camOn ? '' : 'is-off'),
    camOn ? ctrl('flip', 'تبديل الكاميرا', flipCamera) : null,
    native() && native().setSpeaker ? ctrl('volume', cur.speaker ? 'إيقاف مكبّر الصوت' : 'مكبّر الصوت', toggleSpeaker, cur.speaker ? 'is-on' : '') : null,
    ctrl('more', 'خيارات', callMenu),
    ctrl('phoneDown', 'إنهاء المكالمة', () => hangup('hangup'), 'call-btn--end'),
  ].filter(Boolean));
}

// ------------------------------------------------------------------ peer connection
function attachLocal() {
  cur.audioTrack = cur.local.getAudioTracks()[0] || null;
  cur.videoTrack = cur.local.getVideoTracks()[0] || null;
  cur.ui.self.srcObject = cur.videoTrack ? new MediaStream([cur.videoTrack]) : null;
  drawVideoState();
}

function createPC() {
  const pc = new RTCPeerConnection({
    iceServers: cur.ice.ice_servers, iceTransportPolicy: cur.ice.ice_transport_policy, bundlePolicy: 'max-bundle',
  });
  cur.pc = pc;
  pc.onicecandidate = (e) => {
    if (!e.candidate) return;
    const c = e.candidate.toJSON();
    c.candidate = scrubRaddr(c.candidate || ''); // never send my own public IP (the server scrubs it too)
    sendSignal('call.ice', { candidate: c });
  };
  pc.ontrack = (e) => {
    const stream = cur.remoteStream || (cur.remoteStream = new MediaStream());
    if (!stream.getTracks().includes(e.track)) stream.addTrack(e.track);
    if (e.track.kind === 'audio') {
      cur.ui.remoteAudio.srcObject = new MediaStream([e.track]);
      cur.ui.remoteAudio.play().catch(() => {});
    } else {
      cur.ui.remote.srcObject = new MediaStream([e.track]);
      cur.ui.remote.play().catch(() => {});
    }
  };
  pc.oniceconnectionstatechange = () => onIceState(pc.iceConnectionState);
  return pc;
}

function addOwnTransceivers() {
  const pc = cur.pc;
  cur.audioTx = pc.addTransceiver(cur.audioTrack || 'audio', {
    direction: 'sendrecv', streams: [cur.local], sendEncodings: [{ priority: 'high', networkPriority: 'high' }],
  });
  cur.videoTx = pc.addTransceiver(cur.videoTrack || 'video', {
    direction: 'sendrecv', streams: [cur.local],
    sendEncodings: [{ maxBitrate: LEVELS[0].maxBitrate }],
  });
  preferCodecs(cur.videoTx);
}

async function adoptRemoteTransceivers() {
  // callee: the offer created the transceivers; put our tracks on them (no extra m-lines)
  for (const t of cur.pc.getTransceivers()) {
    const kind = t.receiver.track.kind;
    t.direction = 'sendrecv';
    if (kind === 'audio' && !cur.audioTx) { cur.audioTx = t; if (cur.audioTrack) await t.sender.replaceTrack(cur.audioTrack); }
    if (kind === 'video' && !cur.videoTx) { cur.videoTx = t; preferCodecs(t); if (cur.videoTrack) await t.sender.replaceTrack(cur.videoTrack); }
    try { t.sender.setStreams && t.sender.setStreams(cur.local); } catch { /* old browsers */ }
  }
}

async function tuneSenders() {
  try {
    if (cur.audioTx) {
      const p = cur.audioTx.sender.getParameters();
      if (p.encodings && p.encodings[0]) { p.encodings[0].priority = 'high'; p.encodings[0].networkPriority = 'high'; await cur.audioTx.sender.setParameters(p); }
    }
    await setLevel(cur.level || 0);
  } catch { /* not supported */ }
}

async function setLevel(i) {
  if (!cur || !cur.videoTx) return;
  cur.level = i;
  const L = LEVELS[i];
  try {
    const p = cur.videoTx.sender.getParameters();
    if (!p.encodings || !p.encodings.length) return;
    Object.assign(p.encodings[0], { maxBitrate: L.maxBitrate, scaleResolutionDownBy: L.scale, maxFramerate: L.fps });
    p.degradationPreference = 'balanced';
    await cur.videoTx.sender.setParameters(p);
  } catch { /* the browser keeps its own adaptation */ }
}

async function makeOffer(iceRestart = false) {
  const offer = await cur.pc.createOffer(iceRestart ? { iceRestart: true } : undefined);
  offer.sdp = mungeVideoStart(mungeOpus(offer.sdp));
  await cur.pc.setLocalDescription(offer);
  sendSignal('call.sdp', { sdp: { type: 'offer', sdp: scrubRaddr(cur.pc.localDescription.sdp) } });
}

async function onRemoteSdp(sdp) {
  const pc = cur.pc;
  if (!pc) return;
  if (sdp.type === 'offer') {
    await pc.setRemoteDescription(sdp);
    if (!cur.adopted) { await adoptRemoteTransceivers(); cur.adopted = true; }
    const answer = await pc.createAnswer();
    answer.sdp = mungeVideoStart(mungeOpus(answer.sdp));
    await pc.setLocalDescription(answer);
    sendSignal('call.sdp', { sdp: { type: 'answer', sdp: scrubRaddr(pc.localDescription.sdp) } });
  } else if (sdp.type === 'answer' && pc.signalingState === 'have-local-offer') {
    await pc.setRemoteDescription(sdp);
  }
  for (const c of cur.pendingIce.splice(0)) pc.addIceCandidate(c).catch(() => {});
  tuneSenders();
}

function onRemoteIce(candidate) {
  if (cur.seen.length < 60) cur.seen.push(candidate.candidate || ''); // test / diagnostics: what the peer revealed
  if (!cur.pc || !cur.pc.remoteDescription) { cur.pendingIce.push(candidate); return; }
  cur.pc.addIceCandidate(candidate).catch(() => {});
}

// ------------------------------------------------------------------ connection health
function onIceState(s) {
  if (!cur) return;
  if (s === 'connected' || s === 'completed') {
    clearTimeout(cur.reconnectTimer); cur.reconnectTimer = null;
    clearTimeout(cur.restartTimer);
    cur.ui.banner.hidden = true;
    if (!cur.mediaUp) {
      cur.mediaUp = true;
      cur.connectedAt = Date.now();
      cur.durationTimer = setInterval(() => { if (cur) setStatus(fmt((Date.now() - cur.connectedAt) / 1000)); }, 1000);
      setStatus('0:00');
      drawVideoState();
    }
  } else if (s === 'disconnected' || s === 'failed') {
    reconnecting(s === 'failed');
  }
}

function reconnecting(failedNow) {
  if (!cur) return;
  cur.ui.banner.textContent = 'جارٍ إعادة الاتصال…';
  cur.ui.banner.hidden = false;
  if (!cur.reconnectTimer) {
    cur.reconnectTimer = setTimeout(() => hangup('reconnect_timeout', 'انقطع الاتصال.'), (cur.ice.reconnect_timeout || 20) * 1000);
  }
  // the caller (offerer) restarts ICE; a short grace first: "disconnected" often heals by itself
  if (cur.outgoing) {
    clearTimeout(cur.restartTimer);
    cur.restartTimer = setTimeout(() => {
      if (cur && cur.pc && !['connected', 'completed'].includes(cur.pc.iceConnectionState)) makeOffer(true).catch(() => {});
    }, failedNow ? 0 : 2500);
  }
}

function onOnline() { if (cur && cur.pc && cur.outgoing && cur.phase === 'active') makeOffer(true).catch(() => {}); }

async function sampleStats() {
  if (!cur || !cur.pc) return;
  let report;
  try { report = await cur.pc.getStats(); } catch { return; }
  const now = Date.now();
  let pair = null;
  const out = {};
  const inn = {};
  let remoteLoss = null;
  const localTypes = new Set();
  let dtls = null;
  report.forEach((r) => {
    if (r.type === 'local-candidate') localTypes.add(r.candidateType);
    if (r.type === 'transport') dtls = { state: r.dtlsState, cipher: r.srtpCipher || null };
    if (r.type === 'transport' && r.selectedCandidatePairId) pair = report.get(r.selectedCandidatePairId) || pair;
    if (r.type === 'candidate-pair' && r.nominated && r.state === 'succeeded' && !pair) pair = r;
    if (r.type === 'outbound-rtp' && !r.isRemote) out[r.kind] = r;
    if (r.type === 'inbound-rtp' && !r.isRemote) inn[r.kind] = r;
    if (r.type === 'remote-inbound-rtp' && r.kind === 'video' && typeof r.fractionLost === 'number') remoteLoss = r.fractionLost;
    if (r.type === 'remote-inbound-rtp' && r.kind === 'audio' && remoteLoss === null && typeof r.fractionLost === 'number') remoteLoss = r.fractionLost;
  });
  const prev = cur.prevStats || {};
  const dt = prev.t ? (now - prev.t) / 1000 : 0;
  const bytesOut = (out.audio ? out.audio.bytesSent : 0) + (out.video ? out.video.bytesSent : 0);
  const bytesIn = (inn.audio ? inn.audio.bytesReceived : 0) + (inn.video ? inn.video.bytesReceived : 0);
  const recv = (inn.audio ? inn.audio.packetsReceived : 0) + (inn.video ? inn.video.packetsReceived : 0);
  const lost = (inn.audio ? inn.audio.packetsLost : 0) + (inn.video ? inn.video.packetsLost : 0);
  const dRecv = recv - (prev.recv || 0);
  const dLost = Math.max(0, lost - (prev.lost || 0));
  const s = {
    rtt_ms: pair && typeof pair.currentRoundTripTime === 'number' ? Math.round(pair.currentRoundTripTime * 1000) : null,
    kbps_out: dt ? Math.round(((bytesOut - (prev.bytesOut || 0)) * 8) / dt / 1000) : null,
    kbps_in: dt ? Math.round(((bytesIn - (prev.bytesIn || 0)) * 8) / dt / 1000) : null,
    loss_pct: dRecv + dLost > 0 ? Math.round((dLost / (dRecv + dLost)) * 1000) / 10 : 0,
    out_loss_pct: remoteLoss === null ? null : Math.round(remoteLoss * 1000) / 10,
    jitter_ms: inn.audio && typeof inn.audio.jitter === 'number' ? Math.round(inn.audio.jitter * 1000) : null,
    available_kbps: pair && pair.availableOutgoingBitrate ? Math.round(pair.availableOutgoingBitrate / 1000) : null,
    height: out.video && out.video.frameHeight ? out.video.frameHeight : null,
    remote_height: inn.video && inn.video.frameHeight ? inn.video.frameHeight : null,
    relay: null,
    local_type: null,
    remote_type: null,
    remote_address: null,
    codec_audio: null,
    codec_video: null,
    local_candidate_types: [...localTypes],
    audio_bytes_in: inn.audio ? inn.audio.bytesReceived : null,
    limited_by: out.video ? out.video.qualityLimitationReason || null : null, // cpu | bandwidth | none
    fps: out.video && out.video.framesPerSecond ? Math.round(out.video.framesPerSecond) : null,
    dtls_state: dtls ? dtls.state : null,
    srtp_cipher: dtls ? dtls.cipher : null,
  };
  if (pair) {
    const lc = report.get(pair.localCandidateId);
    const rc = report.get(pair.remoteCandidateId);
    s.local_type = lc ? lc.candidateType : null;
    s.remote_type = rc ? rc.candidateType : null;
    s.remote_address = rc ? (rc.address || rc.ip || null) : null;
    s.relay = s.local_type === 'relay';
  }
  const codec = (r) => (r && r.codecId && report.get(r.codecId) ? report.get(r.codecId).mimeType.split('/')[1] : null);
  s.codec_audio = codec(out.audio);
  s.codec_video = codec(out.video);
  cur.prevStats = { t: now, bytesOut, bytesIn, recv, lost };
  cur.lastStats = s;
  adapt(s);
  drawQuality(s);
  if (!cur.lastReport || now - cur.lastReport >= (cur.ice.quality_report_seconds || 10) * 1000) {
    cur.lastReport = now;
    sendSignal('call.quality', {
      stats: Object.fromEntries(Object.entries({
        rtt_ms: s.rtt_ms, loss_pct: s.loss_pct, jitter_ms: s.jitter_ms, kbps_out: s.kbps_out, kbps_in: s.kbps_in,
        relay: s.relay, codec_audio: s.codec_audio, codec_video: s.codec_video, height: s.height,
      }).filter(([, v]) => v !== null && v !== undefined)),
    });
  }
}

function adapt(s) {
  // The browser's congestion control already moves the bitrate inside maxBitrate. This ladder only
  // changes resolution / frame rate on real distress (loss or delay), never on the start-up bandwidth
  // estimate (it is low while ramping up and cannot rise above the cap we set).
  if (!cur.videoTrack || !cur.videoTrack.enabled || !cur.videoTx) return;
  const loss = s.out_loss_pct ?? s.loss_pct ?? 0;
  const rtt = s.rtt_ms ?? 0;
  const starved = s.available_kbps && cur.mediaUp && Date.now() - cur.connectedAt > 15000
    && s.available_kbps * 1000 < LEVELS[cur.level || 0].maxBitrate * 0.35;
  const bad = loss > 8 || rtt > 450 || starved;
  const good = loss < 2 && rtt < 250;
  cur.bad = bad ? (cur.bad || 0) + 1 : 0;
  cur.good = good ? (cur.good || 0) + 1 : 0;
  if (cur.bad >= 2 && cur.level < LEVELS.length - 1) { setLevel(cur.level + 1); cur.bad = 0; cur.good = 0; }
  else if (cur.good >= 5 && cur.level > 0) { setLevel(cur.level - 1); cur.good = 0; }
}

function drawQuality(s) {
  const q = cur.ui.quality;
  const loss = Math.max(s.loss_pct || 0, s.out_loss_pct || 0);
  const rtt = s.rtt_ms || 0;
  const level = rtt < 250 && loss < 3 ? 3 : rtt < 500 && loss < 10 ? 2 : 1;
  q.hidden = !cur.mediaUp;
  q.dataset.level = String(level);
  q.title = level === 3 ? 'اتصال ممتاز' : level === 2 ? 'اتصال متوسط' : 'اتصال ضعيف';
}

// ------------------------------------------------------------------ controls
function toggleMute() {
  if (!cur.audioTrack) return;
  cur.audioTrack.enabled = !cur.audioTrack.enabled;
  drawControls();
}

async function toggleCamera() {
  if (cur.videoTrack && cur.videoTrack.enabled) {
    cur.videoTrack.enabled = false;
    sendSignal('call.media', { video: false });
  } else {
    if (!cur.videoTrack) {
      try {
        const s = await navigator.mediaDevices.getUserMedia({ video: videoConstraints(cur.facing) });
        cur.videoTrack = s.getVideoTracks()[0];
        cur.local.addTrack(cur.videoTrack);
        if (cur.videoTx) await cur.videoTx.sender.replaceTrack(cur.videoTrack);
        cur.ui.self.srcObject = new MediaStream([cur.videoTrack]);
        setLevel(cur.level || 0);
      } catch (err) { toast(mediaError(err, true), 'error', 5000); return; }
    }
    cur.videoTrack.enabled = true;
    sendSignal('call.media', { video: true });
    if (native() && native().setSpeaker && !cur.speakerTouched) { cur.speaker = true; native().setSpeaker(true); }
  }
  drawVideoState();
  drawControls();
}

async function flipCamera() {
  if (!cur.videoTrack) return;
  cur.facing = cur.facing === 'environment' ? 'user' : 'environment';
  try {
    const s = await navigator.mediaDevices.getUserMedia({ video: videoConstraints(cur.facing) });
    const track = s.getVideoTracks()[0];
    if (cur.videoTx) await cur.videoTx.sender.replaceTrack(track);
    cur.local.removeTrack(cur.videoTrack);
    cur.videoTrack.stop();
    cur.videoTrack = track;
    cur.local.addTrack(track);
    cur.ui.self.srcObject = new MediaStream([track]);
    cur.ui.self.classList.toggle('is-rear', cur.facing === 'environment');
  } catch (err) { toast(mediaError(err, true), 'error'); }
}

function toggleSpeaker() {
  cur.speaker = !cur.speaker;
  cur.speakerTouched = true;
  try { native().setSpeaker(cur.speaker); } catch { /* ignore */ }
  drawControls();
}

function callMenu() {
  const call = cur;
  sheet((panel, close) => {
    panel.append(
      h('h2', { text: 'خيارات المكالمة' }),
      h('div', { class: 'actions' },
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: () => { close(); reportCall(call); } }, icon('flag'), 'إبلاغ عن المكالمة'),
        h('button', { class: 'btn btn--danger btn--block', type: 'button', onclick: () => { close(); blockFromCall(call); } }, icon('block'), 'حظر هذا الشخص'),
        h('button', { class: 'btn btn--ghost btn--block', type: 'button', onclick: close }, 'إلغاء'),
      ));
  });
}

function reportCall(call) {
  sheet((panel, close) => {
    const details = h('textarea', { class: 'input', rows: '3', maxlength: '500', placeholder: 'تفاصيل إضافية (اختياري)' });
    const alsoBlock = h('input', { type: 'checkbox', checked: true });
    const choices = REPORT_REASONS.map(([value, label], i) =>
      h('label', { class: 'choice' }, h('input', { type: 'radio', name: 'call-reason', value, checked: i === 0 }), h('span', { text: label })));
    const submit = h('button', { class: 'btn btn--primary btn--block', type: 'button' }, 'إرسال البلاغ');
    submit.addEventListener('click', async () => {
      submit.disabled = true;
      try {
        await api.post(`/api/calls/${encodeURIComponent(call.id)}/report`, {
          reason: panel.querySelector('input[name="call-reason"]:checked')?.value, details: details.value.trim() || null,
        });
        close();
        if (alsoBlock.checked) await blockNow(call);
        toast(alsoBlock.checked ? 'شكرًا لك. تم إرسال البلاغ وحظر هذا الشخص.' : 'شكرًا لك. تم إرسال البلاغ.');
      } catch (err) { toast(err.message, 'error'); submit.disabled = false; }
    });
    panel.append(h('h2', { text: 'الإبلاغ عن المكالمة' }),
      h('p', { text: 'المكالمات لا تُسجَّل. نحتفظ مع البلاغ بوقت المكالمة ومدتها وآخر الرسائل المكتوبة فقط.' }),
      ...choices, h('div', { class: 'field' }, details),
      h('label', { class: 'choice' }, alsoBlock, h('span', { text: 'احظر هذا الشخص أيضًا (تنتهي المكالمة)' })),
      h('div', { class: 'actions' }, submit));
  });
}

async function blockNow(call) {
  if (!call.conversationId) return;
  await api.post(`/api/conversations/${encodeURIComponent(call.conversationId)}/block`); // the server ends the call
  store.removeConversationLocally(call.conversationId);
  if (cur === call) teardown('انتهت المكالمة.');
}

async function blockFromCall(call) {
  const ok = await confirmSheet({
    title: 'حظر هذا الشخص؟', confirm: 'حظر', danger: true,
    text: 'تنتهي المكالمة فورًا، ولن يتمكن من مراسلتك أو الاتصال بك مجددًا.',
  });
  if (ok) { try { await blockNow(call); toast('تم الحظر.'); } catch (err) { toast(err.message, 'error'); } }
}

// ------------------------------------------------------------------ lifecycle
function newCall(call, phase) {
  cur = {
    id: call.id, conversationId: call.conversation_id, kind: call.kind, outgoing: !!call.outgoing, peer: call.peer,
    phase, outbox: [], pendingIce: [], seen: [], level: 0, facing: 'user', speaker: false, ui: buildScreen(),
  };
  drawPeer();
  drawControls();
  stopPrefetch(); // the call gets all the bandwidth
  keepAwake(true);
  return cur;
}

/** Start a call from a chat (window.dzCalls.start). */
export async function start(conversationId, kind) {
  if (cur) { toast('لديك مكالمة جارية.'); return; }
  const me = store.state.me || {};
  if (me.age_confirmed === false && !(await confirmAge())) return;
  if (!navigator.mediaDevices || !window.RTCPeerConnection) { toast('هذا المتصفح لا يدعم المكالمات.', 'error'); return; }
  if (!(await explainPermissions(kind === 'video'))) return;
  let local;
  try { local = await getMedia(kind === 'video'); } catch (err) { toast(mediaError(err, kind === 'video'), 'error', 6000); return; }
  let ice;
  let res;
  try {
    ice = await api.get('/api/calls/ice');
    res = await api.post('/api/calls', { conversation_id: conversationId, kind });
  } catch (err) {
    local.getTracks().forEach((t) => t.stop());
    toast(err.message, 'error', 5000);
    return;
  }
  const c = newCall(res.call, 'outgoing');
  c.local = local;
  c.ice = ice;
  attachLocal();
  if (native() && native().setSpeaker) { c.speaker = kind === 'video'; native().setSpeaker(c.speaker); }
  if (res.call.state === 'busy') { finish('busy'); return; }
  setStatus('جارٍ الاتصال…');
  c.stopTone = makeTone('ringback');
  createPC();
  addOwnTransceivers();
}

function showIncoming(call) {
  if (cur) return;
  const c = newCall(call, 'incoming');
  c.ui.el.classList.add('is-incoming');
  setStatus(call.kind === 'video' ? 'مكالمة فيديو واردة على DZPLAY' : 'مكالمة صوتية واردة على DZPLAY');
  c.stopTone = makeTone('ring');
  if (navigator.vibrate) { navigator.vibrate([600, 400, 600]); c.vibrate = setInterval(() => navigator.vibrate([600, 400, 600]), 2500); }
  sendSignal('call.ringing');
  // answered from the Android ring notification ("رد"): pick it up right away
  try {
    const answered = native() && native().takePendingAnswer ? native().takePendingAnswer() : '';
    if (answered && answered === call.id) { setTimeout(() => acceptIncoming(call.kind === 'video'), 300); return; }
  } catch { /* ignore */ }
  if (document.visibilityState !== 'visible' && 'Notification' in window && Notification.permission === 'granted') {
    navigator.serviceWorker?.ready.then((reg) => reg.showNotification('DZPLAY', {
      body: 'مكالمة واردة على DZPLAY', tag: 'dz-call', renotify: true, requireInteraction: true, data: { url: '/#/messages' },
    })).catch(() => {});
  }
}

function stopRinging() {
  if (!cur) return;
  if (cur.stopTone) { cur.stopTone(); cur.stopTone = null; }
  if (cur.vibrate) { clearInterval(cur.vibrate); cur.vibrate = null; if (navigator.vibrate) navigator.vibrate(0); }
}

async function acceptIncoming(withVideo) {
  if (!cur || cur.phase !== 'incoming') return;
  const c = cur;
  stopRinging();
  c.phase = 'accepting';
  c.ui.el.classList.remove('is-incoming');
  setStatus('جارٍ الاتصال…');
  drawControls();
  const me = store.state.me || {};
  if (me.age_confirmed === false && !(await confirmAge())) { decline(); return; }
  if (!(await explainPermissions(withVideo))) { decline(); return; }
  try {
    c.local = await getMedia(withVideo);
    c.ice = await api.get('/api/calls/ice');
  } catch (err) {
    toast(err && err.name ? mediaError(err, withVideo) : (err.message || 'تعذّر الرد.'), 'error', 6000);
    decline();
    return;
  }
  if (cur !== c) { c.local.getTracks().forEach((t) => t.stop()); return; }
  attachLocal();
  if (native() && native().setSpeaker) { c.speaker = withVideo; native().setSpeaker(withVideo); }
  createPC();
  c.phase = 'active';
  drawControls();
  if (!sendSignal('call.accept', { device: DEVICE })) {
    c.outbox = c.outbox.filter((m) => m.type !== 'call.accept');
    api.post(`/api/calls/${encodeURIComponent(c.id)}/accept`, { device: DEVICE }).catch((err) => finish('failed', err.message));
  }
}

function decline() {
  if (!cur) return;
  if (!sendSignal('call.decline')) api.post(`/api/calls/${encodeURIComponent(cur.id)}/decline`).catch(() => {});
  teardown('رفضت المكالمة.');
}

function hangup(reason = 'hangup', text = null) {
  if (!cur) return;
  const c = cur;
  if (!sendSignal('call.hangup', { reason })) {
    api.post(`/api/calls/${encodeURIComponent(c.id)}/hangup`, { reason }).catch(() => {});
  }
  const dur = c.connectedAt ? fmt((Date.now() - c.connectedAt) / 1000) : null;
  teardown(text || (c.phase === 'outgoing' ? 'أُلغيت المكالمة.' : dur ? `انتهت المكالمة · ${dur}` : 'انتهت المكالمة.'));
}

const END_TEXT = {
  missed: (c) => (c.outgoing ? 'لم يرد.' : 'مكالمة فائتة.'),
  declined: (c) => (c.outgoing ? 'رُفضت المكالمة.' : 'رفضت المكالمة.'),
  busy: () => 'الطرف الآخر في مكالمة أخرى.',
  canceled: (c) => (c.outgoing ? 'أُلغيت المكالمة.' : 'مكالمة فائتة.'),
  failed: () => 'تعذّر الاتصال.',
  ended: (c, d) => (d ? `انتهت المكالمة · ${fmt(d)}` : 'انتهت المكالمة.'),
};

function finish(state, reason = null, duration = null) {
  if (!cur) return;
  const d = duration ?? (cur.connectedAt ? (Date.now() - cur.connectedAt) / 1000 : null);
  const text = reason === 'blocked' ? 'انتهت المكالمة.' : (END_TEXT[state] || END_TEXT.ended)(cur, d);
  teardown(text);
}

function teardown(text) {
  const c = cur;
  if (!c) return;
  cur = null;
  if (c.stopTone) c.stopTone();
  if (c.vibrate) { clearInterval(c.vibrate); if (navigator.vibrate) navigator.vibrate(0); }
  clearInterval(c.durationTimer);
  clearInterval(c.statsTimer);
  clearTimeout(c.reconnectTimer);
  clearTimeout(c.restartTimer);
  try { c.pc && c.pc.close(); } catch { /* ignore */ }
  if (c.local) c.local.getTracks().forEach((t) => t.stop());
  if (c.wakeLock) c.wakeLock.release().catch(() => {});
  try {
    if (native()) {
      if (native().keepScreenOn) native().keepScreenOn(false);
      if (native().endCallAudio) native().endCallAudio(); // back to normal audio (Reels on the loudspeaker)
      else if (native().setSpeaker) native().setSpeaker(false);
    }
  } catch { /* ignore */ }
  c.ui.status.textContent = text;
  c.ui.banner.hidden = true;
  c.ui.controls.replaceChildren();
  c.ui.el.classList.add('is-ended');
  setTimeout(() => {
    c.ui.el.remove();
    if (!document.querySelector('.call-screen')) document.body.classList.remove('in-call');
  }, 1800);
  startPrefetch(store.state.config);
  store.scheduleSync(300); // the call summary appears in the chat
}

// ------------------------------------------------------------------ events from the server
async function onEvent(msg) {
  if (msg.type === 'call.incoming') { showIncoming(msg.call); return; }
  if (!cur || msg.call_id !== cur.id) return;
  try {
    if (msg.type === 'call.state') {
      if (msg.state === 'ringing' && cur.phase === 'outgoing') setStatus('يرن…');
      else if (msg.state === 'connected') {
        if (cur.outgoing && cur.phase === 'outgoing') {
          if (cur.stopTone) { cur.stopTone(); cur.stopTone = null; }
          cur.phase = 'active';
          setStatus('جارٍ الاتصال…');
          drawControls();
          await makeOffer();
        } else if (!cur.outgoing && msg.device && msg.device !== DEVICE && cur.phase === 'incoming') {
          stopRinging();
          teardown('تم الرد من جهاز آخر.');
          return;
        }
        if (!cur.statsTimer) cur.statsTimer = setInterval(sampleStats, STATS_MS);
      } else if (['ended', 'declined', 'missed', 'busy', 'failed', 'canceled'].includes(msg.state)) {
        finish(msg.state, msg.reason, msg.duration);
      }
    } else if (msg.type === 'call.sdp') {
      await onRemoteSdp(msg.sdp);
    } else if (msg.type === 'call.ice') {
      onRemoteIce(msg.candidate);
    } else if (msg.type === 'call.media') {
      cur.remoteVideo = !!msg.video;
      drawVideoState();
    }
  } catch (err) {
    console.warn('call signaling', err);
  }
}

function watchRemoteVideo() {
  // remote camera on / off is also visible from the track itself (covers the first frame)
  setInterval(() => {
    if (!cur || !cur.remoteStream) return;
    const v = cur.remoteStream.getVideoTracks()[0];
    const live = !!(v && !v.muted && v.readyState === 'live' && cur.ui.remote.videoWidth > 0);
    if (live !== !!cur.remoteVideo) { cur.remoteVideo = live; drawVideoState(); }
  }, 1000);
}

/** Debug view for tests / field diagnostics (only this device's own numbers). */
function debugState() {
  if (!cur) return null;
  return {
    id: cur.id, phase: cur.phase, mediaUp: !!cur.mediaUp, level: LEVELS[cur.level || 0].label, stats: cur.lastStats || null,
    ice: cur.pc ? cur.pc.iceConnectionState : null, policy: cur.ice ? cur.ice.ice_transport_policy : null,
    remoteVideo: !!cur.remoteVideo, selfVideo: !!(cur.videoTrack && cur.videoTrack.enabled),
    audioIn: cur.lastStats ? cur.lastStats.audio_bytes_in : null,
    audioPlaying: !!(cur.ui.remoteAudio.srcObject && !cur.ui.remoteAudio.paused),
    muted: !!(cur.audioTrack && !cur.audioTrack.enabled),
    remoteCandidates: cur.seen.slice(),
    remoteSdpCandidates: cur.pc && cur.pc.remoteDescription ? cur.pc.remoteDescription.sdp.split('\r\n').filter((l) => l.startsWith('a=candidate:')) : [],
    capture: cur.videoTrack && cur.videoTrack.getSettings ? (({ width, height, frameRate }) => ({ width, height, frameRate }))(cur.videoTrack.getSettings()) : null,
  };
}

let started = false;
export function initCalls() {
  if (started) return;
  started = true;
  window.dzCalls = { start, debug: debugState };
  store.subscribe((type, detail) => {
    if (type === 'ws') onEvent(detail);
    else if (type === 'connection' && store.state.connected) flushOutbox();
  });
  window.addEventListener('online', onOnline);
  window.addEventListener('pagehide', () => {
    if (!cur) return;
    const path = cur.phase === 'incoming' ? 'decline' : 'hangup';
    fetch(`/api/calls/${encodeURIComponent(cur.id)}/${path}`, {
      method: 'POST', keepalive: true, credentials: 'same-origin', body: '{}',
      headers: { 'Content-Type': 'application/json', 'X-DZ-Requested': '1' }, // the API's CSRF guard
    }).catch(() => {});
  });
  watchRemoteVideo();
  checkActive(true);
  // Back in the foreground (e.g. opened from the ring notification): a call may be ringing for me.
  document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') checkActive(false); });
}

function checkActive(firstLoad) {
  if (cur) return;
  api.get('/api/calls/active').then(({ call }) => {
    if (!call || cur) return;
    if (!call.outgoing && (call.state === 'calling' || call.state === 'ringing')) showIncoming(call);
    else if (firstLoad) api.post(`/api/calls/${encodeURIComponent(call.id)}/hangup`, { reason: 'failed' }).catch(() => {}); // this page lost it
  }).catch(() => {});
}
