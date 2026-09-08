'use strict';

const $ = id => document.getElementById(id);
const player = $('player');
let currentStatus = null;
let refreshBusy = false;
let refreshTimer = null;
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

const phaseNames = {
  IDLE: '等待连接', CHECK: '检查环境', INIT: '准备连接', RESOLVE: '获取直播间',
  SNIFF: '选择比赛线路', MEASURE: '识别比赛时钟', VERIFY: '复核时钟', RUN: '直播运行中',
  'P0-RUN': '直链测试中', ADJUST: '正在应用偏移', RECOVER: '正在恢复连接',
  STOPPING: '正在停止', STOPPED: '任务已停止', ERROR: '连接失败'
};

function newEditor() {
  return {image: null, roi: null, flip: 'none', inverted: false, dirty: false, version: null};
}
function resetEditors() {
  editors.video = newEditor(); editors.bili = newEditor();
  drawRoi();
}
resetEditors();

function toast(message, error = false) {
  clearTimeout(toastTimer);
  $('toast').textContent = message;
  $('toast').classList.toggle('error', error);
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, error ? 6500 : 3500);
}

async function timedFetch(path, options = {}, timeout = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  try { return await fetch(path, {...options, signal: controller.signal}); }
  finally { clearTimeout(timer); }
}

async function post(path, body = {}) {
  const response = await timedFetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || `请求失败 (${response.status})`);
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
    if (colon <= 0) throw new Error('请求头需使用「名称: 值」，每行一条');
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
    if (offset !== null && !Number.isFinite(offset)) throw new Error('初始偏移必须是有限数值');
    await post('/api/start', {
      video_url: $('video-url').value.trim(), bili_url: $('bili-url').value.trim(),
      video_direct: $('video-direct').checked, bili_direct: $('bili-direct').checked,
      auto_measure: $('auto-measure').checked, offset_seconds: offset,
      video_headers: parseHeaders($('video-headers').value),
      bili_headers: parseHeaders($('bili-headers').value)
    });
    toast('正在连接，两路直播的进度会显示在控制台');
  } catch (error) { toast(error.message, true); }
  finally { submitting = false; refresh(); }
});

document.querySelectorAll('[data-delta]').forEach(button => {
  action(button, '/api/offset', () => ({delta_ms: Number(button.dataset.delta)}));
});
action($('remeasure'), '/api/remeasure', {}, '正在重新采样比赛时钟');
action($('resniff'), '/api/resniff', {}, '请在本机浏览器重新选择比赛线路');
action($('stop'), '/api/stop', {}, '正在停止任务');
action($('cancel-source'), '/api/source', {id: null}, '已暂停当前线路的自动选择');

function activeSession(status) {
  return status.active ?? !['IDLE', 'STOPPED', 'ERROR', 'INIT'].includes(status.state);
}
function clockTime(value) {
  if (!value) return '尚未复核';
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '—' : date.toLocaleTimeString('zh-CN', {hour12: false});
}
function elapsed(value) {
  if (!Number.isFinite(value)) return '—';
  const seconds = Math.floor(value);
  return seconds >= 3600 ? `${Math.floor(seconds / 3600)}时 ${Math.floor(seconds % 3600 / 60)}分`
    : seconds >= 60 ? `${Math.floor(seconds / 60)}分 ${seconds % 60}秒` : `${seconds}秒`;
}

function updateStatus(status) {
  currentStatus = status;
  if (!defaultsApplied && status.defaults) {
    defaultsApplied = true;
    for (const [id, key] of [['auto-measure', 'auto_measure'], ['video-direct', 'video_direct'], ['bili-direct', 'bili_direct']]) {
      $(id).checked = status.defaults[key];
    }
    if (status.defaults.offset_seconds != null) $('initial-offset').value = status.defaults.offset_seconds;
  }
  const active = activeSession(status);
  const controllable = active && status.state !== 'STOPPING';
  const session = status.session_id ?? 'p0';
  if (session !== lastSession) {
    lastSession = session; resetEditors(); detachPlayer();
  }
  $('phase-text').textContent = phaseNames[status.state] || status.state;
  $('phase').classList.toggle('running', Boolean(status.ffmpeg?.running));
  $('phase').classList.toggle('busy', active && !['RUN', 'P0-RUN', 'ERROR'].includes(status.state));
  $('source-fields').disabled = active;
  $('start').disabled = active || submitting;
  const sessionsSupported = Boolean(status.capabilities?.session_controls);
  $('source-form').hidden = !sessionsSupported;
  $('session-actions').hidden = !active || !sessionsSupported;
  document.querySelector('.source-panel').classList.toggle('connected', active);
  $('stop').disabled = !controllable;
  $('resniff').disabled = !controllable || !status.video || Boolean(status.sniffer?.running);
  document.querySelectorAll('[data-delta]').forEach(button => { button.disabled = !controllable; });
  $('remeasure').disabled = !controllable || !status.video || !status.capabilities?.roi;
  $('flip').disabled = !controllable;
  $('inverted').disabled = !controllable;
  if (active && typeof status.auto_measure === 'boolean') $('auto-measure').checked = status.auto_measure;

  const offset = Number(status.offset_seconds || 0);
  $('offset-value').textContent = `${offset >= 0 ? '+' : '−'}${Math.abs(offset).toFixed(3)}`;
  const confidence = status.confidence;
  const manual = confidence?.method?.startsWith('manual');
  const aligned = Boolean(status.aligned);
  $('alignment').textContent = !active ? '未对齐' : confidence?.stopped ? '检测到停表'
    : aligned ? manual ? '手动设置' : '自动已对齐' : '未对齐';
  $('alignment').classList.toggle('warning', !active || !aligned || Boolean(confidence?.stopped));
  $('offset-note').textContent = !active ? '连接后即可调节' : status.adjust_pending
    ? `待应用 · 当前播放 ${Number(status.applied_offset_seconds ?? offset).toFixed(3)}s`
    : '正值推后 B 站音频';
  $('ffmpeg-status').textContent = status.ffmpeg?.running ? '运行中' : active ? '等待启动' : '未运行';
  $('health-light').classList.toggle('on', Boolean(status.ffmpeg?.running));
  $('confidence').textContent = confidence?.video_samples != null
    ? `画面 ${confidence.video_samples} · 音源 ${confidence.bili_samples} 帧` : manual ? '手动设置' : '—';
  const residuals = [confidence?.video_residual, confidence?.bili_residual];
  $('residual').textContent = residuals.every(Number.isFinite) ? `${Math.max(...residuals).toFixed(3)} 秒` : '—';
  $('last-verified').textContent = clockTime(status.last_verified_at);
  $('uptime').textContent = status.ffmpeg?.running ? elapsed(status.ffmpeg.uptime_seconds) : '—';
  $('status-message').textContent = status.message || '等待连接';
  $('status-message').classList.toggle('warning', active && !aligned || status.state === 'ERROR');
  $('logs').textContent = [
    `状态：${status.state}`,
    `处理速度：${status.ffmpeg?.speed == null ? '尚无数据' : `${status.ffmpeg.speed.toFixed(2)}x`}`,
    `DTS 异常：${status.ffmpeg?.non_monotonic_dts || 0}`,
    ...(status.ffmpeg?.stderr_tail || [])
  ].join('\n');
  $('source-info').hidden = !active;
  $('source-info').textContent = [
    status.video ? `画面 · ${status.video.domain} · ${status.video.video_codec || '探测中'}${status.video.height ? ` / ${status.video.height}p` : ''}` : '',
    status.bili ? `音源 · ${status.bili.domain} · ${status.bili.audio_codec || '探测中'}` : ''
  ].filter(Boolean).join('  ｜  ') || (active ? '正在获取直播信息…' : '');
  $('measurement-state').textContent = status.measurement?.running ? '正在采样与识别…'
    : status.measurement?.needs_roi?.length ? '请框选未识别的时钟' : aligned && !manual ? '时钟已锁定' : '等待采样';
  $('measurement-state').classList.toggle('busy', Boolean(status.measurement?.running));
  updateCandidates(status.sniffer);
  updatePreview(status);
  if (!active) {
    if (playerGeneration !== null) detachPlayer();
    $('placeholder-title').textContent = status.state === 'ERROR' ? '连接尚未完成' : '你的主场，准备就绪';
    $('placeholder-note').textContent = status.state === 'ERROR' ? '查看连接状态，调整设置后重新开始。' : '连接两路直播，比赛将在这里开始。';
    $('player-state').textContent = '等待视频源';
  } else if (playerGeneration === null) {
    $('placeholder-title').textContent = status.ffmpeg?.running ? '正在等待直播分片' : '正在连接两路直播';
    $('placeholder-note').textContent = '取流和缓冲完成后，即可开始观看。';
    $('player-state').textContent = status.ffmpeg?.running ? '正在缓冲' : '正在连接';
  }
  if (active && status.ffmpeg?.running) ensurePlayback(status);
}

function updateCandidates(sniffer) {
  $('candidates-panel').hidden = !sniffer?.running;
  if (!sniffer?.running) return;
  $('sniff-message').textContent = sniffer.message || '稳定播放 5 秒后开始倒计时，可点选立即确认。';
  const keep = new Set();
  for (const item of [...(sniffer.candidates || [])].sort((a, b) => a.id - b.id)) {
    keep.add(String(item.id));
    let row = $('candidates').querySelector(`[data-id="${item.id}"]`);
    if (!row) {
      row = document.createElement('div'); row.className = 'candidate'; row.dataset.id = item.id;
      const head = document.createElement('div'); head.className = 'candidate-head';
      const label = document.createElement('span'); label.className = 'candidate-label';
      const button = document.createElement('button'); button.type = 'button';
      button.className = 'button secondary small'; button.textContent = '选用';
      action(button, '/api/source', {id: item.id}, '正在验收所选线路');
      head.append(label, button);
      const url = document.createElement('p'); url.className = 'candidate-url';
      row.append(head, url); $('candidates').append(row);
    }
    row.querySelector('.candidate-label').textContent = `线路 ${item.id} · ${item.cancelled ? '已暂停自动选择' : item.active ? `正在播放${item.countdown != null ? ` · ${item.countdown}s` : ''}` : '等待分片'}`;
    row.querySelector('.candidate-url').textContent = item.url;
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
    $('player-state').textContent = '等待直播恢复';
  } finally { playbackProbe = false; }
}

function retryPlayback(message) {
  $('player-message').textContent = message;
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
  $('player-state').textContent = '正在缓冲';
  $('player-message').textContent = '视频保持原画质 · 使用 B 站直播间音源';
  if (player.canPlayType('application/vnd.apple.mpegurl')) {
    player.src = source; tryPlay();
  } else if (window.Hls && Hls.isSupported()) {
    hls = new Hls({liveSyncDurationCount: 3, liveMaxLatencyDurationCount: 8, backBufferLength: 30});
    hls.attachMedia(player); hls.loadSource(source);
    hls.on(Hls.Events.MANIFEST_PARSED, tryPlay);
    hls.on(Hls.Events.ERROR, (_event, data) => {
      if (!data.fatal) return;
      if (data.type === Hls.ErrorTypes.MEDIA_ERROR) {
        retryPlayback('当前媒体无法播放，正在重试；HEVC 画面需要浏览器支持。');
      } else retryPlayback('直播暂时中断，正在等待恢复…');
    });
  } else {
    $('player-message').textContent = '浏览器不支持此 HLS 播放方式，可用 Safari 或外部播放器打开播放地址。';
  }
}

async function tryPlay() {
  try { await player.play(); $('play-button').hidden = true; }
  catch (_error) { $('play-button').hidden = false; }
}
$('play-button').addEventListener('click', () => { player.muted = false; tryPlay(); });
$('retry-play').addEventListener('click', () => {
  if (currentStatus && activeSession(currentStatus)) ensurePlayback(currentStatus, true);
  else toast('请先连接直播源');
});
player.addEventListener('loadeddata', () => { $('player-placeholder').hidden = true; });
player.addEventListener('playing', () => {
  $('player-state').textContent = '正在播放'; $('play-button').hidden = true;
  $('player-placeholder').hidden = true;
});
player.addEventListener('waiting', () => { if (playerGeneration !== null) $('player-state').textContent = '正在缓冲'; });
player.addEventListener('pause', () => { if (playerGeneration !== null) $('player-state').textContent = '已暂停'; });
player.addEventListener('error', () => {
  if (playerGeneration !== null) retryPlayback('播放器等待直播恢复，可尝试重新载入。');
});
player.addEventListener('ended', () => {
  if (currentStatus && activeSession(currentStatus)) retryPlayback('等待新一段直播…');
});

function updatePreview(status) {
  for (const label of ['video', 'bili']) {
    const meta = status.measurement?.sources?.[label];
    const editor = editors[label];
    if (meta?.config?.roi && !editor.dirty && !dragStart) {
      editor.roi = meta.config.roi.slice(); editor.flip = meta.config.flip;
      editor.inverted = meta.config.inverted;
    }
    if (!meta?.available || editor.version === meta.version) continue;
    editor.version = meta.version;
    const image = new Image();
    const version = meta.version;
    image.onload = () => {
      if (editors[label] !== editor || editor.version !== version) return;
      editor.image = image;
      if (label === sourceLabel) drawRoi();
    };
    image.onerror = () => { if (editor.version === version) editor.version = null; };
    image.src = `/api/snapshot/${label}.jpg?v=${encodeURIComponent(version)}`;
  }
  const meta = status.measurement?.sources?.[sourceLabel];
  $('snapshot-time').textContent = meta?.captured_at ? `采样于 ${clockTime(meta.captured_at)}` : '以采样截图为准';
  drawRoi();
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
    context.fillStyle = 'rgba(8,16,9,.47)';
    context.fillRect(0, 0, width, top); context.fillRect(0, top + rh, width, height - top - rh);
    context.fillRect(0, top, left, rh); context.fillRect(left + rw, top, width - left - rw, rh);
    context.strokeStyle = '#c2f86e'; context.lineWidth = Math.max(2, width / 400);
    context.strokeRect(left, top, rw, rh);
    $('roi-summary').textContent = `${Math.round(rw)} × ${Math.round(rh)} 像素的识别区域`;
  } else $('roi-summary').textContent = '拖动框选完整的比赛计时';
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
  if (!validRoi(editor.roi)) { toast('请在画面内选择有效的时钟区域', true); return; }
  $('save-roi').disabled = true;
  try {
    await post('/api/roi', {source: label, roi: editor.roi, flip: editor.flip, inverted: editor.inverted});
    editor.dirty = false; toast('识别区域已保存，正在重新测量');
  } catch (error) { toast(error.message, true); }
  finally { drawRoi(); }
});

$('hls-address').value = `${location.origin}/live.m3u8`;
$('copy-link').addEventListener('click', async () => {
  const input = $('hls-address');
  try {
    if (navigator.clipboard?.writeText && window.isSecureContext) await navigator.clipboard.writeText(input.value);
    else { input.focus(); input.select(); if (!document.execCommand('copy')) throw new Error(); }
    toast('播放地址已复制');
  } catch (_error) { input.focus(); input.select(); toast('请复制已选中的播放地址'); }
});

async function refresh() {
  if (refreshBusy) return;
  refreshBusy = true; clearTimeout(refreshTimer);
  try {
    const response = await timedFetch('/api/status', {cache: 'no-store'});
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    updateStatus(await response.json());
  } catch (_error) {
    $('phase-text').textContent = '控制台连接中断';
    $('phase').classList.remove('running');
    $('status-message').textContent = '正在尝试重新连接本机服务，请确认 Footboy 仍在运行。';
    $('status-message').classList.add('warning');
  } finally {
    refreshBusy = false;
    refreshTimer = setTimeout(refresh, 2000);
  }
}
refresh();
