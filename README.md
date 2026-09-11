# Footboy（足小子）

把比赛直播画面与 B 站直播间的解说同步组合，输出局域网 HLS。

- **视频复制**：画面始终复制，音频按需转为 AAC。
- **自动 / 手动同步**：OCR 读取两路比赛计时；手动偏移优先，过期测量不覆盖新设置。
- **切换线路**：按页面名称选择线路，新源探测通过后接替播放。
- **独立音量**：两路分别控制音量和静音，可混入原直播声音。

## 快速开始

**依赖**：Python 3.10+、[FFmpeg 6.0+ 和 ffprobe](https://ffmpeg.org/download.html)、[Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)（自动同步用）。页面取流使用本机 Chrome、Edge 或 Playwright Chromium。

确保 FFmpeg、ffprobe、Tesseract 及英文模型在 PATH 中（`tesseract --list-langs` 应包含 `eng`）。

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate

# Windows PowerShell
py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1
```

```bash
python -m pip install --upgrade pip
python -m pip install -e ".[tesseract]"
python -m playwright install chromium
footboy --check
footboy --host 127.0.0.1 --ocr tesseract
```

打开 <http://127.0.0.1:8080/>。`--check` 确认 FFmpeg 版本和 ffprobe 可用。退出时在终端按 Ctrl+C。

> **其他 OCR 方案**：Python 3.10–3.12 可用 `pip install -e ".[rapidocr]"` + `--ocr rapidocr`。只需手动同步时安装基础包 `pip install -e .`，以 `--no-auto-measure --offset 0` 启动。

> **Windows 注意**：若没有 `py` 命令可用 `python`。若 PowerShell 阻止脚本激活，可直接使用 `.\.venv\Scripts\python.exe` 和 `.\.venv\Scripts\footboy.exe`。

## 连接与观看

1. 在控制页输入比赛页面和音源直播间的地址。媒体直链需勾选对应选项。
2. 可填写线路名称，点击连接。嗅探窗口会出现在运行 Footboy 的电脑上。
3. 等待媒体探测和同步。计时无法识别时，选画面方向，框住计时区域并保存；或使用偏移按钮。
4. 按需开启原声、调整音量或选择新线路。

自动同步要求两路画面都显示同一场比赛的连续计时。六档偏移按钮调整 ±0.1 / ±0.5 / ±2 秒，连续点击在停顿 1.5 秒后合并应用。"立即同步"重新采样计时并刷新混流。

局域网观看用 `footboy` 启动（默认 `0.0.0.0:8080`），其他设备打开终端显示的地址或使用 `/live.m3u8`。

> **安全提示**：控制 API 无身份认证且允许跨域请求，仅在可信的本机或局域网使用。

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 FFmpeg / ffprobe 或版本过旧 | 安装并加入 PATH，或用 `--ffmpeg` / `--ffprobe` 指定路径，再运行 `footboy --check` |
| 找不到 Tesseract 或英文模型 | 确认 `tesseract --list-langs` 含 `eng`；必要时用 `--tesseract-command` 指定路径 |
| Linux 浏览器缺少系统库 | 运行 `python -m playwright install --with-deps chromium` |
| 无图形桌面 | 用 `--headless-sniff`（不支持手动选线）或改用媒体直链 |
| 无法播放 HEVC | 选择 H.264 线路，或使用支持 HEVC 的播放器 |
| 自动识别一直"未对齐" | 确认两路有走动的同场计时，重新框选区域，或改用手动同步 |

## 同步原理

```text
比赛时钟 = 源 PTS + K
D = K_B - K_V
```

`V` 为比赛画面，`B` 为 B 站音源。FFmpeg 在 B 站输入上应用 `-itsoffset D`：正值推后声音，负值提前。`D` 包含两路源 PTS 起点差异。更换线路后需重新确认偏移。

## 命令行

以下假定 `FOOTBOY_VIDEO_PAGE_URL` 和 `FOOTBOY_BILI_ROOM_URL` 已设置：

```bash
footboy \
  --video-page "$FOOTBOY_VIDEO_PAGE_URL" \
  --bili-room "$FOOTBOY_BILI_ROOM_URL"
```

也可不传地址，启动后从控制页输入。PowerShell 中变量写作 `$env:FOOTBOY_VIDEO_PAGE_URL`。

| 参数 | 用途 |
| --- | --- |
| `--video-line "线路名称"` | 按页面文字选择线路 |
| `--video-no-proxy` | 比赛页面及媒体请求直连 |
| `--video-direct` / `--bili-direct` | 将输入解释为媒体直链 |
| `--no-auto-measure --offset 0` | 关闭 OCR，手动偏移 |
| `--bili-cookies cookies.txt` | Netscape 格式 Cookie 文件 |
| `--ffmpeg` / `--ffprobe` | 指定可执行文件路径 |
| `--host` / `--port` | 监听地址和端口 |
| `--state-file` / `--output-dir` | 状态和 HLS 输出位置 |

`footboy-p0` 用于手工验证两条媒体直链，不执行自动 OCR。完整选项见 `footboy --help` 与 `footboy-p0 --help`。

## 控制接口

POST 请求体采用 JSON；成功返回 202，参数错误 400，任务冲突 409。

| 接口 | 说明 |
| --- | --- |
| `GET /api/status` | 当前状态 |
| `POST /api/start` | `video_url`、`bili_url` 必填；可选 `video_direct`、`bili_direct`、`video_no_proxy`、`video_line_text`、`auto_measure`、`offset_seconds`、`video_headers`、`bili_headers` |
| `POST /api/stop` | 停止任务 |
| `POST /api/offset` | `{"delta_ms": 500}` 相对调整偏移 |
| `POST /api/remeasure` | 重新采样并同步 |
| `POST /api/audio` | `{"original_enabled":true,"original_volume":0.25,"commentary_volume":0.8}` 支持部分字段，音量 0–1 |
| `POST /api/line` | `{"text":"线路名称"}` |
| `POST /api/resniff` | 重新打开浏览器选线 |
| `POST /api/source` | `{"id":1}` 选择候选；`{"id":null}` 暂停自动选择 |
| `POST /api/roi` | `{"source":"video","roi":[0.1,0.1,0.2,0.1],"flip":"none","inverted":false}` |
| `GET /api/snapshot/video.jpg` | 比赛画面截图 |
| `GET /api/snapshot/bili.jpg` | 音源截图 |

## 本地数据

`state.json` 保存识别区域、翻转、线路和偏移等设置，按来源域名区分。Cookie、请求头和签名地址不写入状态文件。分享日志或截图前需脱敏。

`.gitignore` 排除虚拟环境、状态文件、HLS 产物、Cookie 及 agent 配置文件。自定义路径也应排除在版本控制之外。

## 验证与限制

- H.264 输出 MPEG-TS HLS，HEVC 输出 fMP4 HLS。Safari 走原生 HLS，支持 MSE 的浏览器优先用包内 hls.js。
- OCR 需至少 3 帧连续走表；停表、遮挡或无计时画面可能无法自动对齐。
- 历史验证见 [基线](docs/validation/validation.md)、[0.1.1](docs/validation/validation-0.1.1.md)、[0.1.2](docs/validation/validation-0.1.2.md)、[0.1.3](docs/validation/validation-0.1.3.md)、[0.1.4](docs/validation/validation-0.1.4.md)。
- **待验收**：不同来源同场精确同步、连续两小时运行、Windows/macOS 原生、实体手机 Safari。

## 开发

```bash
python -m pip install -e ".[dev,tesseract]"
git config core.hooksPath .githooks        # ZIP 下载可跳过
python -m pytest -ra
ruff check src tests
ruff format --check src tests
node --check src/footboy/serve/static/app.js  # 需要 Node.js
python -m build
```

测试依赖实际 FFmpeg / ffprobe / Tesseract；缺少时对应测试跳过。可通过 `FOOTBOY_FFMPEG`、`FOOTBOY_FFPROBE`、`FOOTBOY_TESSERACT` 指定路径。浏览器回归需设置 `FOOTBOY_BROWSER_TESTS=1`。

每次提交必须更新并暂存 [CHANGELOG.md](CHANGELOG.md)。

## 许可

代码采用 [MIT License](LICENSE)。随包提供的 hls.js 采用 Apache-2.0，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

软件许可不授予比赛画面、直播解说或平台商标的使用权。请仅处理你有权访问的内容，遵守来源平台的使用条款。
