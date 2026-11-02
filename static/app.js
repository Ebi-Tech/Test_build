/* Beyond Sale — recorder UI. Plain JS, no framework, no build step. */

import { ChunkQueue } from './queue.js';

const CONSENT_KEY = 'bts.consent';
const CREDS_KEY = 'bts.creds';
const HEARTBEAT_MS = 30000;

const $ = (id) => document.getElementById(id);

const state = {
  cfg: null,
  shopId: null,
  token: null,
  sessionId: null,
  recorder: null,
  stream: null,
  audioCtx: null,
  analyser: null,
  levelBuffer: [],
  levelTimer: null,
  chunkIndex: 0,
  nextChunkStart: 0,
  sessionStart: 0,
  wakeLock: null,
  heartbeatTimer: null,
  retryTimer: null,
  flushTimer: null,
  queue: null,
  pendingAdds: [],
  status: 'idle',
  online: navigator.onLine,
};

/* ------------------------------------------------------------------ setup */

async function boot() {
  state.cfg = await fetch('/config', { cache: 'no-store' }).then((r) => r.json());
  $('consent-text').textContent = state.cfg.consent_text;

  const creds = readJson(CREDS_KEY);
  if (creds?.shop_id && creds?.token) {
    state.shopId = creds.shop_id;
    state.token = creds.token;
    showShopName();
  }

  state.queue = new ChunkQueue({
    fetchImpl: (...args) => fetch(...args),
    endpoint: '/upload',
  });
  state.queue.onChange = (pending) => {
    $('queued').textContent = String(pending);
  };

  if (!readJson(CONSENT_KEY)) {
    $('consent').classList.remove('hidden');
    return;
  }
  afterConsent();
}

function afterConsent() {
  $('consent').classList.add('hidden');
  if (!state.shopId || !state.token) {
    $('setup').classList.remove('hidden');
    const select = $('setup-shop');
    select.innerHTML = '';
    for (const id of state.cfg.shop_ids || []) {
      const option = document.createElement('option');
      option.value = id;
      option.textContent = id;
      select.appendChild(option);
    }
    return;
  }
  $('setup').classList.add('hidden');
  $('recorder').classList.remove('hidden');
  setStatus(state.online ? 'idle' : 'offline');
  state.queue.count().then((n) => ($('queued').textContent = String(n)));
  addEventListener('online', onOnline);
  addEventListener('offline', onOffline);
  document.addEventListener('visibilitychange', onVisibility);
  state.flushTimer = setInterval(() => flushQueue(), 15000);
  if (!state.online) onOffline();
}

function showShopName() {
  $('shop-name').textContent = state.shopId;
}

/* --------------------------------------------------------------- recording */

async function startRecording() {
  if (state.recorder) return;
  setStatus('uploading', 'starting mic…');
  try {
    // Ask for the mic first: no server session is created if permission is denied.
    await ensureStream();
    await ensureSession();
    const mimeType = pickMimeType();
    const recorder = new MediaRecorder(state.stream, {
      mimeType,
      audioBitsPerSecond: 16000, // Opus ~16 kbps (browsers may use more)
    });

    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) handleChunk(event.data);
    };
    recorder.onstop = () => logEvent('recorder_stopped', { chunks: state.chunkIndex });
    recorder.onerror = (event) => logEvent('recorder_stopped', { error: String(event.error || 'unknown') });

    startLevelMonitor();
    state.recorder = recorder;
    state.chunkIndex = 0;
    state.nextChunkStart = 0;
    state.sessionStart = performance.now();
    recorder.start(Number(state.cfg.chunk_seconds || 30) * 1000);
    $('pause').disabled = false;
    $('start').disabled = true;
    setStatus(state.online ? 'recording' : 'offline');
    startHeartbeat();
    await requestWakeLock();
    logEvent('recorder_started', { mime_type: mimeType });
  } catch (error) {
    setStatus('idle', `mic error: ${error.message || error}`);
    logEvent('recorder_stopped', { error: String(error) });
  }
}

function pauseRecording() {
  if (!state.recorder || state.recorder.state !== 'recording') return;
  state.recorder.pause(); // truly stops capturing, unlike muting
  state.pausedAt = performance.now();
  $('start').disabled = false;
  $('pause').disabled = true;
  setStatus('paused');
  logEvent('recorder_paused', {});
}

async function resumeRecording() {
  if (!state.recorder || state.recorder.state !== 'paused') return;
  const pausedFor = performance.now() - (state.pausedAt || performance.now());
  state.nextChunkStart += pausedFor;
  state.recorder.resume();
  $('start').disabled = true;
  $('pause').disabled = false;
  setStatus(state.online ? 'recording' : 'offline');
  await requestWakeLock();
  logEvent('recorder_resumed', {});
}

async function stopRecording() {
  const recorder = state.recorder;
  if (!recorder) return;
  state.recorder = null;
  $('start').disabled = false;
  $('pause').disabled = true;

  // Wait for the final dataavailable/stop events before closing the session.
  await new Promise((resolve) => {
    recorder.addEventListener('stop', resolve, { once: true });
    try {
      recorder.stop();
    } catch {
      resolve();
    }
  });

  stopLevelMonitor();
  releaseWakeLock();
  stopHeartbeat();
  if (state.stream) {
    state.stream.getTracks().forEach((track) => track.stop());
    state.stream = null;
  }

  setStatus(state.online ? 'uploading' : 'offline', 'finishing…');
  await Promise.all(state.pendingAdds);
  await flushQueue();
  await post('/session/end', { shop_id: state.shopId, token: state.token, session_id: state.sessionId });
  state.sessionId = null;
  setStatus(state.online ? 'idle' : 'offline');
}

async function ensureSession() {
  if (state.sessionId) return state.sessionId;
  const result = await post('/session/start', { shop_id: state.shopId, token: state.token });
  if (!result?.session_id) throw new Error(result?.detail || 'could not start session');
  state.sessionId = result.session_id;
  return state.sessionId;
}

async function ensureStream() {
  if (state.stream) return state.stream;
  state.stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      channelCount: 1,
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
    },
  });
  return state.stream;
}

function pickMimeType() {
  const candidates = [
    'audio/webm;codecs=opus',
    'audio/ogg;codecs=opus',
    'audio/webm',
    'audio/mp4',
  ];
  return candidates.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

/* ------------------------------------------------------------------ chunks */

function handleChunk(blob) {
  const chunkIndex = state.chunkIndex++;
  const startTimestamp = state.nextChunkStart;
  state.nextChunkStart = performance.now() - state.sessionStart;
  const levelDb = averageLevel();
  state.levelBuffer = [];

  const threshold = state.cfg.silence_threshold_db;
  const silent = threshold != null && $('skip-silent').checked && levelDb < threshold;
  if (silent) {
    // Skipped on purpose, and recorded as an event so it is not a silent loss.
    logEvent('silent_chunk_skipped', { chunk_index: chunkIndex, level_db: levelDb });
    return;
  }

  const addition = state.queue
    .add({
      shop_id: state.shopId,
      token: state.token,
      session_id: state.sessionId,
      chunk_index: chunkIndex,
      start_timestamp: startTimestamp,
      level_db: levelDb,
      blob,
    })
    .then(() => flushQueue())
    .catch((error) => logEvent('queue_error', { error: String(error) }));
  state.pendingAdds.push(addition);
  addition.finally(() => {
    state.pendingAdds = state.pendingAdds.filter((promise) => promise !== addition);
  });
}

async function flushQueue() {
  if (!state.queue || !state.online) return;
  const wasRecording = state.recorder?.state === 'recording';
  if (wasRecording) setStatus('uploading');
  const result = await state.queue.flush();
  $('uploaded').textContent = String(Number($('uploaded').textContent) + result.uploaded);
  if (state.recorder) {
    setStatus(state.recorder.state === 'paused' ? 'paused' : state.online ? 'recording' : 'offline');
  } else {
    setStatus(state.online ? 'idle' : 'offline');
  }
  if (result.failed > 0) scheduleRetry();
}

async function scheduleRetry() {
  if (state.retryTimer) return;
  const items = await state.queue.all();
  if (!items.length) return;
  const attempts = Math.max(1, ...items.map((item) => item.attempts || 0));
  const delay = state.queue.backoffDelay(attempts);
  state.retryTimer = setTimeout(async () => {
    state.retryTimer = null;
    if (!state.online) return;
    const result = await state.queue.flush();
    $('uploaded').textContent = String(Number($('uploaded').textContent) + result.uploaded);
    if (result.failed > 0) scheduleRetry();
  }, delay);
}

/* ------------------------------------------------------------- status light */

function setStatus(kind, detail) {
  state.status = kind;
  document.body.dataset.status = kind;
  const light = $('light');
  light.className = `light ${kind}`;
  const labels = {
    idle: 'ready',
    recording: 'recording',
    paused: 'paused',
    uploading: 'uploading',
    offline: 'offline',
  };
  $('status-text').textContent = detail || labels[kind] || kind;
}

/* -------------------------------------------------------------- heartbeat */

function startHeartbeat() {
  stopHeartbeat();
  state.heartbeatTimer = setInterval(() => logEvent('heartbeat', {}), HEARTBEAT_MS);
  logEvent('heartbeat', { first: true });
}

function stopHeartbeat() {
  if (state.heartbeatTimer) clearInterval(state.heartbeatTimer);
  state.heartbeatTimer = null;
}

async function logEvent(type, meta) {
  if (!state.sessionId) return;
  await post('/events', {
    shop_id: state.shopId,
    token: state.token,
    session_id: state.sessionId,
    type,
    at: Date.now() / 1000,
    meta,
  });
}

/* -------------------------------------------------------------- wake lock */

async function requestWakeLock() {
  if (!('wakeLock' in navigator) || !state.recorder) return;
  try {
    state.wakeLock = await navigator.wakeLock.request('screen');
    state.wakeLock.addEventListener('release', () => {
      state.wakeLock = null;
      logEvent('wake_lock_lost', {});
      // Re-acquire only when the page is actually visible again.
      if (state.recorder && document.visibilityState === 'visible') requestWakeLock();
    });
  } catch (error) {
    logEvent('wake_lock_lost', { error: String(error) });
  }
}

function releaseWakeLock() {
  if (state.wakeLock) {
    state.wakeLock.release().catch(() => {});
    state.wakeLock = null;
  }
}

/* ---------------------------------------------------------------- network */

function onOnline() {
  state.online = true;
  logEvent('online', {});
  if (state.recorder) setStatus(state.recorder.state === 'paused' ? 'paused' : 'recording');
  flushQueue();
}

function onOffline() {
  state.online = false;
  logEvent('offline', {});
  setStatus('offline');
}

function onVisibility() {
  if (document.visibilityState === 'hidden') {
    logEvent('visibility_hidden', {});
  } else {
    logEvent('visibility_visible', {});
    if (state.recorder && document.visibilityState === 'visible') requestWakeLock();
    flushQueue();
  }
}

/* ------------------------------------------------------------------ audio */

function startLevelMonitor() {
  if (!state.stream) return;
  const Ctx = window.AudioContext || window.webkitAudioContext;
  if (!Ctx) return;
  state.audioCtx = new Ctx();
  const source = state.audioCtx.createMediaStreamSource(state.stream);
  state.analyser = state.audioCtx.createAnalyser();
  state.analyser.fftSize = 2048;
  source.connect(state.analyser);
  const buffer = new Float32Array(state.analyser.fftSize);
  state.levelTimer = setInterval(() => {
    state.analyser.getFloatTimeDomainData(buffer);
    let sum = 0;
    for (let i = 0; i < buffer.length; i += 1) sum += buffer[i] * buffer[i];
    const rms = Math.sqrt(sum / buffer.length);
    const db = rms > 0 ? 20 * Math.log10(rms) : -100;
    state.levelBuffer.push(db);
    $('level').style.width = `${Math.max(0, Math.min(100, (db + 60) * 1.6))}%`;
  }, 250);
}

function stopLevelMonitor() {
  if (state.levelTimer) clearInterval(state.levelTimer);
  state.levelTimer = null;
  if (state.audioCtx) state.audioCtx.close().catch(() => {});
  state.audioCtx = null;
  state.analyser = null;
  state.levelBuffer = [];
}

function averageLevel() {
  if (!state.levelBuffer.length) return -100;
  const sum = state.levelBuffer.reduce((a, b) => a + b, 0);
  return Math.round((sum / state.levelBuffer.length) * 10) / 10;
}

/* ------------------------------------------------------------------ utils */

async function post(path, body) {
  try {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const detail = await response.json().catch(() => ({}));
      return { error: response.status, detail: detail.detail };
    }
    return await response.json();
  } catch (error) {
    return { error: 'network', detail: String(error) };
  }
}

function readJson(key) {
  try {
    return JSON.parse(localStorage.getItem(key) || 'null');
  } catch {
    return null;
  }
}

/* ------------------------------------------------------------------- wiring */

$('consent-accept').addEventListener('click', () => {
  localStorage.setItem(CONSENT_KEY, JSON.stringify({ accepted_at: Date.now() }));
  afterConsent();
});

$('setup-save').addEventListener('click', (event) => {
  event.preventDefault();
  const shopId = $('setup-shop').value.trim();
  const token = $('setup-token').value.trim();
  if (!shopId || !token) return;
  localStorage.setItem(CREDS_KEY, JSON.stringify({ shop_id: shopId, token }));
  state.shopId = shopId;
  state.token = token;
  showShopName();
  afterConsent();
});

$('start').addEventListener('click', () => {
  if (state.recorder?.state === 'paused') resumeRecording();
  else startRecording();
});

$('pause').addEventListener('click', () => pauseRecording());
$('stop').addEventListener('click', () => stopRecording());

window.addEventListener('beforeunload', () => {
  if (!state.sessionId) return;
  // Best effort: queued chunks stay in IndexedDB and upload next visit.
  navigator.sendBeacon?.(
    '/session/end',
    new Blob(
      [JSON.stringify({ shop_id: state.shopId, token: state.token, session_id: state.sessionId })],
      { type: 'application/json' },
    ),
  );
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/sw.js').catch(() => {});
}

boot().catch((error) => setStatus('idle', `startup error: ${error.message || error}`));
