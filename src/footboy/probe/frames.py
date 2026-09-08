from __future__ import annotations

import threading
import time
from collections.abc import Iterator

import numpy as np

from footboy.sources.models import Source


class FrameProbeError(RuntimeError):
    pass


def keyframes(
    source: Source,
    *,
    duration: float = 15.0,
    min_frames: int = 4,
    max_frames: int = 8,
    stop_event: threading.Event | None = None,
) -> Iterator[tuple[float, np.ndarray]]:
    """Yield decoded keyframes with their unmodified source PTS in seconds."""
    if stop_event is not None and stop_event.is_set():
        raise FrameProbeError("测量已取消")
    try:
        import av
    except ImportError as exc:
        raise FrameProbeError("未安装 PyAV") from exc

    started = time.monotonic()
    yielded = 0
    last_pts: float | None = None
    sample_interval = duration / max(1, max_frames - 1)
    try:
        container = av.open(source.url, options=source.pyav_options(), timeout=(8.0, 5.0))
    except Exception as exc:
        raise FrameProbeError(f"无法打开 {source.domain} 视频流: {exc}") from exc
    try:
        if not container.streams.video:
            raise FrameProbeError(f"{source.domain} 视频流不含画面")
        stream = container.streams.video[0]
        stream.codec_context.skip_frame = "NONKEY"
        for frame in container.decode(stream):
            if stop_event is not None and stop_event.is_set():
                raise FrameProbeError("测量已取消")
            elapsed = time.monotonic() - started
            if elapsed >= duration:
                break
            time_base = frame.time_base or stream.time_base
            if frame.pts is None or time_base is None:
                continue
            pts_seconds = float(frame.pts * time_base)
            if last_pts is not None and pts_seconds < last_pts:
                raise FrameProbeError("探针采样期间源 PTS 回退，拒绝使用不同时间轴")
            if last_pts is not None and pts_seconds - last_pts < sample_interval:
                continue
            yield pts_seconds, frame.to_ndarray(format="bgr24")
            yielded += 1
            last_pts = pts_seconds
            if yielded >= max_frames:
                return
    except FrameProbeError:
        raise
    except Exception as exc:
        raise FrameProbeError(f"读取 {source.domain} 关键帧失败: {exc}") from exc
    finally:
        container.close()


def collect_keyframes(
    source: Source, *, duration: float = 15.0, stop_event: threading.Event | None = None
) -> list[tuple[float, np.ndarray]]:
    frames = list(keyframes(source, duration=duration, stop_event=stop_event))
    if len(frames) < 3:
        raise FrameProbeError(f"{source.domain} 在探针窗口内只有 {len(frames)} 个可用关键帧")
    return frames
