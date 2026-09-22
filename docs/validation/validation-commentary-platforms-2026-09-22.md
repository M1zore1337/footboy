# 多平台解说源验证（2026-09-22）

本轮扩展解说输入：B 站保留专用解析；其他直播间使用 Streamlink、yt-dlp，提取失败时进入 Chromium 网页嗅探。Web UI、CLI 和启动 API 均接受通用解说地址，旧 `bili_*` 字段及 `--bili-*` 参数继续兼容。

## 环境

Linux / WSL2，Python 3.14.4，Streamlink 8.6.1，yt-dlp 2026.8.19，urllib3 2.7.0，PyAV 18.1.0，Playwright 1.62.0，pytest 9.1.1。FFmpeg / ffprobe 为 2026-09-03 的 `N-126390-g9fc8c785e2` 构建。

依赖最小版本有实际依据：Streamlink 7.6.0 不含斗鱼插件，因此要求 8.6.1+；当前 Streamlink 与 yt-dlp 同时修改 urllib3 的 URL 处理逻辑，与 urllib3 2.8 的接口存在冲突，因此暂约束 `urllib3<2.8`。两种导入顺序的 HTTP 请求构造均有子进程回归覆盖。

## 真实平台验证

没有使用用户登录 Cookie。本记录仅保留平台名称、音视频元信息和验证结果；真实房间号、房间地址、签名媒体地址、临时分片及调试日志未加入仓库。使用文档中的地址均以 `ROOM_ID` 占位。

| 平台 | 取流与探测 | 持续混流验证 |
| --- | --- | --- |
| 斗鱼 | Streamlink `douyu`；FLV，H.264 1920×1080，AAC | 使用本地比赛视频及公开解说样本，由 Supervisor 解析、估计时间轴并混流；连续生成 10 个完整 HLS 分片，并发取帧后仍继续产出 |
| 虎牙 | Streamlink `huya`；FLV，H.264 1920×1080，AAC | 使用本地比赛视频及公开解说样本，约 4.13 秒生成至少 3 个分片；选中 2 秒分片含 86 个 AAC 包，完整解码通过 |

斗鱼最终验证使用正常 15 秒取帧窗口：混流期间读取到一帧 1920×1080 关键帧，随后继续生成至少 2 个分片；最后 3 个完整分片各含 50 个 H.264 视频包及 94、93、94 个 AAC 音频包，全部解码成功。约 16.8 秒的观察期间无 HTTP 截断、FLV 拼接错误、DTS 错误或自动恢复，混流代次保持不变。

这些公开样本用于验证取流、音频包、持续封装和并发取帧，不是两路同场足球比赛的 OCR 内容对齐验收。其他平台的上游插件分派、提取器回退和格式选择有自动化覆盖，本轮没有逐个平台进行外网实播验收。

## 斗鱼直链复用问题与修复

同一斗鱼鉴权直链被 ffprobe、时间轴采样和 FFmpeg 先后或并发使用时，后续读取会在 FLV 包中途提前结束。简单 HTTP 重连可能继续产生 FLV 拼接和异常 DTS；关闭代理或禁用 seek 也未解决这一样本的问题。

分别重新解析两条签名地址后，同机并发的 PyAV 与 FFmpeg 能持续读取。修复因此为斗鱼 `Source` 提供运行时 `reader_factory`，每个实际读取器通过 `for_reader()` 获取自己的地址：媒体探测、时间轴采样、OCR、音频采样及混流均已接入。解码所需的探测元信息、请求头、Cookie 和直连设置继续传递；回调不进入序列化或公开状态。其他平台不增加此轮额外解析请求。

真实新接口对照中，FFmpeg 持续达到 494 帧，PyAV 同时读取 683 个视频包、1065 个音频包，无读取错误。自动化媒体回归另使用只能消费一次的本地签名地址，强制验证探测、时间轴、OCR 和混流各取得独立 URL。

## 自动化覆盖

完整回归 **503 项通过，0 跳过**，耗时 240.82 秒，包含真实本地媒体、OCR 和浏览器测试。Ruff、格式、JavaScript / Shell 语法及差异检查通过。

- 新旧 CLI/API 参数、其他平台地址、冲突参数与错误输入。
- Streamlink 真实插件分派及流类型，yt-dlp 回退、移动/分享链接规范化、音视频格式筛选、取消与错误脱敏。
- 请求头大小写覆盖、带域和路径的 Cookie、Netscape 会话 Cookie、鉴权头与签名查询参数保留。
- 真实 AES-128 HLS 解说：Cookie 经 ffprobe、PyAV、FFmpeg 传递，输出画面像素和相对 PTS 保持正确。
- 真实 Chromium 网页回退：加载 Cookie 文件，捕获页面生成的 Authorization 和自定义令牌，并完成媒体探测与后续取帧。
- 原有混流、重连、音量、ROI/OCR、中英文、桌面和手机浏览器流程。

完整验证命令：

```bash
FOOTBOY_BROWSER_TESTS=1 .venv/bin/python -m pytest -ra
.venv/bin/python -m ruff check src scripts tests
.venv/bin/python -m ruff format --check src scripts tests
node --check src/footboy/serve/static/app.js
node --check src/footboy/serve/static/i18n.js
.venv/bin/python -m build
```

## 支持范围

目标是可访问的 HTTP(S) 直播媒体，解说源需同时含画面和音轨。未开播、需要登录、地区限制、签名失效、平台改版或提取器尚未适配均可能导致失败；DRM、仅 WebRTC、需要独立合并音视频轨的流以及纯音频解说不在本轮支持范围内。自动 OCR 仍要求两路画面显示同场比赛计时；没有计时牌时可以关闭自动测量并手动调偏。
