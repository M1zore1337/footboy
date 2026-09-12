from __future__ import annotations

import math
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from footboy.sources.models import Source


class TimelineProbeError(RuntimeError):
    pass


def _sample_packet(source: Source, kind: str, stop_event: threading.Event) -> tuple[float, float]:
    import av

    started = time.monotonic()
    if stop_event.is_set():
        raise TimelineProbeError("时间轴采样已取消")
    try:
        with av.open(source.url, options=source.pyav_options(), timeout=(8.0, 5.0)) as container:
            streams = container.streams.video if kind == "video" else container.streams.audio
            if not streams:
                raise TimelineProbeError("时间轴采样缺少所需媒体轨道")
            for packet in container.demux(streams[0]):
                if stop_event.is_set() or time.monotonic() - started > 15:
                    raise TimelineProbeError("时间轴采样已取消或超时")
                if packet.size and packet.pts is not None and packet.time_base is not None:
                    pts = float(packet.pts * packet.time_base)
                    if math.isfinite(pts):
                        # Transport and source latency remain unknown; matching
                        # the live edges is only an initial timeline estimate.
                        return pts, started
    except (OSError, ValueError) as exc:
        raise TimelineProbeError(f"无法读取源时间轴：{exc}") from exc
    raise TimelineProbeError("时间轴采样没有有效 PTS")


def sample_audio_start(source: Source, *, stop_event: threading.Event | None = None) -> float:
    return _sample_packet(source, "audio", stop_event or threading.Event())[0]


def _sample_origin(source: Source, kind: str, stop_event: threading.Event) -> float:
    pts, started = _sample_packet(source, kind, stop_event)
    return pts - started


def estimate_initial_offset(video: Source, bili: Source, *, stop_event: threading.Event) -> float:
    """Place live edges near each other without changing either source PTS."""
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="timeline-probe") as pool:
        video_sample = pool.submit(_sample_origin, video, "video", stop_event)
        audio_sample = pool.submit(_sample_origin, bili, "audio", stop_event)
        return round(video_sample.result() - audio_sample.result(), 3)
