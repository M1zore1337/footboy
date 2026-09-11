# 0.1.0 基线验证

验证日期：2026-09-09。完整测试运行结果：**90 passed，0 skipped，63.05 秒**。

## 环境

| 项目 | 版本 / 条件 |
| --- | --- |
| 系统 | Ubuntu 26.04 LTS，x86_64 |
| Python | 3.14.4 |
| FFmpeg | 7.0.2 静态构建 |
| ffprobe | N-126455-gecc7eb519e-20260907 |
| PyAV | 18.1.0 |
| NumPy / OpenCV | 2.5.2 / 5.0.0.93 |
| Tesseract | 5.5.0，English 模型 |
| Playwright | 1.62.0，Chrome for Testing 151.0.7922.34 |
| 浏览器尺寸 | 1440×1080 和 390×844；后者是桌面浏览器的手机尺寸视口 |

旧 ffprobe 静态包读取 MPEG-TS 时出现段错误；同一输出可由 PyAV 正常读取。本次换用上表 ffprobe 后完成媒体与浏览器检查。测试程序存放在临时目录，通过参数/环境变量指定，不随 Footboy 发布。

## 已验证范围

| 检查 | 数量 | 验证内容 |
| --- | ---: | --- |
| 基础及 HTTP | 72 | URL/请求头校验、取流选择、Cookie 上下文、源 PTS、停表、手动优先、漂移确认、重连后的结果失效、启停、状态文件、HLS MIME/CORS/Range、WebUI 资源 |
| FFmpeg 媒体 | 7 | D 为 −60、−12.375、0、+7.5、+60 秒时音视频相对 PTS 正确，误差不超过 25ms；输出视频像素与输入相同；HEVC 初始化文件不被覆盖；连续分片、重启及进程退出 |
| 真实 Tesseract | 9 | 四种翻转、自动区域发现、停止时钟拒绝、正负偏移及独立 PTS 起点；估计误差不超过 1 秒 |
| 浏览器完整流程 | 2 | 从表单启动到实际 HLS 播放、采样截图、拖动框选、保存区域、偏移调整和停止；无页面脚本错误、无横向溢出、无外部页面依赖请求 |

浏览器独立回归测得调整后恢复播放为桌面 4.08 秒、手机尺寸 4.09 秒，包含 1.5 秒去抖。测试源按 4 倍速度发送合成 FLV，因此该时间仅说明本地流程可恢复，不能保证真实直播也在 5 秒内恢复。

正负 60 秒的媒体测试验证 PTS 变换，不代表已验证实际墙钟相差 60 秒的双路长时间缓冲。真实 OCR 测试使用清晰的合成 `mm:ss`，不代表所有直播字体、遮挡或比分布局都能自动识别。

另外已通过 Ruff 检查与格式检查、`node --check` 和 CLI 环境检查；源码包与 wheel 构建成功，wheel 内的应用代码、HTML/CSS/JavaScript、hls.js 和许可文件与工作区一致。

## 复现

按 README 安装开发依赖、FFmpeg、Tesseract 和 Playwright Chromium，然后运行：

```bash
# 以下示例适用于 macOS / Linux，路径替换为本机实际程序
FOOTBOY_FFMPEG=/path/to/ffmpeg \
FOOTBOY_FFPROBE=/path/to/ffprobe \
FOOTBOY_TESSERACT=/path/to/tesseract \
FOOTBOY_BROWSER_TESTS=1 \
python -m pytest -ra
```

Windows PowerShell 使用 `$env:变量名 = "值"` 设置上述变量后执行 `python -m pytest -ra`。程序都在 PATH 时可省略三个程序路径变量；浏览器测试仍需显式启用。缺少外部依赖的测试会跳过，检查最终结果是否包含 skipped。

## 下一阶段实播验收

- 提供同一场比赛的第三方页面与当前开播的 B 站直播间，验证真实时钟、请求上下文和选线行为。
- 对照人工时间记录检查 D 的方向与实际同步效果，覆盖两路实际延迟差较大的情况。
- 连续运行 2 小时，记录内存、分片时长、DTS 告警、断流/直链过期后的恢复和偏移调整时间。
- 用实体手机 Safari 连播至少 30 分钟；另行验证 Windows/macOS 的 FFmpeg、浏览器与退出清理。

这些项目尚未完成，不能由本地合成测试替代。
