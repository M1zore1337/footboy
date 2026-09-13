from __future__ import annotations

import argparse
import math
import signal
import sys
import threading
from pathlib import Path

from footboy.app import Application
from footboy.environment import check_binary as _check_binary
from footboy.i18n import ArgumentParser, configure_cli_language, tr
from footboy.serve.http import ControlServer
from footboy.supervisor import SupervisorConfig


def parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    configure_cli_language(argv)
    result = ArgumentParser(
        prog="footboy",
        allow_abbrev=False,
        description=tr(
            "Start the Footboy web console and synchronize match video with Bilibili audio using source PTS."
        ),
    )
    result.add_argument(
        "--video-page", help=tr("Match page URL; omit to enter it in the web console")
    )
    result.add_argument(
        "--bili-room", help=tr("Bilibili live room URL; provide together with --video-page")
    )
    result.add_argument(
        "--video-direct", action="store_true", help=tr("Treat the match URL as a direct media URL")
    )
    result.add_argument(
        "--video-line",
        help=tr("Stream label as shown on the match page; circled numbers are supported"),
    )
    result.add_argument(
        "--video-no-proxy",
        action="store_true",
        help=tr("Bypass proxies for the match page and media stream"),
    )
    result.add_argument(
        "--bili-direct",
        action="store_true",
        help=tr("Treat the Bilibili URL as a direct media URL (P0)"),
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=Path("hls_out"),
        help=tr("HLS output directory (default: hls_out)"),
    )
    result.add_argument(
        "--state-file",
        type=Path,
        default=Path("state.json"),
        help=tr("Saved settings file (default: state.json)"),
    )
    result.add_argument("--host", default="0.0.0.0", help=tr("Listen address (default: 0.0.0.0)"))
    result.add_argument("--port", type=int, default=8080, help=tr("Listen port (default: 8080)"))
    result.add_argument(
        "--ffmpeg", default="ffmpeg", help=tr("Executable path; by default search PATH, then tools")
    )
    result.add_argument(
        "--ffprobe",
        default="ffprobe",
        help=tr("Executable path; by default search PATH, then tools"),
    )
    result.add_argument("--bili-cookies", type=Path, help=tr("Optional Netscape cookies.txt file"))
    result.add_argument(
        "--ocr",
        choices=("auto", "rapidocr", "tesseract"),
        default="auto",
        help=tr("OCR engine (default: auto)"),
    )
    result.add_argument(
        "--tesseract-command", help=tr("Executable path; by default search PATH, then tools")
    )
    result.add_argument(
        "--offset",
        type=float,
        help=tr("Initial signed offset D in seconds; positive values delay Bilibili audio"),
    )
    result.add_argument(
        "--no-auto-measure",
        action="store_true",
        help=tr("Disable startup and periodic OCR; adjust manually"),
    )
    result.add_argument(
        "--headless-sniff",
        action="store_true",
        help=tr("Discover streams without showing a browser window"),
    )
    result.add_argument("--verify-interval", type=float, default=120.0, help=argparse.SUPPRESS)
    result.add_argument("--check", action="store_true", help=tr("Check FFmpeg / ffprobe and exit"))
    return result


def main(argv: list[str] | None = None) -> int:
    argument_parser = parser(argv)
    args = argument_parser.parse_args(argv)
    if bool(args.video_page) != bool(args.bili_room):
        argument_parser.error(tr("Provide both --video-page and --bili-room, or omit both"))
    if not 0 <= args.port <= 65535:
        argument_parser.error(tr("--port must be between 0 and 65535"))
    if args.offset is not None and not math.isfinite(args.offset):
        argument_parser.error(tr("--offset must be a finite number"))
    if not math.isfinite(args.verify_interval) or args.verify_interval < 1:
        argument_parser.error(tr("--verify-interval must be at least 1 second"))
    if args.check:
        try:
            _check_binary(args.ffmpeg, minimum_major=6)
            _check_binary(args.ffprobe)
        except RuntimeError as exc:
            print(tr("Environment check failed: {0}", exc), file=sys.stderr)
            return 2
        print(tr("FFmpeg / ffprobe checks passed"))
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
        video_no_proxy=args.video_no_proxy,
        video_line_text=args.video_line,
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
        print(tr("Footboy failed to start: {0}", exc), file=sys.stderr)
        return 1
    finally:
        print(tr("Stopping Footboy…"), flush=True)
        app.close()
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
