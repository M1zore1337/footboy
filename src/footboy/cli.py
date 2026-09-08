from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
from pathlib import Path

from footboy.app import Application
from footboy.environment import check_binary as _check_binary
from footboy.serve.http import ControlServer
from footboy.supervisor import SupervisorConfig


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="footboy",
        description="启动 Footboy WebUI，将比赛画面与 B站音源按源 PTS 对齐。",
    )
    result.add_argument("--video-page", help="第三方比赛页面 URL；省略后从 WebUI 输入")
    result.add_argument("--bili-room", help="B站直播间 URL；与 --video-page 同时填写")
    result.add_argument("--video-direct", action="store_true", help="比赛地址为媒体直链")
    result.add_argument("--bili-direct", action="store_true", help="B站地址为媒体直链（P0）")
    result.add_argument("--output-dir", type=Path, default=Path("hls_out"))
    result.add_argument("--state-file", type=Path, default=Path("state.json"))
    result.add_argument("--host", default="0.0.0.0")
    result.add_argument("--port", type=int, default=8080)
    result.add_argument("--ffmpeg", default="ffmpeg")
    result.add_argument("--ffprobe", default="ffprobe")
    result.add_argument("--bili-cookies", type=Path, help="可选 Netscape cookies.txt")
    result.add_argument("--ocr", choices=("auto", "rapidocr", "tesseract"), default="auto")
    result.add_argument("--tesseract-command", help="Windows 上 tesseract.exe 的完整路径")
    result.add_argument("--offset", type=float, help="初始有符号 D 秒；正值推后 B站音频")
    result.add_argument(
        "--no-auto-measure", action="store_true", help="关闭启动和周期 OCR，手动调节"
    )
    result.add_argument("--headless-sniff", action="store_true", help="自动嗅探时不显示浏览器窗口")
    result.add_argument("--verify-interval", type=float, default=120.0, help=argparse.SUPPRESS)
    result.add_argument("--check", action="store_true", help="只检查 FFmpeg / ffprobe 环境")
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser()
    args = argument_parser.parse_args(argv)
    if bool(args.video_page) != bool(args.bili_room):
        argument_parser.error("--video-page 与 --bili-room 必须同时提供，也可以都省略")
    if not 0 <= args.port <= 65535:
        argument_parser.error("--port 必须在 0..65535")
    if args.offset is not None and not math.isfinite(args.offset):
        argument_parser.error("--offset 必须是有限数值")
    if not math.isfinite(args.verify_interval) or args.verify_interval < 1:
        argument_parser.error("--verify-interval 必须至少为 1 秒")
    if args.check:
        try:
            _check_binary(args.ffmpeg, minimum_major=6)
            _check_binary(args.ffprobe)
        except RuntimeError as exc:
            print(f"环境检查失败: {exc}", file=sys.stderr)
            return 2
        print("FFmpeg / ffprobe 检查通过")
        return 0
    config = SupervisorConfig(
        video_page_url=args.video_page or "",
        bili_room_url=args.bili_room or "",
        output_dir=args.output_dir.resolve(),
        state_file=args.state_file.resolve(),
        host=args.host,
        port=args.port,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        cookies_file=args.bili_cookies,
        ocr_backend=args.ocr,
        tesseract_command=args.tesseract_command,
        auto_measure=not args.no_auto_measure,
        initial_offset=args.offset,
        verify_interval=args.verify_interval,
        video_direct=args.video_direct,
        bili_direct=args.bili_direct,
        headless_sniff=args.headless_sniff,
    )
    app = Application(config)
    server = ControlServer(app, config.output_dir, host=args.host, port=args.port)
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    try:
        server.start()
        if args.video_page:
            app.request_start({"video_url": args.video_page, "bili_url": args.bili_room})
        while not stopping.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        print(f"Footboy 启动失败: {exc}", file=sys.stderr)
        return 1
    finally:
        print("正在停止 Footboy……", flush=True)
        app.close()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
