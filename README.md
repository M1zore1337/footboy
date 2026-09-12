# Footboy（足小子）

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Code style: ruff](https://img.shields.io/badge/code%20style-ruff-000000.svg)](https://github.com/astral-sh/ruff)
[![Platform: Windows | macOS | Linux](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey.svg)]()

把比赛直播画面与 B 站直播间的解说同步组合，输出局域网 HLS。

![Footboy 控制台](docs/images/webui-1440.png)

- **视频复制**：画面始终复制原画质，音频按需转为 AAC。
- **自动 / 手动同步**：OCR 读取两路比赛计时；手动偏移优先，过期测量不覆盖新设置。
- **切换线路**：按页面名称选择线路，新源探测通过后接替播放。
- **独立音量**：两路分别控制音量和静音，可混入原直播声音。
- **多端观看**：支持手机（Safari）、平板、电视盒子与各类网络播放器局域网同看。

```mermaid
flowchart TD
    subgraph Input ["1. 直播源接入"]
        V["比赛直播页面 / 媒体直链<br/>(视频画面)"]
        B["B 站直播间 / 媒体直链<br/>(解说音频)"]
    end

    subgraph Core ["2. Footboy 混流核心"]
        OCR["OCR 时钟识别 / 采样<br/>(自动连续走表或交互框选 ROI)"]
        Sync["源 PTS 对齐 & 偏移微调<br/>(D = K_B - K_V)"]
        FFmpeg["FFmpeg 混流器<br/>(画面复制 + itsoffset 音频)"]
    end

    subgraph Output ["3. 多端分发与控制"]
        WebUI["Web 控制台 (8080 端口)<br/>(实时画面 / 偏移微调 / 独立音量)"]
        HLS["局域网 HLS 串流<br/>(/live.m3u8)"]
        Client["手机 (Safari) / 平板 / 智能电视 / VLC"]
    end

    V --> OCR
    B --> OCR
    OCR --> Sync
    V --> FFmpeg
    B --> FFmpeg
    Sync --> FFmpeg
    FFmpeg --> HLS
    HLS --> WebUI
    HLS --> Client
```

## 目录

- [快速开始](#快速开始)
- [外部依赖](#外部依赖)
- [连接与观看](#连接与观看)
- [跨设备与局域网观看](#跨设备与局域网观看)
- [常见问题](#常见问题)
- [同步原理](#同步原理)
- [命令行](#命令行)
- [控制接口](#控制接口)
- [本地数据](#本地数据)
- [验证与限制](#验证与限制)
- [开发](#开发)
- [许可](#许可)

## 快速开始

**依赖**：Python 3.10+、[FFmpeg 6.0+ 和 ffprobe](https://ffmpeg.org/download.html)、[Tesseract](https://tesseract-ocr.github.io/tessdoc/Installation.html)（自动同步用）。页面取流使用本机 Chrome、Edge 或 Playwright Chromium。

外部程序可加入 PATH，也可放入项目 `tools` 目录，见下文。Tesseract 需英文模型 `eng`。

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate

# Windows PowerShell
py -3 -m venv .venv; .\.venv\Scripts\Activate.ps1
```

```bash
python -m pip install --upgrade pip
python -m pip install ".[tesseract]"                  # 源码开发模式可用 -e ".[tesseract]"
python -m playwright install chromium                 # Linux 若缺失系统依赖改用: python -m playwright install --with-deps chromium
footboy --check                                       # 确认 FFmpeg 6.0+ 和 ffprobe 可用
footboy                                               # 启动服务（默认监听 0.0.0.0:8080）
```

打开终端打印的控制页链接（带 `#token=...`），或打开 <http://127.0.0.1:8080/> 并输入终端中的本次控制密钥。退出时在终端按 Ctrl+C。

> **启动提示**：默认以 `--ocr auto` 启动，会自动匹配已安装的 Tesseract 或 RapidOCR；若仅供本机访问，可指定 `footboy --host 127.0.0.1`。`footboy --check` 专门用于检查 FFmpeg 与 ffprobe 环境。
>
> **其他 OCR 方案**：Python 3.10–3.12 可用 `pip install ".[rapidocr]"` + `--ocr rapidocr`。只需手动同步时安装基础包 `pip install .`，以 `--no-auto-measure --offset 0` 启动。
>
> **Windows 注意**：若没有 `py` 命令可用 `python`。若 PowerShell 阻止脚本激活，可直接使用 `.\.venv\Scripts\python.exe` 和 `.\.venv\Scripts\footboy.exe`。

## 外部依赖

查找顺序：**自定义程序路径 → 系统 PATH → 当前目录的 tools → 源码项目的 tools**。找到后仍会检查版本；PATH 中的旧版本不会自动跳过，可用参数指定新版。通过 wheel 安装时，从含 `tools` 的目录启动。

```text
tools/
├─ ffmpeg/bin/
│  ├─ ffmpeg.exe
│  └─ ffprobe.exe
└─ tesseract/
   ├─ tesseract.exe
   ├─ 配套 DLL 等文件
   └─ tessdata/eng.traineddata
```

**Windows**

1. 从 [FFmpeg 下载页](https://ffmpeg.org/download.html)进入 Windows builds，下载 Gyan 的 release essentials ZIP。解压到 `tools/ffmpeg`，确保 `bin` 下有两个 EXE；保留配套文件。
2. 从 [Tesseract 安装说明](https://tesseract-ocr.github.io/tessdoc/Installation.html)进入 UB Mannheim 下载页。安装到项目的 `tools/tesseract`，或将完整安装目录复制到此处，保留 DLL 和英文模型。
3. 在项目根目录验证，无需修改 PATH：

```powershell
footboy --check
& ".\tools\tesseract\tesseract.exe" --list-langs
```

语言列表应包含 `eng`。缺少时，将 [eng.traineddata](https://github.com/tesseract-ocr/tessdata_fast/raw/main/eng.traineddata) 放入 `tools/tesseract/tessdata`。

**macOS / Linux**

目录结构同上，可执行文件不带 `.exe`；Tesseract 也支持放在 `tools/tesseract/bin`。使用与系统、CPU 架构匹配的构建，保留动态库和语言数据，并确保程序有执行权限。FFmpeg 可从[官方列出的构建](https://ffmpeg.org/download.html)下载后解压整理到 `tools/ffmpeg/bin`。

Tesseract 建议通过系统包管理器安装，避免单独复制程序导致动态库缺失：

```bash
# macOS（已安装 Homebrew）
brew install ffmpeg tesseract

# Ubuntu / Debian（FFmpeg 需 6.0+，旧系统的软件源可能不满足）
sudo apt update
sudo apt install ffmpeg tesseract-ocr tesseract-ocr-eng
```

安装 Tesseract 到工具目录：按[编译说明](https://tesseract-ocr.github.io/tessdoc/Compiling.html)准备依赖，在 Tesseract 源码目录依次执行 `./autogen.sh`、`./configure --prefix="/绝对路径/footboy/tools/tesseract"`、`make`、`make install`，再将 `eng.traineddata` 放入该前缀下的 `share/tessdata`。

**自定义路径**

```bash
footboy --ffmpeg "/path/to/ffmpeg" --ffprobe "/path/to/ffprobe" --ocr tesseract --tesseract-command "/path/to/tesseract"
```

Windows 路径同样用引号包裹。相对路径以终端当前目录为准；指定路径无效时直接报错。`tools` 已被 Git 忽略，外部程序不随 Footboy 发布。Python 依赖仍按快速开始安装；浏览器用本机 Chrome/Edge 或 `python -m playwright install chromium`。

## 连接与观看

1. 在控制页输入比赛页面和音源直播间的地址。媒体直链需勾选对应选项。
2. 可填写线路名称，点击连接。嗅探窗口会出现在运行 Footboy 的电脑上。
3. 等待媒体探测和同步。计时无法识别时，选画面方向，框住计时区域并保存；或使用偏移按钮。
4. 按需开启原声、调整音量或选择新线路。

自动同步要求两路画面都显示同一场比赛的连续计时。六档偏移按钮调整 ±0.1 / ±0.5 / ±2 秒，连续点击在停顿 1.5 秒后合并应用。"立即同步"重新采样计时并刷新混流。

> **访问控制**：每次启动服务会生成新的控制密钥，控制 API、状态和采样截图均需验证；网页拒绝外站控制请求。控制链接只交给可信操作者，密钥在当前浏览器标签页保存，重启服务后需使用新链接。HLS 播放地址无需密钥，可交给局域网播放器。
>
> 服务使用 HTTP，仍应仅在可信的本机或局域网运行。支持本机和内网媒体直链，浏览器与媒体客户端仍可访问主机能连通的地址；仅连接可信来源。

## 跨设备与局域网观看

Footboy 默认监听 `0.0.0.0:8080`。启动后控制台和终端会提示本机的局域网访问地址（例如 `http://192.168.1.100:8080/`）：

- **手机 / 平板（iOS / iPadOS / macOS）**：在同一局域网 Wi-Fi 下用 Safari 打开终端中含密钥的控制页链接，或打开 `http://<局域网IP>:8080/live.m3u8`，利用苹果原生 HLS 硬件加速播放。
- **智能电视 / 电视盒子 / PC 播放器**：在 Apple TV、Android 电视盒子、PC 上的 VLC、IINA、PotPlayer 或 Kodi 中选择「打开网络串流 / 串流地址」，输入 `http://<局域网IP>:8080/live.m3u8` 即可大屏观看。
- **防火墙设置**：Windows 用户首次启动时，请在系统防火墙弹窗中允许「专用网络访问」；若其他设备无法连接，请放行对应端口（默认 8080）。
- **播放延迟说明**：总延迟由较慢源自身的直播延迟，加上局域网 HLS 分片缓冲（通常约为 **6–10 秒**）组成。

<details>
<summary><b>各系统防火墙放行参考（以默认 8080 端口为例）</b></summary>

- **Windows（以管理员身份运行 PowerShell）**：
  ```powershell
  New-NetFirewallRule -DisplayName "Footboy" -Direction Inbound -LocalPort 8080 -Protocol TCP -Action Allow
  ```
- **Linux (Ubuntu / Debian - UFW)**：
  ```bash
  sudo ufw allow 8080/tcp
  ```
- **Linux (CentOS / RHEL / openSUSE - firewalld)**：
  ```bash
  sudo firewall-cmd --permanent --add-port=8080/tcp && sudo firewall-cmd --reload
  ```
- **macOS**：
  进入「系统设置 → 网络 → 防火墙」，确保未开启「阻止所有传入连接」，并在弹出提示时允许 Python 接收外部网络传入连接。

</details>

## 常见问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 FFmpeg / ffprobe 或版本过旧 | 安装到 PATH 或 `tools`，也可用 `--ffmpeg` / `--ffprobe` 指定路径，再运行 `footboy --check` |
| 找不到 Tesseract 或英文模型 | 安装到 PATH 或 `tools`；用实际程序路径执行 `--list-langs`，确认含 `eng`；可用 `--tesseract-command` 指定路径 |
| Linux 浏览器缺少系统库 | 运行 `python -m playwright install --with-deps chromium` |
| 无图形桌面 / NAS 运行 | 用 `--headless-sniff`（不支持手动选线）或改用媒体直链 |
| 无法播放 HEVC | 选择 H.264 线路，或使用支持 HEVC 的播放器 |
| 自动识别一直"未对齐" | 确认两路有走动的同场计时，重新框选区域，或改用手动同步 |
| 局域网其他设备打不开网页 | 检查运行 Footboy 电脑的防火墙设置，放行对应端口（默认 8080） |

## 同步原理

```text
比赛时钟 = 源 PTS + K
D = K_B - K_V
```

`V` 为比赛画面，`B` 为 B 站音源。FFmpeg 在 B 站输入上应用 `-itsoffset D`：正值推后声音，负值提前。`D` 包含两路源 PTS 起点差异。更换线路后需重新确认偏移。

**日常手动调偏直觉**：
- **解说剧透（声音比画面快）**：需推后声音，点击 `+0.1s` / `+0.5s` / `+2s` 按钮（或输入正偏移）。
- **解说滞后（动作发生后解说才喊）**：需提前声音，点击 `−0.1s` / `−0.5s` / `−2s` 按钮（或输入负偏移）。

## 命令行

以下假定 `FOOTBOY_VIDEO_PAGE_URL` 和 `FOOTBOY_BILI_ROOM_URL` 已设置：

```bash
footboy \
  --video-page "$FOOTBOY_VIDEO_PAGE_URL" \
  --bili-room "$FOOTBOY_BILI_ROOM_URL"
```

也可不传地址，启动后从控制页输入。PowerShell 中变量写作 `$env:FOOTBOY_VIDEO_PAGE_URL`。

| 参数 | 默认值 | 用途 |
| --- | --- | --- |
| `--video-page URL` | - | 比赛页面 URL（与 `--bili-room` 配对使用，亦可从控制页输入） |
| `--bili-room URL` | - | B 站直播间 URL |
| `--video-line "名称"` | - | 按页面文字选择线路（例如：`高清直播5`；支持圈号数字） |
| `--video-no-proxy` | `false` | 比赛页面及媒体请求直连，不使用代理 |
| `--video-direct` | `false` | 将比赛地址直接解释为媒体直链 |
| `--bili-direct` | `false` | 将 B 站地址直接解释为媒体直链 |
| `--headless-sniff` | `false` | 嗅探时不显示浏览器窗口（适用于无桌面服务器或 NAS） |
| `--ocr {auto,rapidocr,tesseract}` | `auto` | OCR 后端引擎；`auto` 自动检测已安装的引擎 |
| `--tesseract-command PATH` | `tesseract` | 自定义 Tesseract 可执行文件路径 |
| `--no-auto-measure` | `false` | 关闭启动和周期 OCR 测量，改用纯手动调节 |
| `--offset 秒数` | - | 初始有符号偏移秒数；正值推后 B 站音频 |
| `--bili-cookies FILE` | - | Netscape 格式的 Cookie 文件路径 |
| `--ffmpeg PATH` / `--ffprobe PATH` | `ffmpeg` / `ffprobe` | 自定义 FFmpeg / ffprobe 程序路径 |
| `--host HOST` / `--port PORT` | `0.0.0.0` / `8080` | 服务监听地址与端口 |
| `--state-file FILE` | `state.json` | 状态持久化文件位置 |
| `--output-dir DIR` | `hls_out` | HLS 输出分片存储目录 |
| `--check` | - | 快速检查本地 FFmpeg 与 ffprobe 环境并退出 |

```bash
# footboy-p0 手工验证两条媒体直链（无需 OCR）
footboy-p0 --video-url "http://.../video.m3u8" --bili-url "http://.../audio.m3u8" --offset 1.5
```

完整选项见 `footboy --help` 与 `footboy-p0 --help`。

## 控制接口

所有 `/api/*` 请求（含状态、截图和 HEAD）必须携带 `Authorization: Bearer <本次控制密钥>`。密钥来自启动终端，不通过 URL 查询参数传递，也不写入公开页面或状态文件。网页通过链接片段读取密钥后会清除地址栏中的片段。

POST 请求必须声明 `Content-Type: application/json`。成功返回 202；参数错误 400、缺少或无效密钥 401、外站请求 403、任务冲突 409、请求类型错误 415。非浏览器客户端可以省略 Origin，浏览器请求只允许服务自身的 Origin。

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

无法读取或解析状态文件时会记录警告并使用默认设置，加载操作保留原文件；后续保存设置仍会写入该路径。

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

可按耗时分组：`python -m pytest -ra -m "not slow"` 运行快速回归，`-m "slow and not browser"` 运行实际媒体和 OCR，启用浏览器环境变量后以 `-m browser` 运行网页流程。默认仍收集完整测试。

GitHub Actions 分别运行快速检查、媒体/OCR 和 Chromium 回归；快速检查覆盖 Python 3.10、3.14。集成任务在测试前要求 FFmpeg 6+、Tesseract 英文模型与 Python 绑定可用，浏览器任务显式安装并启用 Chromium，避免缺少依赖时仅凭跳过结果通过。

每次提交必须更新并暂存 [CHANGELOG.md](CHANGELOG.md)。

## 许可

代码采用 [MIT License](LICENSE)。随包提供的 hls.js 采用 Apache-2.0，详见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。

软件许可不授予比赛画面、直播解说或平台商标的使用权。请仅处理你有权访问的内容，遵守来源平台的使用条款。
