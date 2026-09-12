from __future__ import annotations

import threading
from functools import partial
from types import SimpleNamespace

import numpy as np
import pytest

from footboy.probe.ocr import (
    ClockProbeResult,
    ClockSample,
    ManualSelectionRequired,
    OcrCancelled,
    OcrError,
    OcrTimeout,
    ProbeConfig,
    StoppedClock,
    _validate_series,
    parse_clock,
    probe_clock,
)
from footboy.probe.offset import MeasurementError, measure_offset
from footboy.sources.models import Source

CONFIG = ProbeConfig((0.0, 0.0, 1.0, 1.0), "none", False)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("12:34", 754), (" 90：01 ", 5401), ("130.59", 7859), ("131:00", None), ("12:60", None)],
)
def test_parse_clock(text: str, seconds: int | None) -> None:
    assert parse_clock(text) == seconds


def test_series_estimates_median_k_in_source_pts_reference() -> None:
    samples = [
        ClockSample(pts=100.2, clock=500),
        ClockSample(pts=102.1, clock=502),
        ClockSample(pts=104.0, clock=504),
        ClockSample(pts=106.2, clock=506),
    ]
    result = _validate_series(samples, CONFIG)
    assert result is not None
    assert result.k == pytest.approx(399.9, abs=0.11)
    assert result.residual <= 0.3


def test_three_static_readings_are_rejected_as_stopped_clock() -> None:
    with pytest.raises(StoppedClock):
        _validate_series(
            [ClockSample(10, 50), ClockSample(12, 50), ClockSample(14, 50)],
            CONFIG,
        )


def test_measurement_uses_k_b_minus_k_v(monkeypatch) -> None:
    video = Source("https://video.example/live", kind="hls")
    bili = Source("https://bili.example/live", kind="flv")
    frames = [(0.0, np.zeros((1, 1, 3), dtype=np.uint8))] * 3
    results = iter(
        [
            ClockProbeResult(100.0, (ClockSample(10, 110),) * 3, 0.2, CONFIG),
            ClockProbeResult(107.5, (ClockSample(10, 110),) * 3, 0.3, CONFIG),
        ]
    )
    monkeypatch.setattr("footboy.probe.offset.make_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr("footboy.probe.offset.probe_clock", lambda *args, **kwargs: next(results))
    measured = measure_offset(video, bili, frame_collector=lambda source: frames)
    assert measured.offset == 7.5


@pytest.mark.parametrize("text", ["1234:56", "99:600", "01:23:45", "131:01"])
def test_invalid_or_hour_timestamps_do_not_match_substrings(text) -> None:
    assert parse_clock(text) is None


def test_missing_readings_break_consecutive_lock() -> None:
    result = _validate_series(
        [
            ClockSample(10, 110),
            ClockSample(12, 112),
            None,
            ClockSample(16, 116),
            ClockSample(18, 118),
        ],
        CONFIG,
    )
    assert result is None


def test_non_increasing_pts_cannot_lock_clock() -> None:
    assert (
        _validate_series([ClockSample(10, 110), ClockSample(10, 111), ClockSample(10, 112)], CONFIG)
        is None
    )


@pytest.mark.parametrize("roi", [(0, 0, 0, 1), (0.5, 0, 0.6, 1), (float("nan"), 0, 1, 1)])
def test_invalid_roi_cannot_be_saved(roi) -> None:
    with pytest.raises(ValueError):
        ProbeConfig(roi, "none", False)


def test_legacy_probe_config_keeps_standard_preprocessing() -> None:
    config = ProbeConfig.from_dict({"roi": [0, 0, 0.3, 0.2], "flip": "h", "inverted": False})
    assert config.style == "standard"


@pytest.mark.parametrize("style", ["unknown", None, True, []])
def test_invalid_ocr_style_cannot_be_saved(style) -> None:
    with pytest.raises(ValueError, match="预处理样式"):
        ProbeConfig((0, 0, 0.3, 0.2), "none", False, style)


def probe_frames():
    return [(float(index), np.zeros((32, 64, 3), dtype=np.uint8)) for index in range(3)]


@pytest.mark.parametrize("saved", [None, CONFIG])
def test_ocr_timeout_and_cancellation_are_not_roi_errors(saved):
    backend = SimpleNamespace(read=lambda _: pytest.fail("No OCR should run after its deadline"))
    with pytest.raises(OcrTimeout):
        probe_clock(probe_frames(), backend, saved=saved, budget=0)
    cancellation = threading.Event()
    cancellation.set()
    with pytest.raises(OcrCancelled):
        probe_clock(probe_frames(), backend, saved=saved, stop_event=cancellation)


@pytest.mark.parametrize("saved", [None, CONFIG])
def test_ocr_backend_failure_is_not_replaced_by_roi_prompt(monkeypatch, saved):
    backend_error = OcrError("Tesseract 识别超时或失败")

    def read(image):
        raise backend_error

    monkeypatch.setattr("footboy.probe.ocr._discover_candidates", lambda *args, **kwargs: [CONFIG])
    with pytest.raises(OcrError) as error:
        probe_clock(probe_frames(), SimpleNamespace(read=read), saved=saved)
    assert error.value is backend_error


def test_measurement_budget_expiry_does_not_request_roi(monkeypatch):
    monkeypatch.setattr("footboy.probe.offset.make_backend", lambda *args, **kwargs: object())
    monkeypatch.setattr("footboy.probe.offset.probe_clock", partial(probe_clock, budget=0))
    with pytest.raises(MeasurementError) as error:
        measure_offset(
            Source("https://video.example/live"),
            Source("https://bili.example/live"),
            frame_collector=lambda _: probe_frames(),
        )
    assert not error.value.needs_roi
    assert "超时" in str(error.value)


def test_missing_clock_requests_roi_without_opening_a_manual_prompt(monkeypatch):
    monkeypatch.setattr("footboy.probe.ocr._discover_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(
        "footboy.probe.ocr._manual_config",
        lambda *args: pytest.fail("Background OCR must not prompt"),
    )
    with pytest.raises(ManualSelectionRequired):
        probe_clock(probe_frames(), object())


def test_stopped_clock_does_not_hide_another_sources_backend_error(monkeypatch):
    video = Source("https://video.example/live")
    bili = Source("https://bili.example/live")

    def collect(source):
        if source is video:
            raise StoppedClock("比赛画面停表")
        raise OcrError("音源识别后端失败")

    with pytest.raises(MeasurementError) as error:
        measure_offset(video, bili, frame_collector=collect)
    assert "后端失败" in str(error.value)
    assert not error.value.needs_roi
