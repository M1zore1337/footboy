from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import numpy as np

from footboy.sources.models import Source
from footboy.state import StateStore

from .frames import collect_keyframes
from .ocr import (
    ClockProbeResult,
    ManualSelectionRequired,
    OcrCancelled,
    OcrError,
    OcrProgress,
    OcrTimeout,
    ProbeConfig,
    StoppedClock,
    make_backend,
    probe_clock,
)


class MeasurementError(OcrError):
    def __init__(self, message: str, needs_roi: list[str]) -> None:
        super().__init__(message)
        self.needs_roi = needs_roi


@dataclass(frozen=True, slots=True)
class OffsetConfidence:
    video_samples: int
    bili_samples: int
    video_residual: float
    bili_residual: float
    stopped: bool = False

    def public_dict(self) -> dict[str, float | int | bool]:
        return {
            "video_samples": self.video_samples,
            "bili_samples": self.bili_samples,
            "video_residual": round(self.video_residual, 3),
            "bili_residual": round(self.bili_residual, 3),
            "stopped": self.stopped,
        }


@dataclass(frozen=True, slots=True)
class OffsetMeasurement:
    offset: float
    confidence: OffsetConfidence
    video: ClockProbeResult
    bili: ClockProbeResult


def measure_offset(
    video: Source,
    bili: Source,
    *,
    state: StateStore | None = None,
    video_key: str | None = None,
    bili_key: str | None = None,
    ocr_backend: str = "auto",
    tesseract_command: str | None = None,
    allow_manual: bool = False,
    frame_collector: Callable[[Source], list] | None = None,
    on_preview: Callable[[str, np.ndarray], None] | None = None,
    on_progress: Callable[[str, OcrProgress], None] | None = None,
    stop_event: threading.Event | None = None,
    persist: bool = True,
) -> OffsetMeasurement:
    """Measure D = K_B - K_V using each stream's original PTS reference."""

    def run(label: str, source: Source, key: str | None) -> ClockProbeResult:
        if on_progress is not None:
            on_progress(label, OcrProgress("sampling", phase="sampling"))
        try:
            frames = (
                frame_collector(source)
                if frame_collector is not None
                else collect_keyframes(source, stop_event=stop_event)
            )
            if stop_event is not None and stop_event.is_set():
                raise OcrCancelled("测量已取消")
            if frames and on_preview is not None:
                on_preview(label, frames[-1][1])
            if len(frames) < 3:
                raise OcrError("至少需要 3 个关键帧才能锁定比赛时钟")
            saved = None
            stored = state.source_probe(key) if state is not None and key else None
            if stored:
                try:
                    saved = ProbeConfig.from_dict(stored)
                except (KeyError, TypeError, ValueError):
                    saved = None
            backend = make_backend(ocr_backend, tesseract_command=tesseract_command)
        except Exception as exc:
            if on_progress is not None:
                status = (
                    "cancelled"
                    if isinstance(exc, OcrCancelled)
                    else "timeout"
                    if isinstance(exc, OcrTimeout)
                    else "stopped"
                    if isinstance(exc, StoppedClock)
                    else "error"
                )
                on_progress(label, OcrProgress(status, phase="sampling", reason=str(exc)))
            raise
        result = probe_clock(
            frames,
            backend,
            saved=saved,
            allow_manual=allow_manual,
            stop_event=stop_event,
            on_progress=(lambda event: on_progress(label, event)) if on_progress else None,
        )
        return result

    # Each worker owns its decoder and OCR backend. No GUI or terminal prompt
    # is needed in web mode, so sampling and recognition can run independently.
    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="frame-probe") as pool:
        futures = {
            "video": pool.submit(run, "video", video, video_key),
            "bili": pool.submit(run, "bili", bili, bili_key),
        }
        results = {}
        errors = {}
        for label, future in futures.items():
            try:
                results[label] = future.result()
            except Exception as exc:
                errors[label] = exc
    if errors:
        labels = {"video": "比赛画面", "bili": "B站直播间"}
        message = "；".join(f"{labels[key]}：{value}" for key, value in errors.items())
        if any(isinstance(exc, OcrCancelled) for exc in errors.values()):
            raise OcrCancelled(message)
        if all(isinstance(exc, StoppedClock) for exc in errors.values()):
            raise StoppedClock(message)
        raise MeasurementError(
            message,
            [key for key, exc in errors.items() if isinstance(exc, ManualSelectionRequired)],
        )
    video_result, bili_result = results["video"], results["bili"]
    if abs(video_result.representative_clock - bili_result.representative_clock) > 300:
        raise ValueError("两路同轮比赛时钟相差超过 300 秒，拒绝应用偏移")
    if persist and state is not None and not (stop_event is not None and stop_event.is_set()):
        for key, result in ((video_key, video_result), (bili_key, bili_result)):
            if key:
                saved = state.source_probe(key) or {}
                state.set_source_probe(key, {**saved, **result.config.to_dict()})
    confidence = OffsetConfidence(
        video_samples=len(video_result.samples),
        bili_samples=len(bili_result.samples),
        video_residual=video_result.residual,
        bili_residual=bili_result.residual,
    )
    return OffsetMeasurement(
        offset=bili_result.k - video_result.k,
        confidence=confidence,
        video=video_result,
        bili=bili_result,
    )
