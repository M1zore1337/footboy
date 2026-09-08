"""Synthetic clock images read by the real Tesseract backend."""

from __future__ import annotations

import importlib.util
import os
import shutil

import cv2
import numpy as np
import pytest

from footboy.probe.ocr import ProbeConfig, StoppedClock, TesseractBackend, flip_frame, probe_clock
from footboy.probe.offset import measure_offset
from footboy.sources.models import Source
from footboy.state import StateStore

TESSERACT = os.environ.get("FOOTBOY_TESSERACT") or shutil.which("tesseract")
pytestmark = pytest.mark.skipif(
    not TESSERACT or importlib.util.find_spec("pytesseract") is None,
    reason="需要 Tesseract 和 pytesseract；可设置 FOOTBOY_TESSERACT 指定可执行文件",
)
ROI = (20 / 640, 16 / 360, 178 / 640, 56 / 360)


def clock_frames(origin: float, clock: int, *, flip="none", stopped=False):
    frames = []
    for index in range(4):
        image = np.full((360, 640, 3), (48, 92, 44), dtype=np.uint8)
        cv2.rectangle(image, (20, 16), (198, 72), (15, 15, 15), -1)
        current = clock if stopped else clock + index * 2
        cv2.putText(
            image,
            f"{current // 60:02}:{current % 60:02}",
            (29, 57),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.15,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        frames.append((origin + index * 2, flip_frame(image, flip)))
    return frames


@pytest.mark.parametrize("flip", ["none", "h", "v", "hv"])
def test_tesseract_recovers_clock_from_each_flip(flip):
    result = probe_clock(
        clock_frames(1000.125, 2700, flip=flip),
        TesseractBackend(TESSERACT),
        saved=ProbeConfig(ROI, flip, False),
        allow_manual=False,
    )
    assert result.k == pytest.approx(1699.875, abs=0.1)
    assert result.residual <= 1.0


def test_tesseract_discovers_clock_without_saved_roi():
    result = probe_clock(
        clock_frames(1000.125, 2700),
        TesseractBackend(TESSERACT),
        allow_manual=False,
    )
    assert result.k == pytest.approx(1699.875, abs=0.1)
    assert len(result.samples) >= 3


def test_tesseract_stopped_clock_is_rejected():
    with pytest.raises(StoppedClock):
        probe_clock(
            clock_frames(1000, 2700, stopped=True),
            TesseractBackend(TESSERACT),
            saved=ProbeConfig(ROI, "none", False),
            allow_manual=False,
        )


@pytest.mark.parametrize(
    ("bili_origin", "bili_clock", "expected"),
    [(1000.5, 2712, 11.625), (1000.5, 2688, -12.375), (9000.5, 2688, -8012.375)],
)
def test_tesseract_alignment_preserves_independent_pts_origins(
    tmp_path,
    bili_origin,
    bili_clock,
    expected,
):
    video, bili = Source("https://video.example/live"), Source("https://bili.example/live")
    store = StateStore(tmp_path / "state.json")
    store.set_source_probe("video", ProbeConfig(ROI, "none", False).to_dict())
    store.set_source_probe("bili", ProbeConfig(ROI, "h", False).to_dict())
    frames = {
        video.url: clock_frames(1000.125, 2700),
        bili.url: clock_frames(bili_origin, bili_clock, flip="h"),
    }
    result = measure_offset(
        video,
        bili,
        state=store,
        video_key="video",
        bili_key="bili",
        frame_collector=lambda source: frames[source.url],
        ocr_backend="tesseract",
        tesseract_command=TESSERACT,
        allow_manual=False,
    )
    assert result.offset == pytest.approx(expected, abs=1.0)
    assert result.confidence.video_samples >= 3
    assert result.confidence.bili_samples >= 3
