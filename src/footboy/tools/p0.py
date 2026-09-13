from __future__ import annotations

import argparse
import queue
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from footboy.cli import _check_binary
from footboy.i18n import ArgumentParser, configure_cli_language, tr
from footboy.mux.ffmpeg import FfmpegMuxer
from footboy.serve.http import ControlServer
from footboy.sources.media_probe import ffprobe_source
from footboy.sources.models import Source


class P0Controller:
    def __init__(self, muxer: FfmpegMuxer, video: Source, bili: Source, offset: float) -> None:
        self.muxer = muxer
        self.video = video
        self.bili = bili
        self.offset = offset
        self.message = tr("P0 manual muxing is running")
        self.events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self.started_at = time.time()

    def public_status(self) -> dict[str, Any]:
        return {
            "state": "P0-RUN",
            "aligned": True,
            "offset_seconds": round(self.offset, 3),
            "confidence": {"method": "manual-p0"},
            "last_verified_at": None,
            "message": self.message,
            "ffmpeg": self.muxer.health.public_dict(),
            "estimated_latency_seconds": tr("6–10 seconds + slower source latency"),
        }

    def request_offset_delta(self, delta_ms: int) -> None:
        self.events.put(("offset", delta_ms))

    def request_remeasure(self) -> None:
        self.message = tr("P0 has no automatic measurement; use the offset buttons")

    def request_resniff(self) -> None:
        self.message = tr("P0 uses direct media URLs; restart the command to change streams")


def parser(argv: list[str] | None = None) -> argparse.ArgumentParser:
    configure_cli_language(argv)
    result = ArgumentParser(
        prog="footboy-p0",
        allow_abbrev=False,
        description=tr("P0: validate two live inputs, offset, and HLS using direct media URLs."),
    )
    result.add_argument("--video-url", required=True, help=tr("Direct match media URL"))
    result.add_argument("--bili-url", required=True, help=tr("Direct commentary media URL"))
    result.add_argument(
        "--offset",
        required=True,
        type=float,
        help=tr("Offset D in seconds; positive values delay Bilibili audio"),
    )
    result.add_argument(
        "--video-header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help=tr("Match request header; repeat for multiple headers"),
    )
    result.add_argument(
        "--video-no-proxy",
        action="store_true",
        help=tr("Bypass proxies for the match media stream"),
    )
    result.add_argument(
        "--bili-header",
        action="append",
        default=[],
        metavar="NAME:VALUE",
        help=tr("Commentary request header; repeat for multiple headers"),
    )
    result.add_argument(
        "--output-dir",
        type=Path,
        default=Path("hls_out"),
        help=tr("HLS output directory (default: hls_out)"),
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
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser(argv).parse_args(argv)
    try:
        _check_binary(args.ffmpeg, minimum_major=6)
        _check_binary(args.ffprobe)
        video = Source(
            args.video_url,
            headers=_headers(args.video_header),
            kind=_kind(args.video_url),
            no_proxy=args.video_no_proxy,
        )
        bili = Source(args.bili_url, headers=_headers(args.bili_header), kind=_kind(args.bili_url))
        ffprobe_source(video, ffprobe=args.ffprobe)
        ffprobe_source(bili, ffprobe=args.ffprobe)
    except Exception as exc:
        print(tr("P0 input validation failed: {0}", exc), file=sys.stderr)
        return 2
    muxer = FfmpegMuxer(args.output_dir, ffmpeg=args.ffmpeg)
    controller = P0Controller(muxer, video, bili, args.offset)
    server = ControlServer(controller, args.output_dir, host=args.host, port=args.port)
    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    for name in ("SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), stop)
    try:
        muxer.start(video, bili, args.offset, fresh=True)
        server.start()
        deadline: float | None = None
        while not stopping.wait(0.2):
            try:
                action, payload = controller.events.get_nowait()
            except queue.Empty:
                action = ""
                payload = None
            if action == "offset":
                if not isinstance(payload, (int, float)) or isinstance(payload, bool):
                    continue
                controller.offset = round(controller.offset + int(payload) / 1000, 3)
                controller.message = tr("Applying the manual offset after a 1.5-second debounce")
                deadline = time.monotonic() + 1.5
            if deadline is not None and time.monotonic() >= deadline:
                muxer.restart(video, bili, controller.offset)
                controller.message = tr("Applied D={0:.3f}s", controller.offset)
                deadline = None
            code = muxer.poll()
            if code is not None:
                print(tr("FFmpeg exited early, code={0}", code), file=sys.stderr)
                return 1
    finally:
        print(tr("Sending q to FFmpeg and flushing the playlist…"))
        muxer.stop()
        server.stop()
    return 0


def _headers(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, content = value.partition(":")
        if not separator or not name.strip() or "\r" in content or "\n" in content:
            raise ValueError(tr("Invalid header: {0!r}", value))
        result[name.strip()] = content.strip()
    return result


def _kind(url: str) -> str:
    path = urlsplit(url).path.lower()
    return "hls" if ".m3u8" in path else "flv" if ".flv" in path else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
