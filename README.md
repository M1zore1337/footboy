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

打开 `http://127.0.0.1:8080/`，输入比赛页面与 B 站直播间地址后连接。WebUI 在取流前即可使用，支持启停、候选线路选择、六档偏移微调、两路截图框选，以及为直链输入请求头。浏览器嗅探窗口出现在运行 Footboy 的电脑上。

手动调节会合并连续点击，在最后一次点击 1.5 秒后应用；较早启动的 OCR 不会覆盖新的手动设置。停止任务后控制页保持可用，可重新连接。Safari 使用原生 HLS，其他支持 MSE 的浏览器使用包内 hls.js，控制页不依赖外部 CDN。Chromium 即使声明支持原生 HLS，也优先使用 hls.js，避免部分真实直播分片在原生播放器中解析失败。

命令行默认设置会同步到 WebUI，例如 `footboy --no-auto-measure --offset 0`。用 `--ffmpeg /path/to/ffmpeg --ffprobe /path/to/ffprobe` 可指定程序位置；Ctrl+C 会停止取流、关闭浏览器并让 FFmpeg 刷新播放列表。

比赛网站需要直连时，在“直链与初始偏移”中勾选“比赛源直连（不走代理）”，或启动时传 `footboy --video-no-proxy`。该设置覆盖比赛页面嗅探、媒体探测、OCR 采样、混流以及 HLS 分片和密钥请求；B 站继续使用原有代理设置。`footboy-p0` 也支持 `--video-no-proxy`。

如果控制台报告 FFmpeg 异常终止（例如 `SIGSEGV`），本次任务会停止自动重试，控制页仍可重新连接。请更换兼容的 FFmpeg/ffprobe 构建，并通过上述参数指定新程序。`--check` 检查可执行文件和最低版本，具体构建的 HLS 兼容性仍需媒体验证；新版 `N-…` Git 构建也能通过版本检查。

同一局域网的其他设备使用终端打印的内网地址，或打开对应的 `/live.m3u8`。`--host 127.0.0.1` 可仅监听本机。

## 先做 P0

先从浏览器开发者工具手抄第三方视频直链和 B 站直播直链，并人工估计 `D`：

```bash
footboy-p0 \
  --video-url 'https://example/video.m3u8' \
  --video-header 'Referer:https://example/' \
  --video-header 'User-Agent:Mozilla/5.0 ...' \
  --bili-url 'https://example/live.flv' \
  --bili-header 'Referer:https://live.bilibili.com/' \
  --offset 12.5
```

命令会先让 ffprobe 验收两路，再打印控制页和 `/live.m3u8` 的内网地址。用手机 Safari 连续播放 30 分钟，并记录：

- 正负偏移方向是否正确；
- 大偏移时内存和 `speed` 是否稳定；
- 是否出现累计的 `Non-monotonic DTS`；
- 调偏移后是否在约 5 秒内越过 discontinuity 恢复播放。

## 完整模式

```bash
footboy \
  --video-page 'https://third-party.example/match' \
  --bili-room 'https://live.example.invalid/ROOM_ID'
```

浏览器打开后，手动进入比赛并点击目标线路。终端按“最近 3 秒确实在拉分片”列候选；稳定 5 秒后进入倒计时，回车可立即确认，输入 `c` 可取消当前首选。候选在接受前必须通过携带 UA、Referer、Origin、Cookie 的 ffprobe。

后台 OCR 会先尝试已保存 ROI，再自动发现角落/中央的时钟。无法锁定时，网页显示两路采样截图，先选择翻转方向，再拖动框选完整的比赛计时并保存。失败时明确显示“未对齐”，继续沿用上次 `D` 或 0，偏移始终可以手调。停表时放弃本轮，自动模式 60 秒后重试。若暂时只想手调：

```bash
footboy --video-page ... --bili-room ... --no-auto-measure --offset 0
```

控制页 API：

- `GET /api/status`
- `POST /api/start`，JSON 为 `{"video_url":"https://video.example.invalid/match", "bili_url":"https://live.example.invalid/ROOM_ID"}`
- `POST /api/stop`
- `POST /api/offset`，JSON 为 `{"delta_ms": 500}`（相对调整）
- `POST /api/remeasure`
- `POST /api/resniff`
- `POST /api/source`，JSON 为 `{"id":1}`；`{"id":null}` 暂停当前候选自动选择
- `POST /api/roi`，JSON 为 `{"source":"video", "roi":[0.1,0.1,0.2,0.1], "flip":"none", "inverted":false}`
- `GET /api/snapshot/video.jpg` 和 `GET /api/snapshot/bili.jpg`

`/api/start` 还接受 `video_direct`、`video_no_proxy`、`bili_direct`、`auto_measure`、`offset_seconds`、`video_headers`、`bili_headers`。`video_no_proxy` 为布尔值，省略时沿用命令行默认值。ROI 是翻转后画面上的归一化 `[x,y,width,height]`，范围为 0–1；`source` 为 `video/bili`，`flip` 为 `none/h/v/hv`。POST 成功返回 202，参数错误返回 400，任务冲突返回 409。

`state.json` 只保存房间/域名对应的识别区域、翻转、线路文本和最后偏移。Cookie、请求头、带签名的直链不写入其中。保存过的偏移仅作为起点，不会静默标记为自动已对齐。可用 `--bili-cookies cookies.txt` 提供 Netscape 格式的 B 站 Cookie 文件。

## 当前实现边界

当前 `0.1.2` 在 P0/P1/P2 可运行基线上补齐比赛源直连、FFmpeg 崩溃处理和 Chromium 的 HLS 播放兼容性，含 P3 的周期 OCR 复核、连续两次漂移确认、B 站重解析及第三方错误重嗅探。音频 GCC-PHAT 模块尚未接入控制流程，时钟跳变处逐帧精细化留待后续阶段。

H.264 输出 MPEG-TS；HEVC 输出 fMP4，使用每次启动独立的初始化文件，浏览器仍需具备 HEVC 解码能力。AAC 音频直接复制，其他音频转 AAC，视频始终不转码。启动后 30 秒无新分片触发恢复；首次启动留出 150 秒缓冲窗口。`D` 包含源 PTS 起点差异，不能直接当作实际缓冲时长。

两个 B 站房间的真实取流、视频与音频复制、手动偏移、恢复及停止重连结果见 [0.1.2 验证记录](docs/validation-0.1.2.md)。本轮关闭 OCR，使用约 3 分钟的功能检查；两个房间播放不同内容，其中视频样本正在重播。同场比赛的同步精度、连续 2 小时运行、实际长延迟双路缓冲及手机 Safari 连播 30 分钟仍待验收。

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
