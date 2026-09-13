'use strict';

const {t, setText, getLanguage} = window.FootboyI18n;
const $ = id => document.getElementById(id);
const player = $('player');
let currentStatus = null;
let refreshBusy = false;
let refreshTimer = null;
let statusRevision = 0;
let toastTimer = null;
let lastSession = null;
let hls = null;
let playerGeneration = null;
let playbackProbe = false;
let playerRetry = null;
let sourceLabel = 'video';
const editors = {};
const canvas = $('roi-canvas');
const context = canvas.getContext('2d');
let dragStart = null;
let submitting = false;
let defaultsApplied = false;
let lineSubmitting = false;
let lineOptionsKey = '';
let audioTimer = null;
let audioDirty = false;
let audioSending = false;
let audioVersion = 0;
let accessToken = '';
const accessStorageKey = 'footboy.controlToken';
const invalidAccessMessage = t("The control token is invalid or expired. Use the console link or token from the current startup.");

const phaseNames = {
  IDLE: t("Ready to connect"), CHECK: t("Checking environment"), INIT: t("Preparing connection"), RESOLVE: t("Resolving live room"),
  SNIFF: t("Choose a match stream"), MEASURE: t("Reading match clocks"), VERIFY: t("Verifying clocks"), RUN: t("Live stream running"),
  'P0-RUN': t("Direct stream test"), ADJUST: t("Applying offset"), RECOVER: t("Reconnecting"),
  STOPPING: t("Stopping"), STOPPED: t("Session stopped"), ERROR: t("Connection failed")
};

function newEditor() {
  return {image: null, roi: null, flip: 'none', inverted: false, dirty: false, version: null,
    ocrVersion: null, ocrImages: {}};
}
function resetEditors() {
  editors.video = newEditor(); editors.bili = newEditor();
  drawRoi();
}
resetEditors();

function toast(message, error = false) {
  clearTimeout(toastTimer);
  setText($('toast'), message);
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
}

function setAccessToken(value) {
  accessToken = value.trim();
  try {
    if (accessToken) sessionStorage.setItem(accessStorageKey, accessToken);
    else sessionStorage.removeItem(accessStorageKey);
  } catch (_error) { /* The control link also works with browser storage disabled. */ }
}

function readAccessLink() {
  const parameters = new URLSearchParams(location.hash.slice(1));
  if (!parameters.has('token')) return false;
  setAccessToken(parameters.get('token') || '');
  history.replaceState(null, '', location.pathname + location.search);
  return true;
}

function showAccessPanel(message) {
  $('access-panel').hidden = false;
  $('workspace').inert = true;
  if (message) setText($('access-message'), message);
  setText($('phase-text'), t("Waiting for authentication"));
  $('phase').classList.remove('running', 'busy');
  currentStatus = null;
  clearTimeout(audioTimer); audioDirty = false; audioVersion += 1;
  detachPlayer(); resetEditors();
}

$('access-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (refreshBusy) return;
  setAccessToken($('access-token').value);
  $('access-token').value = '';
  await refresh();
});
window.addEventListener('hashchange', () => {
  if (readAccessLink()) refresh();
});

async function timedFetch(path, options = {}, timeout = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const token = path.startsWith('/api/') ? accessToken : '';
  try {
    const headers = new Headers(options.headers);
    if (token) headers.set('Authorization', `Bearer ${token}`);
    if (path.startsWith('/api/')) headers.set('Accept-Language', getLanguage());
    const response = await fetch(path, {...options, headers, signal: controller.signal});
    if (response.status === 401 && token && token === accessToken) {
      setAccessToken('');
      showAccessPanel(invalidAccessMessage);
    }
    return response;
  }
  finally { clearTimeout(timer); }
}

async function post(path, body = {}) {
  const response = await timedFetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || t('Request failed ({0})', response.status));
  statusRevision += 1;
  refresh();
  return result;
}

function action(button, path, body, message) {
  button.addEventListener('click', async () => {
    try {
      await post(path, typeof body === 'function' ? body() : body);
      if (message) toast(message);
    } catch (error) { toast(error.message, true); }
  });
}

function parseHeaders(text) {
  const result = {};
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line) continue;
    const colon = line.indexOf(':');
    if (colon <= 0) throw new Error(t("Use Name: Value for each header, one per line"));
    result[line.slice(0, colon).trim()] = line.slice(colon + 1).trim();
  }
  return result;
}

$('source-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (submitting) return;
  submitting = true; $('start').disabled = true;
  try {
    const initial = $('initial-offset').value;
    const offset = initial === '' ? null : Number(initial);
    if (offset !== null && !Number.isFinite(offset)) throw new Error(t("The initial offset must be a finite number"));
    await post('/api/start', {
      video_url: $('video-url').value.trim(), bili_url: $('bili-url').value.trim(),
      video_direct: $('video-direct').checked, bili_direct: $('bili-direct').checked,
      video_no_proxy: $('video-no-proxy').checked,
      video_line_text: $('video-direct').checked ? null : $('video-line-text').value.trim() || null,
      auto_measure: $('auto-measure').checked, offset_seconds: offset,
      video_headers: parseHeaders($('video-headers').value),
      bili_headers: parseHeaders($('bili-headers').value)
    });
    toast(t("Connecting. Progress for both streams will appear in the console."));
  } catch (error) { toast(error.message, true); }
  finally { submitting = false; refresh(); }
});

document.querySelectorAll('[data-delta]').forEach(button => {
  action(button, '/api/offset', () => ({delta_ms: Number(button.dataset.delta)}));
});
action($('remeasure'), '/api/remeasure', {}, t("Reading both match clocks and synchronizing"));
action($('resniff'), '/api/resniff', {}, t("Choose a match stream in the browser on the host computer"));
action($('stop'), '/api/stop', {}, t("Stopping the session"));
action($('cancel-source'), '/api/source', {id: null}, t("Automatic selection paused for the current stream"));
$('line-form').addEventListener('submit', async event => {
  event.preventDefault();
  if (lineSubmitting || !$('line-choice').value) return;
  lineSubmitting = true; $('switch-line').disabled = true;
  try {
    await post('/api/line', {text: $('line-choice').value});
    toast(t("Fetching the selected stream; switching after validation"));
  } catch (error) { toast(error.message, true); }
  finally { lineSubmitting = false; refresh(); }
});

function activeSession(status) {
  return status.active ?? !['IDLE', 'STOPPED', 'ERROR', 'INIT'].includes(status.state);
}

function audioValues() {
  return {original_enabled: $('original-enabled').checked,
    original_volume: Number($('original-volume').value) / 100,
    commentary_volume: Number($('commentary-volume').value) / 100};
}
function updateAudioLabels() {
  for (const name of ['original', 'commentary']) {
    setText($(`${name}-volume-value`), `${$(`${name}-volume`).value}%`);
  }
}
async function sendAudio() {
  if (audioSending || !audioDirty) return;
  if (!currentStatus || !activeSession(currentStatus)) { audioDirty = false; return; }
  audioSending = true;
  const version = audioVersion;
  try { await post('/api/audio', audioValues()); }
  catch (error) { toast(error.message, true); }
  finally {
    audioSending = false;
    if (version === audioVersion) { audioDirty = false; refresh(); }
    else audioTimer = setTimeout(sendAudio, 300);
  }
}
for (const id of ['original-enabled', 'original-volume', 'commentary-volume']) {
  $(id).addEventListener('input', () => {
    audioDirty = true; audioVersion += 1;
    updateAudioLabels();
    $('original-volume').disabled = !$('original-enabled').checked;
    setText($('audio-state'), t("Audio levels pending"));
    clearTimeout(audioTimer); audioTimer = setTimeout(sendAudio, 300);
  });
}
function clockTime(value) {
  if (!value) return t("Not yet verified");
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleTimeString(getLanguage(), {hour12: false});
}
function elapsed(value) {
  if (!Number.isFinite(value)) return '—';
  const seconds = Math.floor(value);
  return seconds >= 3600 ? t('{0}h {1}m', Math.floor(seconds / 3600), Math.floor(seconds % 3600 / 60))
    : seconds >= 60 ? t('{0}m {1}s', Math.floor(seconds / 60), seconds % 60) : t('{0}s', seconds);
}

function updateStatus(status) {
  currentStatus = status;
  if (!defaultsApplied && status.defaults) {
    defaultsApplied = true;
    for (const [id, key] of [['auto-measure', 'auto_measure'], ['video-direct', 'video_direct'], ['bili-direct', 'bili_direct'], ['video-no-proxy', 'video_no_proxy']]) {
      $(id).checked = status.defaults[key];
    }
    if (status.defaults.offset_seconds != null) $('initial-offset').value = status.defaults.offset_seconds;
    if (status.defaults.video_line_text) $('video-line-text').value = status.defaults.video_line_text;
  }
  const active = activeSession(status);
  const controllable = active && status.state !== 'STOPPING';
  const session = status.session_id ?? 'p0';
  if (session !== lastSession) {
    clearTimeout(audioTimer); audioDirty = false; audioVersion += 1;
    lastSession = session; resetEditors(); detachPlayer();
  }
  setText($('phase-text'), phaseNames[status.state] || status.state);
  $('phase').classList.toggle('running', Boolean(status.ffmpeg?.running));
  $('phase').classList.toggle('busy', active && !['RUN', 'P0-RUN', 'ERROR'].includes(status.state));
  $('source-fields').disabled = active;
  $('video-line-text').disabled = $('video-direct').checked;
  $('start').disabled = active || submitting;
  const sessionsSupported = Boolean(status.capabilities?.session_controls);
  $('source-form').hidden = !sessionsSupported;
  $('session-actions').hidden = !active || !sessionsSupported;
  document.querySelector('.source-panel').classList.toggle('connected', active);
  $('stop').disabled = !controllable;
  $('resniff').disabled = !controllable || !status.video || status.video_direct
    || Boolean(status.sniffer?.running) || Boolean(status.source_switching);
  document.querySelectorAll('[data-delta]').forEach(button => { button.disabled = !controllable; });
  $('remeasure').disabled = !controllable || !status.video || !status.capabilities?.roi;
  setText($('remeasure'), status.measurement?.running ? t("↻ Syncing…") : t("↻ Sync now"));
  $('audio-controls').hidden = !status.capabilities?.audio_mix;
  const audioReady = controllable && Boolean(status.video);
  const originalAvailable = Boolean(status.video?.has_audio);
  $('original-enabled').disabled = !audioReady || !originalAvailable;
  $('commentary-volume').disabled = !audioReady;
  if (!audioDirty && !audioSending) {
    const audio = status.audio || {original_enabled: false, original_volume: 1, commentary_volume: 1};
    $('original-enabled').checked = audio.original_enabled;
    $('original-volume').value = Math.round(audio.original_volume * 100);
    $('commentary-volume').value = Math.round(audio.commentary_volume * 100);
    updateAudioLabels();
    setText($('audio-state'), !audioReady ? '' : !originalAvailable ? t("The original stream has no audio track")
      : JSON.stringify(status.audio) !== JSON.stringify(status.applied_audio) ? t("Audio levels pending") : '');
  }
  $('original-volume').disabled = !audioReady || !originalAvailable || !$('original-enabled').checked;
  $('flip').disabled = !controllable;
  $('inverted').disabled = !controllable;
  if (active && typeof status.auto_measure === 'boolean') $('auto-measure').checked = status.auto_measure;

  const offset = Number(status.offset_seconds || 0);
  setText($('offset-value'), `${offset >= 0 ? '+' : '−'}${Math.abs(offset).toFixed(3)}`);
  const confidence = status.confidence;
  const manual = confidence?.method?.startsWith('manual');
  const aligned = Boolean(status.aligned);
  setText($('alignment'), !active ? t("Not aligned") : confidence?.stopped ? t("Clock stopped")
    : aligned ? manual ? t("Manual offset") : t("Auto-aligned") : t("Not aligned"));
  $('alignment').classList.toggle('warning', !active || !aligned || Boolean(confidence?.stopped));
  setText($('offset-note'), !active ? t("Connect to adjust") : status.adjust_pending
    ? t('Pending · Playing at {0}s', Number(status.applied_offset_seconds ?? offset).toFixed(3))
    : t("Positive values delay Bilibili audio"));
  setText($('ffmpeg-status'), status.ffmpeg?.running ? t("Running") : active ? t("Waiting to start") : t("Not running"));
  $('health-light').classList.toggle('on', Boolean(status.ffmpeg?.running));
  setText($('confidence'), confidence?.video_samples != null
    ? t('Video {0} · Audio {1} frames', confidence.video_samples, confidence.bili_samples) : manual ? t("Manual offset") : '—');
  const residuals = [confidence?.video_residual, confidence?.bili_residual];
  setText($('residual'), residuals.every(Number.isFinite) ? t('{0} s', Math.max(...residuals).toFixed(3)) : '—');
  setText($('last-verified'), clockTime(status.last_verified_at));
  setText($('uptime'), status.ffmpeg?.running ? elapsed(status.ffmpeg.uptime_seconds) : '—');
  setText($('status-message'), status.message || t("Ready to connect"));
  $('status-message').classList.toggle('warning', active && !aligned || status.state === 'ERROR');
  setText($('logs'), [
    t('State: {0}', status.state),
    t('Processing speed: {0}', status.ffmpeg?.speed == null ? t("No data yet") : `${status.ffmpeg.speed.toFixed(2)}x`),
    t('DTS anomalies: {0}', status.ffmpeg?.non_monotonic_dts || 0),
    ...(status.ffmpeg?.stderr_tail || [])
  ].join('\n'));
  $('source-info').hidden = !active;
  setText($('source-info'), [
    status.video ? t('Video · {0} · {1}{2}', status.video.line_text || status.video.domain, status.video.video_codec || t("Probing"), status.video.height ? ` / ${status.video.height}p` : '') : '',
    status.bili ? t('Audio · {0} · {1}', status.bili.domain, status.bili.audio_codec || t("Probing")) : ''
  ].filter(Boolean).join('  ｜  ') || (active ? t("Fetching stream information…") : ''));
  const ocrStates = Object.values(status.measurement?.sources || {}).map(source => source.ocr?.state);
  setText($('measurement-state'), status.measurement?.running ? t("Capturing and reading clocks…")
    : ocrStates.includes('error') ? t("Capture or recognition failed") : ocrStates.includes('timeout') ? t("Recognition timed out")
    : ocrStates.includes('stopped') ? t("Match clock stopped")
    : status.measurement?.needs_roi?.length ? t("Select the undetected clock") : aligned && !manual ? t("Clock locked") : t("Waiting for frames"));
  $('measurement-state').classList.toggle('busy', Boolean(status.measurement?.running));
  updateCandidates(status.sniffer);
  updateLines(status, controllable);
  updatePreview(status);
  if (!active) {
    if (playerGeneration !== null) detachPlayer();
    setText($('placeholder-title'), status.state === 'ERROR' ? t("Connection incomplete") : t("Your home ground is ready"));
    setText($('placeholder-note'), status.state === 'ERROR' ? t("Check the connection status, adjust settings, and try again.") : t("Connect both streams to start watching here."));
    setText($('player-state'), t("Waiting for video"));
  } else if (playerGeneration === null) {
    setText($('placeholder-title'), status.ffmpeg?.running ? t("Waiting for live segments") : t("Connecting both streams"));
    setText($('placeholder-note'), t("Playback starts once the streams are ready and buffered."));
    setText($('player-state'), status.ffmpeg?.running ? t("Buffering") : t("Connecting"));
  }
  if (active && status.ffmpeg?.running) ensurePlayback(status);
}

$('video-direct').addEventListener('change', () => {
  $('video-line-text').disabled = $('video-direct').checked;
});

function updateLines(status, controllable) {
  const sniffer = status.sniffer || {};
  const lines = sniffer.lines || [];
  $('line-form').hidden = !controllable || !status.capabilities?.switch_line
    || status.video_direct || !lines.length;
  const select = $('line-choice');
  const key = JSON.stringify([lastSession, lines]);
  if (key !== lineOptionsKey) {
    const previous = select.value;
    select.replaceChildren(...lines.map(text => new Option(text, text)));
    const preferred = previous || sniffer.pending_line || sniffer.selected_line || status.video?.line_text;
    if (lines.includes(preferred)) select.value = preferred;
    lineOptionsKey = key;
  }
  $('switch-line').disabled = lineSubmitting || !controllable || !select.value
    || (status.source_switching && !sniffer.running);
  const current = status.video?.line_text;
  const pending = sniffer.pending_line || (sniffer.running ? sniffer.selected_line : null);
  setText($('current-line'), [current ? t('Current: {0}', current) : t("Waiting for stream validation"),
    pending ? t('Fetching: {0}', pending) : ''].filter(Boolean).join(' · '));
}

function updateCandidates(sniffer) {
  $('candidates-panel').hidden = !sniffer?.running;
  if (!sniffer?.running) return;
  setText($('sniff-message'), sniffer.message || t("Auto-selection starts after 5 seconds of stable playback. Select a stream to confirm now."));
  const keep = new Set();
  for (const item of [...(sniffer.candidates || [])].sort((a, b) => a.id - b.id)) {
    keep.add(String(item.id));
    let row = $('candidates').querySelector(`[data-id="${item.id}"]`);
    if (!row) {
      row = document.createElement('div'); row.className = 'candidate'; row.dataset.id = item.id;
      const head = document.createElement('div'); head.className = 'candidate-head';
      const label = document.createElement('span'); label.className = 'candidate-label';
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'button secondary small'; setText(button, t("Select"));
      action(button, '/api/source', {id: item.id}, t("Validating the selected stream"));
      head.append(label, button);
      const url = document.createElement('p'); url.className = 'candidate-url';
      row.append(head, url); $('candidates').append(row);
    }
    setText(row.querySelector('.candidate-label'), t('{0} · {1}', item.line_text || t('Stream {0}', item.id), item.cancelled ? t("Auto-selection paused") : item.browser_blocked ? t("Waiting for media validation") : item.active ? t('Playing{0}', item.countdown != null ? ` · ${item.countdown}s` : '') : t("Waiting for segments")));
    setText(row.querySelector('.candidate-url'), item.url);
  }
  $('candidates').querySelectorAll('.candidate').forEach(row => {
    if (!keep.has(row.dataset.id)) row.remove();
  });
}

function detachPlayer() {
  clearTimeout(playerRetry); playerRetry = null;
  if (hls) { hls.destroy(); hls = null; }
  player.pause(); player.removeAttribute('src'); player.load();
  playerGeneration = null;
  $('player-placeholder').hidden = false;
  $('play-button').hidden = true;
}

async function ensurePlayback(status, force = false) {
  const generation = status.ffmpeg?.generation || 'p0';
  if (!force && playerGeneration === generation || playbackProbe) return;
  playbackProbe = true;
  const session = lastSession;
  try {
    const response = await timedFetch('/live.m3u8', {cache: 'no-store'}, 4000);
    if (!response.ok) return;
    const manifest = await response.text();
    if (generation !== 'p0' && !manifest.includes(`seg_${generation}_`)) return;
    if (session !== lastSession || !currentStatus || !activeSession(currentStatus)) return;
    if (generation !== (currentStatus.ffmpeg?.generation || 'p0')) return;
    attachPlayer(generation);
  } catch (error) {
    setText($('player-state'), t("Waiting for the stream to resume"));
  } finally { playbackProbe = false; }
}

function retryPlayback(message) {
  setText($('player-message'), message);
  clearTimeout(playerRetry);
  playerRetry = setTimeout(() => {
    if (currentStatus && activeSession(currentStatus) && currentStatus.ffmpeg?.running) {
      ensurePlayback(currentStatus, true);
    }
  }, 4000);
}

function attachPlayer(generation) {
  detachPlayer(); playerGeneration = generation;
  const source = `/live.m3u8?g=${encodeURIComponent(generation)}`;
  setText($('player-state'), t("Buffering"));
  setText($('player-message'), t("Original video quality · Bilibili commentary"));
  // Chromium can advertise native HLS while failing to parse live TS streams.
  // Keep native playback on Apple browsers and use MSE elsewhere when available.
  const nativeHls = Boolean(player.canPlayType('application/vnd.apple.mpegurl'));
  const mseHls = Boolean(window.Hls && Hls.isSupported());
  const appleBrowser = navigator.vendor === 'Apple Computer, Inc.';
  if (nativeHls && (appleBrowser || !mseHls)) {
    player.src = source; tryPlay();
  } else if (mseHls) {
    hls = new Hls({liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8, backBufferLength: 30});
    hls.attachMedia(player); hls.loadSource(source);
    hls.on(Hls.Events.MANIFEST_PARSED, tryPlay);
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        retryPlayback(t("Cannot play this media; retrying. HEVC video requires browser support."));
      } else retryPlayback(t("Stream interrupted; waiting to resume…"));
    });
  } else {
    setText($('player-message'), t("This browser cannot play this HLS stream. Open the playback URL in Safari or an external player."));
  }
}

async function tryPlay() {
  try { await player.play(); $('play-button').hidden = true; }
  catch (_error) { $('play-button').hidden = false; }
}
$('play-button').addEventListener('click', () => { player.muted = false; tryPlay(); });
$('retry-play').addEventListener('click', () => {
  if (currentStatus && activeSession(currentStatus)) ensurePlayback(currentStatus, true);
  else toast(t("Connect the live sources first"));
});
player.addEventListener('loadeddata', () => { $('player-placeholder').hidden = true; });
player.addEventListener('playing', () => {
  setText($('player-state'), t("Playing")); $('play-button').hidden = true;
  $('player-placeholder').hidden = true;
});
player.addEventListener('waiting', () => { if (playerGeneration !== null) setText($('player-state'), t("Buffering")); });
player.addEventListener('pause', () => { if (playerGeneration !== null) setText($('player-state'), t("Paused")); });
player.addEventListener('error', () => {
  if (playerGeneration !== null) retryPlayback(t("Waiting for the stream to resume. Try reloading the player."));
});
player.addEventListener('ended', () => {
  if (currentStatus && activeSession(currentStatus)) retryPlayback(t("Waiting for new live segments…"));
});

function updatePreview(status) {
  for (const label of ['video', 'bili']) {
    const meta = status.measurement?.sources?.[label];
    const editor = editors[label];
    if (meta?.config?.roi && !editor.dirty && !dragStart) {
      editor.roi = meta.config.roi.slice(); editor.flip = meta.config.flip;
      editor.inverted = meta.config.inverted;
    }
    const reading = meta?.ocr?.reading;
    if (reading?.version && editor.ocrVersion !== reading.version) {
      editor.ocrVersion = reading.version;
      editor.ocrImages = {};
      loadOcrImages(label, editor, reading.version);
    } else if (!reading?.version) {
      editor.ocrVersion = null; editor.ocrImages = {};
    }
    if (!meta?.available || editor.version === meta.version) continue;
    editor.version = meta.version;
    loadPreview(label, editor, meta.version);
  }
  const meta = status.measurement?.sources?.[sourceLabel];
  setText($('snapshot-time'), meta?.captured_at ? t('Captured at {0}', clockTime(meta.captured_at)) : t("Based on the captured frame"));
  drawRoi();
}

async function loadOcrImages(label, editor, version) {
  await Promise.all(['crop', 'processed'].map(async kind => {
    try {
      const response = await timedFetch(`/api/ocr/${label}/${kind}.png?v=${encodeURIComponent(version)}`, {cache: 'no-store'});
      if (!response.ok) throw new Error(t("The OCR image is currently unavailable"));
      const blob = await response.blob();
      if (editors[label] !== editor || editor.ocrVersion !== version) return;
      const url = URL.createObjectURL(blob), image = new Image();
      image.onload = () => {
        URL.revokeObjectURL(url);
        if (editors[label] !== editor || editor.ocrVersion !== version) return;
        editor.ocrImages[kind] = image;
        if (label === sourceLabel) drawOcrFeedback();
      };
      image.onerror = () => {
        URL.revokeObjectURL(url);
        if (editors[label] === editor && editor.ocrVersion === version) editor.ocrVersion = null;
      };
      image.src = url;
    } catch (_error) {
      if (editors[label] === editor && editor.ocrVersion === version) editor.ocrVersion = null;
    }
  }));
}

function matchClock(seconds) {
  return `${String(Math.floor(seconds / 60)).padStart(2, '0')}:${String(seconds % 60).padStart(2, '0')}`;
}

function drawOcrFeedback() {
  const editor = editors[sourceLabel];
  const progress = currentStatus?.measurement?.sources?.[sourceLabel]?.ocr || {};
  const reading = progress.reading;
  const states = {
    queued: t("Region saved; waiting for new frames"), sampling: t("Capturing match frames…"),
    searching: progress.phase === 'nearby' ? t("Looking for the clock near your selection…") : t("Looking for the clock in the frame…"),
    stopped: t("Match clock stopped; keeping the current offset"), timeout: t("Recognition timed out; try a smaller region"),
    error: t("Capture or recognition failed; see details"), not_found: t("Clock not locked; select the full match clock"),
    cancelled: t("This round was cancelled"), locked: t('Running clock confirmed across {0} consecutive frames', progress.samples || 3)
  };
  setText($('ocr-reading-state'), editor.dirty ? t("Region changed; save to read the clock")
    : reading ? reading.clock == null ? t("Latest frame: no clock found") : t('Latest frame: {0}', matchClock(reading.clock))
    : states[progress.state] || t("Waiting for a clock reading"));
  setText($('ocr-validation-state'), editor.dirty ? t("This selection has not been validated")
    : states[progress.state] || t("Not locked; a running clock across at least 3 consecutive frames is required"));
  $('ocr-diagnostics').hidden = editor.dirty || !reading && !progress.reason;
  for (const [kind, id] of [['crop', 'ocr-actual-crop'], ['processed', 'ocr-processed']]) {
    const target = $(id), image = editor.ocrImages[kind];
    target.parentElement.hidden = !image || editor.dirty;
    if (image) {
      target.width = image.naturalWidth; target.height = image.naturalHeight;
      target.getContext('2d').drawImage(image, 0, 0);
    }
  }
  setText($('ocr-details'), [
    reading ? t('Raw OCR text: {0} · Frame {1}', reading.text?.trim() || t("(empty)"), reading.frame_index + 1) : '',
    reading?.seconds != null ? t('Reading time: {0} s', reading.seconds.toFixed(3)) : '',
    progress.attempts != null ? t('{0} attempts this round · {1} s', progress.attempts, (progress.elapsed || 0).toFixed(3)) : '',
    progress.reason || ''
  ].filter(Boolean).join('\n'));
}

async function loadPreview(label, editor, version) {
  try {
    const response = await timedFetch(`/api/snapshot/${label}.jpg?v=${encodeURIComponent(version)}`, {cache: 'no-store'});
    if (!response.ok) throw new Error(t("The frame snapshot is currently unavailable"));
    const blob = await response.blob();
    if (editors[label] !== editor || editor.version !== version) return;
    const url = URL.createObjectURL(blob);
    const image = new Image();
    image.onload = () => {
      URL.revokeObjectURL(url);
      if (editors[label] !== editor || editor.version !== version) return;
      editor.image = image;
      if (label === sourceLabel) drawRoi();
    };
    image.onerror = () => {
      URL.revokeObjectURL(url);
      if (editors[label] === editor && editor.version === version) editor.version = null;
    };
    image.src = url;
  } catch (_error) {
    if (editors[label] === editor && editor.version === version) editor.version = null;
  }
}

function validRoi(roi) {
  return roi && roi.every(Number.isFinite) && roi[0] >= 0 && roi[1] >= 0 && roi[2] > 0 && roi[3] > 0
    && roi[0] + roi[2] <= 1.000001 && roi[1] + roi[3] <= 1.000001;
}

function drawRoi() {
  const editor = editors[sourceLabel];
  if (!editor) return;
  $('flip').value = editor.flip; $('inverted').checked = editor.inverted;
  $('roi-empty').hidden = Boolean(editor.image);
  const canSave = editor.image && validRoi(editor.roi) && currentStatus && activeSession(currentStatus)
    && currentStatus.state !== 'STOPPING' && currentStatus.capabilities?.roi;
  $('save-roi').disabled = !canSave;
  $('roi-crop-preview').hidden = !editor.image || !validRoi(editor.roi);
  drawOcrFeedback();
  if (!editor.image) { context.clearRect(0, 0, canvas.width, canvas.height); return; }
  canvas.width = editor.image.naturalWidth; canvas.height = editor.image.naturalHeight;
  const width = canvas.width, height = canvas.height;
  const horizontal = ['h', 'hv'].includes(editor.flip), vertical = ['v', 'hv'].includes(editor.flip);
  context.save(); context.translate(horizontal ? width : 0, vertical ? height : 0);
  context.scale(horizontal ? -1 : 1, vertical ? -1 : 1);
  context.drawImage(editor.image, 0, 0, width, height); context.restore();
  if (validRoi(editor.roi)) {
    const [x, y, w, h] = editor.roi;
    const left = x * width, top = y * height, rw = w * width, rh = h * height;
    const crop = $('roi-crop'), scale = Math.min(4, 240 / rw, 100 / rh);
    crop.width = Math.max(1, Math.round(rw * scale)); crop.height = Math.max(1, Math.round(rh * scale));
    crop.getContext('2d').drawImage(canvas, left, top, rw, rh, 0, 0, crop.width, crop.height);
    context.fillStyle = 'rgba(8,16,9,.47)';
    context.fillRect(0, 0, width, top); context.fillRect(0, top + rh, width, height - top - rh);
    context.fillRect(0, top, left, rh); context.fillRect(left + rw, top, width - left - rw, rh);
    context.strokeStyle = '#c2f86e'; context.lineWidth = Math.max(2, width / 400);
    context.strokeRect(left, top, rw, rh);
    setText($('roi-summary'), t('Clock region: {0} × {1} px', Math.round(rw), Math.round(rh)));
  } else setText($('roi-summary'), t("Drag around the full match clock"));
  ['x', 'y', 'w', 'h'].forEach((axis, index) => {
    const input = $(`roi-${axis}`);
    if (document.activeElement !== input) input.value = editor.roi ? (editor.roi[index] * 100).toFixed(1) : '';
  });
}

document.querySelectorAll('[data-source]').forEach(button => {
  button.addEventListener('click', () => {
    sourceLabel = button.dataset.source; dragStart = null;
    document.querySelectorAll('[data-source]').forEach(tab => {
      tab.setAttribute('aria-selected', String(tab === button));
    });
    $('ocr-editor').setAttribute('aria-labelledby', button.id);
    drawRoi(); if (currentStatus) updatePreview(currentStatus);
  });
});
$('flip').addEventListener('change', () => {
  const editor = editors[sourceLabel]; editor.flip = $('flip').value;
  editor.roi = null; editor.dirty = true; drawRoi();
});
$('inverted').addEventListener('change', () => {
  editors[sourceLabel].inverted = $('inverted').checked; editors[sourceLabel].dirty = true;
  drawRoi();
});

function pointer(event) {
  const rect = canvas.getBoundingClientRect();
  return [Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)),
    Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height))];
}
canvas.addEventListener('pointerdown', event => {
  if (!editors[sourceLabel].image || !currentStatus || !activeSession(currentStatus)) return;
  dragStart = pointer(event); canvas.setPointerCapture(event.pointerId);
  editors[sourceLabel].dirty = true;
});
canvas.addEventListener('pointermove', event => {
  if (!dragStart) return;
  const [x, y] = pointer(event), [sx, sy] = dragStart;
  editors[sourceLabel].roi = [Math.min(x, sx), Math.min(y, sy), Math.abs(x - sx), Math.abs(y - sy)];
  drawRoi();
});
function endDrag(event) {
  if (!dragStart) return;
  const [x, y] = pointer(event), [sx, sy] = dragStart;
  editors[sourceLabel].roi = [Math.min(x, sx), Math.min(y, sy), Math.abs(x - sx), Math.abs(y - sy)];
  dragStart = null; drawRoi();
}
canvas.addEventListener('pointerup', endDrag);
canvas.addEventListener('pointercancel', () => { dragStart = null; });
['x', 'y', 'w', 'h'].forEach(axis => {
  $(`roi-${axis}`).addEventListener('input', () => {
    editors[sourceLabel].roi = ['x', 'y', 'w', 'h'].map(a => Number($(`roi-${a}`).value) / 100);
    editors[sourceLabel].dirty = true; drawRoi();
  });
});
$('save-roi').addEventListener('click', async () => {
  const editor = editors[sourceLabel], label = sourceLabel;
  if (!validRoi(editor.roi)) { toast(t("Select a valid clock region within the frame"), true); return; }
  $('save-roi').disabled = true;
  const selection = {source: label, roi: editor.roi.slice(), flip: editor.flip, inverted: editor.inverted};
  try {
    await post('/api/roi', selection);
    if (editors[label] === editor && JSON.stringify(selection) === JSON.stringify({source: label, roi: editor.roi, flip: editor.flip, inverted: editor.inverted})) {
      editor.dirty = false;
      editor.ocrVersion = null; editor.ocrImages = {};
      const meta = currentStatus?.measurement?.sources?.[label];
      if (meta) meta.ocr = {state: 'queued'};
    }
    toast(t("Clock region saved; remeasuring"));
  } catch (error) { toast(error.message, true); }
  finally { drawRoi(); }
});

$('hls-address').value = `${location.origin}/live.m3u8`;
$('copy-link').addEventListener('click', async () => {
  const input = $('hls-address');
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) await navigator.clipboard.writeText(input.value);
    else { input.focus(); input.select(); if (!document.execCommand('copy')) throw new Error(); }
    toast(t("Playback URL copied"));
  } catch (_error) { input.focus(); input.select(); toast(t("Copy the selected playback URL")); }
});

async function refresh() {
  if (refreshBusy) return;
  clearTimeout(refreshTimer);
  if (!accessToken) { showAccessPanel(); return; }
  if (!/^[A-Za-z0-9_-]{1,128}$/.test(accessToken)) {
    setAccessToken(''); showAccessPanel(invalidAccessMessage); return;
  }
  const token = accessToken;
  const revision = statusRevision;
  const language = getLanguage();
  $('access-submit').disabled = true;
  refreshBusy = true;
  try {
    const response = await timedFetch('/api/status', {cache: 'no-store'});
    if (response.status === 401) return;
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const status = await response.json();
    if (token !== accessToken || revision !== statusRevision || language !== getLanguage()) return;
    updateStatus(status);
    $('access-panel').hidden = true;
    $('workspace').inert = false;
  } catch (_error) {
    setText($('phase-text'), t("Console disconnected"));
    $('phase').classList.remove('running');
    setText($('status-message'), t("Reconnecting to the host. Check that Footboy is still running."));
    $('status-message').classList.add('warning');
  } finally {
    refreshBusy = false;
    $('access-submit').disabled = false;
    if (accessToken) refreshTimer = setTimeout(refresh, token === accessToken && language === getLanguage() && revision === statusRevision ? 2000 : 0);
  }
}
window.addEventListener('languagechange', () => {
  statusRevision += 1;
  $('toast').hidden = true;
  if (currentStatus) updateStatus(currentStatus);
  else drawRoi();
  refresh();
});
if (!readAccessLink()) {
  try { accessToken = sessionStorage.getItem(accessStorageKey) || ''; }
  catch (_error) { /* Ask for the control link when storage is unavailable. */ }
}
refresh();
