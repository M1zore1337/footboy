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
        self.message = "P0 手工混流运行中"
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
            "estimated_latency_seconds": "6-10 + 较慢源自身延迟",
        }

    def request_offset_delta(self, delta_ms: int) -> None:
        self.events.put(("offset", delta_ms))

    def request_remeasure(self) -> None:
        self.message = "P0 不含自动测量；请使用偏移按钮"

    def request_resniff(self) -> None:
        self.message = "P0 使用手抄直链；请重启命令更换线路"


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="P0：用两条手抄直链验证双直播输入、偏移和 HLS。")
    result.add_argument("--video-url", required=True)
    result.add_argument("--bili-url", required=True)
    result.add_argument("--offset", required=True, type=float, help="D 秒；正值推后 B 站音频")
    result.add_argument("--video-header", action="append", default=[], metavar="NAME:VALUE")
    result.add_argument("--video-no-proxy", action="store_true", help="比赛媒体流直连，不使用代理")
    result.add_argument("--bili-header", action="append", default=[], metavar="NAME:VALUE")
    result.add_argument("--output-dir", type=Path, default=Path("hls_out"))
    result.add_argument("--host", default="0.0.0.0")
    result.add_argument("--port", type=int, default=8080)
    result.add_argument("--ffmpeg", default="ffmpeg")
    result.add_argument("--ffprobe", default="ffprobe")
    return result


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
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
        print(f"P0 输入验收失败: {exc}", file=sys.stderr)
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
                controller.message = "等待 1.5 秒去抖后应用手动偏移"
                deadline = time.monotonic() + 1.5
            if deadline is not None and time.monotonic() >= deadline:
                muxer.restart(video, bili, controller.offset)
                controller.message = f"已应用 D={controller.offset:.3f}s"
                deadline = None
            code = muxer.poll()
            if code is not None:
                print(f"ffmpeg 提前退出，code={code}", file=sys.stderr)
                return 1
    finally:
        print("正在向 ffmpeg 写入 q 并刷新播放列表……")
        muxer.stop()
        server.stop()
    return 0


def _headers(values: list[str]) -> dict[str, str]:
    result = {}
    for value in values:
        name, separator, content = value.partition(":")
        if not separator or not name.strip() or "\r" in content or "\n" in content:
            raise ValueError(f"无效请求头: {value!r}")
        result[name.strip()] = content.strip()
    return result


def _kind(url: str) -> str:
    path = urlsplit(url).path.lower()
    return "hls" if ".m3u8" in path else "flv" if ".flv" in path else "unknown"


if __name__ == "__main__":
    raise SystemExit(main())
