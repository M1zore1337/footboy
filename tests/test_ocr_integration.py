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
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not TESSERACT or importlib.util.find_spec("pytesseract") is None,
        reason="需要 Tesseract 和 pytesseract；可设置 FOOTBOY_TESSERACT 指定可执行文件",
    ),
]
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


def test_tesseract_discovers_large_dark_clock_inside_bright_scoreboard():
    frames = []
    for index in range(4):
        image = np.full((720, 1280, 3), 20, dtype=np.uint8)
        cv2.rectangle(image, (83, 62), (345, 145), (245, 245, 245), -1)
        cv2.putText(
            image,
            f"47:{8 + index * 2:02}",
            (115, 129),
            cv2.FONT_HERSHEY_SIMPLEX,
            1.6,
            (15, 15, 15),
            3,
            cv2.LINE_AA,
        )
        frames.append((8000 + index * 2, image))
    result = probe_clock(frames, TesseractBackend(TESSERACT), allow_manual=False)
    assert result.k == pytest.approx(2828 - 8000)
    assert len(result.samples) >= 3


def compact_clock_frames(*, flip="none"):
    """Synthetic condensed digits with scoreboard clutter and a decoy countdown."""
    frames = []
    for index in range(7):
        image = np.full((720, 1280, 3), (48, 92, 44), dtype=np.uint8)
        cv2.rectangle(image, (330, 0), (950, 83), (15, 15, 15), -1)
        cv2.putText(
            image,
            "TEAM A   11.2K   2 - 1   10.8K   TEAM B",
            (355, 32),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (240, 240, 240),
            1,
            cv2.LINE_AA,
        )
        cv2.rectangle(image, (0, 0), (150, 35), (15, 15, 15), -1)
        cv2.putText(
            image,
            f"03:{30 - index * 3:02}",
            (14, 27),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
        clock = 281 + index * 3
        glyph = np.zeros((50, 180, 3), dtype=np.uint8)
        cv2.putText(
            glyph,
            f"{clock // 60:02}:{clock % 60:02}",
            (3, 37),
            cv2.FONT_HERSHEY_DUPLEX,
            1.2,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        x, y, w, h = cv2.boundingRect(cv2.findNonZero(cv2.cvtColor(glyph, cv2.COLOR_BGR2GRAY)))
        image[53:69, 628:652] = cv2.resize(
            glyph[y : y + h, x : x + w], (24, 16), interpolation=cv2.INTER_AREA
        )
        frames.append((1000.125 + index * 3, flip_frame(image, flip)))
    return frames


def test_tesseract_locates_small_top_center_clock_and_rejects_countdown():
    result = probe_clock(compact_clock_frames(), TesseractBackend(TESSERACT), allow_manual=False)
    assert result.k == pytest.approx(-719.125, abs=0.1)
    assert len(result.samples) >= 3
    x, y, width, height = result.config.roi
    assert x < 0.5 < x + width and y < 0.08 < y + height
    assert width < 0.1 and height < 0.1


def test_tesseract_discovers_spaced_football_clock_below_scoreboard():
    frames = []
    for index in range(4):
        image = np.full((1080, 1920, 3), (48, 92, 44), dtype=np.uint8)
        cv2.rectangle(image, (45, 40), (520, 145), (40, 15, 10), -1)
        cv2.putText(
            image,
            "TEAM A  3 - 1  TEAM B",
            (60, 80),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.9,
            (245, 245, 245),
            2,
            cv2.LINE_AA,
        )
        clock = 4916 + index * 4
        x = 245
        for text in (str(clock // 60), ":", f"{clock % 60:02}"):
            glyph = np.zeros((45, 80, 3), dtype=np.uint8)
            cv2.putText(
                glyph,
                text,
                (3, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.85,
                (245, 245, 245),
                2,
                cv2.LINE_AA,
            )
            left, _, width, _ = cv2.boundingRect(
                cv2.findNonZero(cv2.cvtColor(glyph, cv2.COLOR_BGR2GRAY))
            )
            crop = glyph[:, left : left + width]
            region = image[95:140, x : x + width]
            region[crop.any(axis=2)] = crop[crop.any(axis=2)]
            x += width + 8
        frames.append((6373.76 + index * 4, image))
    result = probe_clock(frames, TesseractBackend(TESSERACT), allow_manual=False)
    assert [sample.clock for sample in result.samples] == [4916, 4920, 4924, 4928]
    assert result.k == pytest.approx(-1457.76, abs=0.001)
    assert result.residual == 0


@pytest.mark.parametrize("flip", ["none", "h", "v", "hv"])
def test_tesseract_retries_condensed_style_inside_saved_roi(flip):
    saved = ProbeConfig((624 / 1280, 49 / 720, 32 / 1280, 24 / 720), flip, True)
    result = probe_clock(
        compact_clock_frames(flip=flip),
        TesseractBackend(TESSERACT),
        saved=saved,
        allow_manual=False,
    )
    assert result.k == pytest.approx(-719.125, abs=0.1)
    assert result.config.roi == saved.roi
    assert result.config.flip == flip
    assert result.config.inverted == saved.inverted
    assert result.config.style == "condensed"
    assert len(result.samples) == 7
    assert ProbeConfig.from_dict(result.config.to_dict()) == result.config


class CountingTesseractBackend(TesseractBackend):
    def __init__(self):
        super().__init__(TESSERACT)
        self.calls = 0

    def read(self, image, *, raw_line=False):
        self.calls += 1
        return super().read(image, raw_line=raw_line)


def textured_clock_frames(*, flip="none"):
    """Moving texture produces many plausible text boxes before the small clock."""
    random = np.random.default_rng(19)
    frames = []
    for index in range(4):
        image = cv2.resize(
            random.integers(0, 256, (180, 320, 3), dtype=np.uint8),
            (1280, 720),
            interpolation=cv2.INTER_LINEAR,
        )
        cv2.rectangle(image, (60, 40), (310, 74), (30, 8, 20), -1)
        cv2.putText(
            image,
            "HOME  2 - 2  AWAY",
            (66, 64),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.65,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
        cv2.rectangle(image, (145, 76), (235, 99), (30, 8, 20), -1)
        clock = 3714 + index * 2
        cv2.putText(
            image,
            f"{clock // 60:02}:{clock % 60:02}",
            (163, 93),
            cv2.FONT_HERSHEY_DUPLEX,
            0.5,
            (245, 245, 245),
            1,
            cv2.LINE_AA,
        )
        frames.append((1000.125 + index * 2, flip_frame(image, flip)))
    return frames


@pytest.mark.parametrize("flip", ["none", "h", "v", "hv"])
def test_tesseract_finds_small_clock_among_textured_candidates(flip):
    backend = CountingTesseractBackend()
    result = probe_clock(textured_clock_frames(flip=flip), backend)

    assert [sample.clock for sample in result.samples] == [3714, 3716, 3718, 3720]
    assert result.k == pytest.approx(2713.875)
    assert result.residual == 0
    assert result.config.flip == flip
    assert backend.calls <= (30 if flip == "none" else 80)


def test_tesseract_recovers_from_saved_team_name_region():
    saved = ProbeConfig((62 / 1280, 43 / 720, 69 / 1280, 24 / 720), "none", False)
    backend = CountingTesseractBackend()
    result = probe_clock(textured_clock_frames(), backend, saved=saved)

    assert [sample.clock for sample in result.samples] == [3714, 3716, 3718, 3720]
    assert result.config.roi != saved.roi
    assert backend.calls <= 40


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
