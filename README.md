# Footboy

Footboy 在个人局域网内把第三方足球直播的画面与 B 站直播间的音源按**源 PTS** 对齐，视频不转码，输出 HLS 和手机可用的控制页。请只处理你有权访问和播放的直播内容；本项目不绕过 DRM，也不负责对外分发。

偏移采用有符号定义：每路走表时 `比赛时钟 = 源 PTS + K`，最终 `D = K_B - K_V`。FFmpeg 使用 `-copyts`，并在 B 站输入前应用 `-itsoffset D`：`D > 0` 推后 B 站音频，`D < 0` 则让它在共同时间轴上提前。

## 环境

- Windows 10/11 或 macOS，Python 3.10+
- FFmpeg 6.0+（`ffmpeg`、`ffprobe` 均在 `PATH`）
- 本机 Chrome 或 Edge；也可安装 Playwright Chromium

安装：

```bash
python -m venv .venv
# Windows PowerShell: .venv\Scripts\Activate.ps1
# macOS / Linux: source .venv/bin/activate
python -m pip install -e ".[rapidocr]"
playwright install chromium
```

`rapidocr-onnxruntime` 的上游版本目前要求 Python `<3.13`，因此 Python 3.10–3.12 推荐上面的免系统安装方案。Python 3.13+ 请改装 `python -m pip install -e ".[tesseract]"` 并另外安装 Tesseract；Windows 若它不在 PATH，启动时传 `--tesseract-command "C:\\...\\tesseract.exe"`。

## 从 WebUI 开始

```bash
footboy --check
footboy
```

打开 `http://127.0.0.1:8080/`，输入比赛页面与 B 站直播间地址后连接。可在“指定线路”填写页面上的名称，例如“中文高清”或“高清直播5”；“5”“⑤”和全角数字可以互相匹配。留空时复用已保存的线路，或选择页面默认播放的流。WebUI 还支持启停、六档偏移微调、两路截图框选，以及为直链输入请求头。浏览器嗅探窗口出现在运行 Footboy 的电脑上。

连接后，“比赛线路”下拉框会列出页面及内嵌播放器中发现的线路名称。选好后点击“切换”，新线路通过媒体探测后替换画面，等待期间继续播放原线路。自动模式会重新测量源 PTS 偏移，切换期间较新的手动设置会保留。换线失败时保留原线路和当前偏移。网站布局无法自动识别时，可用“重新选线路”打开本机浏览器手选；该入口会等待你选择。媒体直链需要停止后更换地址。

手动调节会合并连续点击，在最后一次点击 1.5 秒后应用；较早启动的 OCR 不会覆盖新的手动设置。停止任务后控制页保持可用，可重新连接。Safari 使用原生 HLS，其他支持 MSE 的浏览器使用包内 hls.js，控制页不依赖外部 CDN。Chromium 即使声明支持原生 HLS，也优先使用 hls.js，避免部分真实直播分片在原生播放器中解析失败。

“音源音量”提供两个独立的 0–100% 滑块：原直播音量与 B 站解说音量。勾选“保留原直播声音”后可混入原音轨，调节任何一路不会改变另一路，0% 为该路静音。原音轨中的现场声与自带解说会一起保留，不能单独分离。设置作用于合成 HLS，保留到本次任务结束；拖动停止后合并应用并重新缓冲。混音或调整解说音量时仅音频转 AAC，视频仍复制。没有原音轨时禁用原声开关。

画面卡顿后可点击“立即同步”：重新 OCR 测量 `D = K_B - K_V`，成功后刷新混流与播放器，即使偏移没变也重新接续播放。没有可读的连续比赛计时则保留当前偏移。首次连接且没有保存或手动指定的偏移时，并行采样两路 PTS 估算初始时间轴以减少无声等待；该估算不代表比赛内容已对齐，也不保存为正式偏移。

命令行默认设置会同步到 WebUI，例如 `footboy --no-auto-measure --offset 0`。用 `--ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe` 可指定程序位置；Ctrl+C 会停止取流、关闭浏览器并让 FFmpeg 刷新播放列表。

比赛网站需要直连时，在“直链与初始偏移”中勾选“比赛源直连（不走代理）”，或启动时传 `footboy --video-no-proxy`。该设置覆盖比赛页面嗅探、媒体探测、OCR 采样、混流以及 HLS 分片和密钥请求；B 站继续使用原有代理设置。`footboy-p0` 也支持 `--video-no-proxy`。

如果控制台报告 FFmpeg 异常终止（例如 `SIGSEGV`），本次任务会停止自动重试，控制页仍可重新连接。请更换兼容的 FFmpeg/ffprobe 构建，并通过上述参数指定新程序。`--check` 检查可执行文件和最低版本，具体构建的 HLS 兼容性仍需媒体验证；新版 `N-…` Git 构建也能通过版本检查。

同一局域网的其他设备使用终端打印的内网地址，或打开对应的 `/live.m3u8`。`--host 127.0.0.1` 可仅监听本机。

## 先做 P0

先从浏览器开发者工具手抄第三方视频直链和 B 站直播直链，并人工估计 `D`：

```bash
footboy-p0 \
  --video-url 'https://video.example/live.m3u8' \
  --video-header 'Referer:https://video.example/' \
  --video-header 'User-Agent:Mozilla/5.0 ...' \
  --bili-url 'https://audio.example/live.flv' \
  --bili-header 'Referer:https://live.bilibili.com/' \
  --offset 12.5
```

命令会先让 ffprobe 验收两路，再打印控制页和 `/live.m3u8` 的内网地址。用手机 Safari 连续播放 30 分钟，并记录：

- 正负偏移方向是否正确；
- 大偏移时内存和 `speed` 是否稳定；
- 是否出现累计的 `Non-monotonic DTS`；
- 调偏移后是否在约 5 秒内越过 discontinuity 恢复播放。

## 完整模式

将比赛页面和 B 站房间地址分别放入本机环境变量 `FOOTBOY_VIDEO_PAGE_URL`、`FOOTBOY_BILI_ROOM_URL`。文档和验证记录只使用匿名描述及占位地址。

```bash
footboy \
  --video-page "$FOOTBOY_VIDEO_PAGE_URL" \
  --bili-room "$FOOTBOY_BILI_ROOM_URL" \
  --video-line "高清直播5"
```

`--video-line` 是可选参数，使用页面上实际存在的线路名称；未找到时显示提示并等待重新选择。浏览器打开后也可手动进入比赛并点击目标线路。终端按“最近 3 秒确实在拉分片”列普通 HLS 候选；稳定 5 秒后进入倒计时，回车可立即确认，输入 `c` 可取消当前首选。候选在接受前必须通过携带 UA、Referer、Origin、Cookie 的 ffprobe。

HTTPS 播放器请求 HTTP 媒体时，浏览器可能拦截播放。Footboy 会读取已观察到的这类请求，验证直播播放列表后交给 ffprobe 验收；结束和点播列表会被拒绝。控制页显示“等待媒体探测”，不把被拦截的请求标为浏览器正在播放。页面本身返回 HTTP 错误时会直接报告访问失败。

后台 OCR 会先尝试已保存 ROI，再定位画面顶部、底部和中央的小计时框。细长的亮色数字会尝试备用识别方式，锁定后保存识别样式；旧状态文件继续可用。无法锁定时，网页显示两路采样截图，先选择翻转方向，再拖动框选完整的比赛计时并保存。失败时明确显示“未对齐”，继续沿用上次 `D` 或 0，偏移始终可以手调。停表时放弃本轮，自动模式 60 秒后重试。若暂时只想手调：

```bash
footboy --video-page ... --bili-room ... --no-auto-measure --offset 0
```

控制页 API：

- `GET /api/status`
- `POST /api/start`，JSON 为 `{"video_url":"<比赛页面地址>", "bili_url":"<B站直播间地址>", "video_line_text":"高清直播5"}`
- `POST /api/stop`
- `POST /api/offset`，JSON 为 `{"delta_ms": 500}`（相对调整）
- `POST /api/remeasure`
- `POST /api/audio`，例如 `{"original_enabled":true,"original_volume":0.25,"commentary_volume":0.8}`；支持只提交一个字段，两路音量独立，范围 0–1
- `POST /api/resniff`
- `POST /api/source`，JSON 为 `{"id":1}`；`{"id":null}` 暂停当前候选自动选择
- `POST /api/line`，JSON 为 `{"text":"高清直播5"}`；嗅探中点击该线路，运行中重新获取并切换
- `POST /api/roi`，JSON 为 `{"source":"video", "roi":[0.1,0.1,0.2,0.1], "flip":"none", "inverted":false}`
- `GET /api/snapshot/video.jpg` 和 `GET /api/snapshot/bili.jpg`

`/api/start` 还接受 `video_direct`、`video_no_proxy`、`video_line_text`、`bili_direct`、`auto_measure`、`offset_seconds`、`video_headers`、`bili_headers`。`video_line_text` 最多 80 字符，省略时沿用命令行默认名称，空串或 null 清除指定名称；直链模式忽略该项。`video_no_proxy` 为布尔值，省略时沿用命令行默认值。状态中的 `sniffer.lines`、`selected_line`、`pending_line` 分别为发现的名称、浏览器已选择的名称和待查找的名称；`video.line_text` 表示已验收的当前视频线路。ROI 是翻转后画面上的归一化 `[x,y,width,height]`，范围为 0–1；`source` 为 `video/bili`，`flip` 为 `none/h/v/hv`。POST 成功返回 202，参数错误返回 400，任务冲突返回 409。

`state.json` 只保存房间/域名对应的识别区域、翻转、识别样式、线路文本和最后偏移。Cookie、请求头、带签名的直链不写入其中。保存过的偏移仅作为起点，不会静默标记为自动已对齐。可用 `--bili-cookies cookies.txt` 提供 Netscape 格式的 B 站 Cookie 文件。

## 当前实现边界

当前 `0.1.4` 增加按名称切换页面线路，处理浏览器混合内容拦截，并修复足球计时中冒号两侧间距较大时的自动定位。保留比赛源直连、FFmpeg 崩溃处理、Chromium HLS 播放、周期 OCR 复核、连续两次漂移确认、B 站重解析及第三方错误重嗅探。音频 GCC-PHAT 模块尚未接入控制流程，时钟跳变处逐帧精细化留待后续阶段。

H.264 输出 MPEG-TS；HEVC 输出 fMP4，使用每次启动独立的初始化文件，浏览器仍需具备 HEVC 解码能力。AAC 音频直接复制，其他音频转 AAC，视频始终不转码。启动后 30 秒无新分片触发恢复；首次启动留出 150 秒缓冲窗口。`D` 包含源 PTS 起点差异，不能直接当作实际缓冲时长。

足球页面“高清直播⑤”的真实 HLS 取流、1080p 画面及连续时钟 OCR 结果见 [0.1.4 验证记录](docs/validation-0.1.4.md)。两个 B 站房间的真实取流、视频与音频复制、手动偏移、恢复及停止重连结果见 [0.1.2 验证记录](docs/validation-0.1.2.md)；该轮约 3 分钟的功能检查关闭 OCR。视频样本 A 的非足球赛事赛事重播 OCR、同一路媒体独立双连接与手动优先结果见 [0.1.3 验证记录](docs/validation-0.1.3.md)。识别仍可能漏读或误读，必须通过至少 3 帧连续走表校验；赛间没有计时时显示“未对齐”。不同来源的同场足球同步精度、连续 2 小时运行、实际长延迟双路缓冲及手机 Safari 连播 30 分钟仍待验收。

不同线路的源 PTS 起点可能不同，更换编码或线路后须重新确认 `D`，不能直接沿用另一线路测得的数值。真实 B 站页面本轮遇到专题跳转和验证码，页面嗅探尚未完成实播验收；媒体直链与 B 站房间解析路径已验收。本地合成流、真实 Tesseract 和既有浏览器验证见 [0.1.1 验证记录](docs/validation-0.1.1.md) 和 [基线验证](docs/validation.md)。

## 运行测试

```bash
python -m pip install -e ".[dev,tesseract]"
python -m pytest
ruff check src tests
ruff format --check src tests
node --check src/footboy/serve/static/app.js
python -m build --no-isolation
```

媒体测试从 PATH 查找 FFmpeg/ffprobe，也可通过 `FOOTBOY_FFMPEG`、`FOOTBOY_FFPROBE` 指定。真实 OCR 测试使用系统 Tesseract，或 `FOOTBOY_TESSERACT` 指定的程序。缺少这些外部依赖时，对应测试会明确跳过。

安装 Playwright Chromium 后，设置 `FOOTBOY_BROWSER_TESTS=1` 启用桌面与手机尺寸的浏览器测试：

```bash
# macOS / Linux
FOOTBOY_BROWSER_TESTS=1 python -m pytest tests/test_media_integration.py -k webui -s
# Windows PowerShell
$env:FOOTBOY_BROWSER_TESTS = "1"
python -m pytest tests/test_media_integration.py -k webui -s
```

设置 `FOOTBOY_SCREENSHOT_DIR` 可保存运行截图。每次 Git 提交必须更新并暂存 [CHANGELOG.md](CHANGELOG.md)；本地启用检查钩子：`git config core.hooksPath .githooks`。
