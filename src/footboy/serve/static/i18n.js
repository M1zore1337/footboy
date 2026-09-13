'use strict';

(() => {
  const messages = {
    "Footboy home": "Footboy 足小子首页",
    "Interface language": "界面语言",
    "Paste the control token from your terminal": "粘贴启动终端中的控制密钥",
    "Live player": "直播播放器",
    "Combined live video": "合成直播画面",
    "Adjust audio timing": "相对调整声音时间",
    "Choose a source for clock recognition": "选择待识别画面",
    "Drag to select the match clock, or use the precise region fields below": "在采样画面上拖动框选比赛时钟；也可使用下方精确区域输入",
    "Selected region preview without the surrounding mask": "当前选区预览，不含框外遮罩",
    "Original crop from the latest reading": "最近试读的原始裁剪",
    "Preprocessed image sent to OCR": "送入 OCR 的预处理图像",
    "Paste the full match page or media URL": "粘贴比赛页面或媒体完整地址",
    "Exact label from the source page": "例如：高清直播5",
    "Paste the full Bilibili live room URL": "粘贴 B 站直播间完整地址",
    "Previous value or 0": "上次值或 0",
    "Choose a match stream": "选择比赛线路",
    "HLS playback URL": "HLS 播放地址",
    "Copy playback URL": "复制播放地址",
    "Footboy · Live Sync Console": "Footboy（足小子）· 直播同步台",
    "Live sync": "足小子",
    "LAN console": "局域网工作台",
    "Language": "语言",
    "Your match. In sync.": "画面与解说，同一时刻。",
    "Watch the match with your favorite Bilibili commentary, perfectly in time.": "连接比赛画面和 B 站音源，在熟悉的解说里看球。",
    "Ready to connect": "等待连接",
    "Connect to console": "连接控制台",
    "Open the console link from your terminal, or enter the current control token.": "打开启动终端中的控制页链接，或输入本次控制密钥。",
    "Control token": "控制密钥",
    "Combined stream": "合成直播",
    "Waiting for video": "等待视频源",
    "Your home ground is ready": "你的主场，准备就绪",
    "Connect both sources to start watching here.": "连接右侧两路直播，比赛将在这里开始。",
    "▶ Play with sound": "▶ 播放并开启声音",
    "Original video quality · Bilibili commentary": "视频保持原画质 · 使用 B 站直播间音源",
    "Reload": "重新载入",
    "Audio timing": "声音时间微调",
    "Not aligned": "未对齐",
    "s": "秒",
    "Connect to adjust": "连接后即可调节",
    "← Earlier audio": "← 声音提前",
    "Later audio →": "声音延后 →",
    "Repeated clicks apply together after a 1.5-second pause.": "连续点击会合并，停下 1.5 秒后应用。",
    "↻ Sync now": "↻ 立即同步",
    "Audio levels": "音源音量",
    "Include original audio": "保留原直播声音",
    "Original audio": "原直播音量",
    "Bilibili commentary": "B 站解说音量",
    "Match clock region": "时钟识别区域",
    "Waiting for frames": "等待采样",
    "If automatic detection fails, choose the orientation, then drag around the full match clock. Your selection is saved for next time.": "自动识别未锁定时，先选方向，再拖动框住完整的比赛计时。区域会在下次观看时复用。",
    "Match video": "比赛画面",
    "Bilibili room": "B 站直播间",
    "No frame captured yet": "还没有采样画面",
    "Connect the streams, then select Sync now to capture both sources.": "连接直播后，点击「重新测量」获取两路截图。",
    "Orientation": "画面方向",
    "Normal": "正常",
    "Flip horizontally": "水平翻转",
    "Flip vertically": "垂直翻转",
    "Rotate 180°": "旋转 180°",
    "Invert colors": "反色识别",
    "No region selected": "尚未选择区域",
    "Selected region": "当前选区",
    "Waiting for a clock reading": "等待试读比赛时钟",
    "A running clock across at least 3 consecutive frames is required": "至少需要连续 3 帧走表才能确认",
    "Recognition details": "本轮识别详情",
    "Latest frame crop": "最近试读的原帧裁剪",
    "Image used for OCR": "识别使用的图像",
    "Precise region settings": "精确设置区域",
    "Left %": "左侧 %",
    "Top %": "顶部 %",
    "Width %": "宽度 %",
    "Height %": "高度 %",
    "Based on the captured frame": "以采样截图为准",
    "Save and remeasure": "保存区域并重测",
    "Connect live sources": "连接直播源",
    "Video": "视频",
    "Stream label (optional)": "指定线路（可选）",
    "Enter the label as shown on the source page to select it automatically. You can switch streams after connecting.": "可填线路名称自动选择；连接后也可从下方线路列表切换。",
    "Audio": "音源",
    "Use the room's audio and read the match clock from its video.": "使用直播间声音，并识别画面里的比赛计时。",
    "Detect and align clocks automatically": "自动识别并对齐时钟",
    "Verify every 2 minutes while enabled": "开启后每 2 分钟复核一次",
    "Direct URLs and initial offset": "直链与初始偏移",
    "Bypass proxies for the match source": "比赛源直连（不走代理）",
    "Match URL is a direct media URL": "比赛地址是媒体直链",
    "Bilibili URL is a direct media URL": "B 站地址是媒体直链",
    "Initial audio offset (seconds)": "初始声音偏移（秒）",
    "Match request headers": "比赛源请求头",
    "Bilibili request headers": "B 站源请求头",
    "Use direct URLs for manual validation. Enter one header per line; headers are not saved.": "直链模式用于手动验收；请求头每行一条，不保存到状态文件。",
    "Connect and watch": "连接并开始观看",
    "Choose another stream": "重新选线路",
    "Stop session": "停止任务",
    "Match stream": "比赛线路",
    "Switch": "切换",
    "The new stream replaces the video after validation. Automatic mode recalibrates the clocks.": "新线路验收后替换画面；自动模式会重新校准时钟。",
    "Pause auto-selection": "暂停自动选择",
    "Choose a stream with active segments. Muxing starts after validation.": "优先选择正在拉取分片的线路，通过探测后开始混流。",
    "Connection status": "连接状态",
    "Muxer": "混流进程",
    "Not started": "未启动",
    "Clock samples": "识别样本",
    "Max. clock residual": "最大识别残差",
    "Last verified": "上次复核",
    "Not yet verified": "尚未复核",
    "Uptime": "连续运行",
    "Enter both source URLs to connect.": "输入两路地址，开始连接。",
    "View logs": "查看运行日志",
    "No logs yet": "暂无日志",
    "Watch on other devices": "在其他设备观看",
    "Open the playback URL in a player on the same LAN. To control the session, use the console link from your terminal.": "同一局域网的播放器可直接打开播放地址；需要控制直播时，使用终端中的控制页链接。",
    "Copy": "复制",
    "Total latency is the slower source's delay plus about": "实际延迟为较慢源自身延迟，再加约",
    "6–10 seconds": "6–10 秒",
    "of playback buffering.": "播放缓冲。",
    "Footboy": "Footboy 足小子",
    "Made for your match": "为你的比赛时刻",
    "On your LAN · One match, two streams": "局域网使用 · 一场比赛，两路直播",
    "Enable JavaScript to use the console; the HLS playback URL is /live.m3u8.": "请启用 JavaScript 以使用控制台；HLS 播放地址为 /live.m3u8。",
    "The control token is invalid or expired. Use the console link or token from the current startup.": "控制密钥无效或已过期，请使用本次启动终端中的控制页链接或密钥。",
    "Checking environment": "检查环境",
    "Preparing connection": "准备连接",
    "Resolving live room": "获取直播间",
    "Reading match clocks": "识别比赛时钟",
    "Verifying clocks": "复核时钟",
    "Live stream running": "直播运行中",
    "Direct stream test": "直链测试中",
    "Applying offset": "正在应用偏移",
    "Reconnecting": "正在恢复连接",
    "Stopping": "正在停止",
    "Session stopped": "任务已停止",
    "Connection failed": "连接失败",
    "Waiting for authentication": "等待验证",
    "Use Name: Value for each header, one per line": "请求头需使用「名称: 值」，每行一条",
    "The initial offset must be a finite number": "初始偏移必须是有限数值",
    "Connecting. Progress for both streams will appear in the console.": "正在连接，两路直播的进度会显示在控制台",
    "Reading both match clocks and synchronizing": "正在识别两路比赛时钟并重新同步",
    "Choose a match stream in the browser on the host computer": "请在本机浏览器重新选择比赛线路",
    "Stopping the session": "正在停止任务",
    "Automatic selection paused for the current stream": "已暂停当前线路的自动选择",
    "Fetching the selected stream; switching after validation": "正在获取所选线路，通过探测后切换",
    "Audio levels pending": "音量待应用",
    "↻ Syncing…": "↻ 正在同步…",
    "The original stream has no audio track": "原直播不含音轨",
    "Clock stopped": "检测到停表",
    "Manual offset": "手动设置",
    "Auto-aligned": "自动已对齐",
    "Positive values delay Bilibili audio": "正值推后 B 站音频",
    "Running": "运行中",
    "Waiting to start": "等待启动",
    "Not running": "未运行",
    "No data yet": "尚无数据",
    "Probing": "探测中",
    "Fetching stream information…": "正在获取直播信息…",
    "Capturing and reading clocks…": "正在采样与识别…",
    "Capture or recognition failed": "采样或识别失败",
    "Recognition timed out": "识别超时",
    "Match clock stopped": "比赛时钟停表",
    "Select the undetected clock": "请框选未识别的时钟",
    "Clock locked": "时钟已锁定",
    "Connection incomplete": "连接尚未完成",
    "Check the connection status, adjust settings, and try again.": "查看连接状态，调整设置后重新开始。",
    "Connect both streams to start watching here.": "连接两路直播，比赛将在这里开始。",
    "Waiting for live segments": "正在等待直播分片",
    "Connecting both streams": "正在连接两路直播",
    "Playback starts once the streams are ready and buffered.": "取流和缓冲完成后，即可开始观看。",
    "Buffering": "正在缓冲",
    "Connecting": "正在连接",
    "Waiting for stream validation": "等待线路验收",
    "Auto-selection starts after 5 seconds of stable playback. Select a stream to confirm now.": "稳定播放 5 秒后开始倒计时，可点选立即确认。",
    "Select": "选用",
    "Validating the selected stream": "正在验收所选线路",
    "Auto-selection paused": "已暂停自动选择",
    "Waiting for media validation": "等待媒体探测",
    "Waiting for segments": "等待分片",
    "Waiting for the stream to resume": "等待直播恢复",
    "Cannot play this media; retrying. HEVC video requires browser support.": "当前媒体无法播放，正在重试；HEVC 画面需要浏览器支持。",
    "Stream interrupted; waiting to resume…": "直播暂时中断，正在等待恢复…",
    "This browser cannot play this HLS stream. Open the playback URL in Safari or an external player.": "浏览器不支持此 HLS 播放方式，可用 Safari 或外部播放器打开播放地址。",
    "Connect the live sources first": "请先连接直播源",
    "Playing": "正在播放",
    "Paused": "已暂停",
    "Waiting for the stream to resume. Try reloading the player.": "播放器等待直播恢复，可尝试重新载入。",
    "Waiting for new live segments…": "等待新一段直播…",
    "The OCR image is currently unavailable": "本次识别图像暂不可用",
    "Region saved; waiting for new frames": "区域已保存，等待重新采样",
    "Capturing match frames…": "正在采样比赛画面…",
    "Looking for the clock near your selection…": "正在选区周边寻找时钟…",
    "Looking for the clock in the frame…": "正在画面中寻找时钟…",
    "Match clock stopped; keeping the current offset": "比赛时钟停表，保留当前偏移",
    "Recognition timed out; try a smaller region": "识别超时，可缩小选区后重试",
    "Capture or recognition failed; see details": "采样或识别失败，请查看详情",
    "Clock not locked; select the full match clock": "未锁定，请框选完整比赛计时",
    "This round was cancelled": "本轮已取消",
    "Region changed; save to read the clock": "选区已修改，保存后试读",
    "Latest frame: no clock found": "最近单帧试读：未读到时间",
    "This selection has not been validated": "当前选区尚未验证",
    "Not locked; a running clock across at least 3 consecutive frames is required": "尚未锁定，至少需要连续 3 帧走表",
    "(empty)": "（空）",
    "The frame snapshot is currently unavailable": "采样画面暂不可用",
    "Drag around the full match clock": "拖动框选完整的比赛计时",
    "Select a valid clock region within the frame": "请在画面内选择有效的时钟区域",
    "Clock region saved; remeasuring": "识别区域已保存，正在重新测量",
    "Playback URL copied": "播放地址已复制",
    "Copy the selected playback URL": "请复制已选中的播放地址",
    "Console disconnected": "控制台连接中断",
    "Reconnecting to the host. Check that Footboy is still running.": "正在尝试重新连接本机服务，请确认 Footboy 仍在运行。",
    "Request failed ({0})": "请求失败 ({0})",
    "{0}h {1}m": "{0}时 {1}分",
    "{0}m {1}s": "{0}分 {1}秒",
    "{0}s": "{0}秒",
    "Pending · Playing at {0}s": "待应用 · 当前播放 {0}s",
    "Video {0} · Audio {1} frames": "画面 {0} · 音源 {1} 帧",
    "{0} s": "{0} 秒",
    "State: {0}": "状态：{0}",
    "Processing speed: {0}": "处理速度：{0}",
    "DTS anomalies: {0}": "DTS 异常：{0}",
    "Video · {0} · {1}{2}": "画面 · {0} · {1}{2}",
    "Audio · {0} · {1}": "音源 · {0} · {1}",
    "Current: {0}": "当前：{0}",
    "Fetching: {0}": "正在获取：{0}",
    "{0} · {1}": "{0} · {1}",
    "Stream {0}": "线路 {0}",
    "Playing{0}": "正在播放{0}",
    "Captured at {0}": "采样于 {0}",
    "Running clock confirmed across {0} consecutive frames": "连续 {0} 帧走表通过",
    "Latest frame: {0}": "最近单帧试读：{0}",
    "Raw OCR text: {0} · Frame {1}": "原始识别文字：{0} · 第 {1} 帧",
    "Reading time: {0} s": "单次试读 {0} 秒",
    "{0} attempts this round · {1} s": "本轮 {0} 次试读 · {1} 秒",
    "Clock region: {0} × {1} px": "{0} × {1} 像素的识别区域"
  };
  const storageKey = 'footboy.language';
  const supported = ['zh-CN', 'en'];
  const bindings = new WeakMap();
  let language = 'zh-CN';
  try {
    const saved = localStorage.getItem(storageKey);
    if (supported.includes(saved)) language = saved;
  } catch (_error) { /* Language selection also works without browser storage. */ }

  class Text {
    constructor(key, args) { this.key = key; this.args = args; }
    toString() {
      const template = language === 'zh-CN' && Object.hasOwn(messages, this.key)
        ? messages[this.key] : this.key;
      return template.replace(/\{(\d+)\}/g, (_match, index) => String(this.args[Number(index)] ?? ''));
    }
  }
  const t = (key, ...args) => new Text(key, args);
  function setText(element, value) {
    if (typeof element === 'string') element = document.getElementById(element);
    bindings.set(element, value);
    element.setAttribute('data-i18n-bound', '');
    element.textContent = String(value);
  }
  function apply() {
    document.documentElement.lang = language;
    document.querySelectorAll('[data-i18n]').forEach(element => {
      element.textContent = String(t(element.dataset.i18n));
    });
    for (const attribute of ['placeholder', 'aria-label', 'title']) {
      document.querySelectorAll(`[data-i18n-${attribute}]`).forEach(element => {
        element.setAttribute(attribute, String(t(element.getAttribute(`data-i18n-${attribute}`))));
      });
    }
    document.querySelectorAll('[data-i18n-bound]').forEach(element => {
      element.textContent = String(bindings.get(element));
    });
    document.getElementById('language').value = language;
  }
  function setLanguage(value, persist = true) {
    if (!supported.includes(value) || value === language) return;
    language = value;
    if (persist) {
      try { localStorage.setItem(storageKey, language); }
      catch (_error) { /* Keep the selection for this page even if storage is disabled. */ }
    }
    apply();
    window.dispatchEvent(new Event('languagechange'));
  }
  window.FootboyI18n = {t, setText, getLanguage: () => language};
  document.getElementById('language').addEventListener('change', event => setLanguage(event.target.value));
  window.addEventListener('storage', event => {
    if (event.key === storageKey) setLanguage(event.newValue || 'zh-CN', false);
  });
  apply();
})();
