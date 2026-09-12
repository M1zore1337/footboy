from __future__ import annotations

import threading
from functools import partial
from itertools import product
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
    TesseractBackend,
    _discover_candidates,
    _read_series,
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


@pytest.mark.parametrize("first_clock", [0, 3714])
def test_discovery_reading_is_reused_without_skipping_a_validation_frame(monkeypatch, first_clock):
    monkeypatch.setattr("footboy.probe.ocr._text_rois", lambda _: [CONFIG.roi])
    monkeypatch.setattr("footboy.probe.ocr._candidate_rois", lambda: [])
    monkeypatch.setattr("footboy.probe.ocr._has_edges", lambda _: True)
    monkeypatch.setattr("footboy.probe.ocr.preprocess", lambda image, *args: image)
    frames = [(float(i), np.full((32, 64, 3), i, dtype=np.uint8)) for i in range(3)]
    calls = []

    def read(image):
        clock = first_clock + int(image[0, 0, 0])
        calls.append(clock)
        return f"{clock // 60:02}:{clock % 60:02}"

    result = probe_clock(frames, SimpleNamespace(read=read))

    assert sorted(calls) == list(range(first_clock, first_clock + 3))
    assert result.k == first_clock
    assert [(sample.pts, sample.clock) for sample in result.samples] == [
        (float(index), first_clock + index) for index in range(3)
    ]


def test_discovery_reaches_later_text_line_before_exhausting_variants(monkeypatch):
    rois = [(index / 20, 0.0, 0.04, 0.2) for index in range(10)]
    monkeypatch.setattr("footboy.probe.ocr._text_rois", lambda _: rois)
    monkeypatch.setattr("footboy.probe.ocr._candidate_rois", lambda: [])
    monkeypatch.setattr("footboy.probe.ocr._has_edges", lambda _: True)
    expected = ProbeConfig(rois[-1], "none", False)
    attempts = []

    def read(frame, config, backend):
        attempts.append(config)
        return 3714 if config == expected else None

    monkeypatch.setattr("footboy.probe.ocr.read_with_config", read)
    result = next(_discover_candidates(np.zeros((100, 400, 3), dtype=np.uint8), object()))

    assert result == expected
    assert len(attempts) <= 30


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


def test_quick_screen_preserves_every_possible_later_run(monkeypatch):
    frames = [(float(i * 2), np.full((2, 2), i, dtype=np.uint8)) for i in range(8)]
    # All 256 occlusion patterns: unknown frames must never be treated as misses.
    for present in product((False, True), repeat=8):
        readings = [500 + i * 2 if found else None for i, found in enumerate(present)]
        monkeypatch.setattr(
            "footboy.probe.ocr.read_with_config",
            lambda frame, *_, readings=readings: readings[int(frame[0, 0])],
        )
        expected = _validate_series(
            [
                ClockSample(pts, clock) if clock is not None else None
                for (pts, _), clock in zip(frames, readings, strict=True)
            ],
            CONFIG,
        )
        actual = _validate_series(_read_series(frames, CONFIG, object()), CONFIG)
        assert actual == expected, present


@pytest.mark.parametrize("count", [3, 4, 7, 8])
def test_empty_saved_region_is_screened_with_few_reads(monkeypatch, count):
    calls = []
    frames = [(float(i), np.zeros((2, 2))) for i in range(count)]
    monkeypatch.setattr("footboy.probe.ocr.read_with_config", lambda *args: calls.append(1))
    samples = _read_series(frames, CONFIG, object())
    assert _validate_series(samples, CONFIG) is None
    assert len(calls) == count // 3


@pytest.mark.parametrize(
    "clocks",
    [
        [500, 498, 496, 494, 492, 490, 488, 486],
        [800, 500, 504, 506, 508, 510, 512, 514],
        [500, 500, 500, 506, 508, 510, 512, 514],
        [500, 502, 504, 900, 508, 510, 512, 514],
    ],
)
def test_quick_screen_preserves_jump_countdown_and_stopped_results(monkeypatch, clocks):
    frames = [(float(i * 2), np.full((2, 2), i)) for i in range(len(clocks))]
    monkeypatch.setattr(
        "footboy.probe.ocr.read_with_config", lambda frame, *_: clocks[int(frame[0, 0])]
    )
    samples = _read_series(frames, CONFIG, object())
    if clocks[:3] == [500] * 3:
        with pytest.raises(StoppedClock):
            _validate_series(samples, CONFIG)
    else:
        expected = _validate_series(
            [ClockSample(i * 2, clock) for i, clock in enumerate(clocks)], CONFIG
        )
        assert _validate_series(samples, CONFIG) == expected


def test_tesseract_receives_remaining_budget_and_late_success_is_rejected(monkeypatch):
    now = [100.0]
    timeouts = []
    monkeypatch.setattr("footboy.probe.ocr.time.monotonic", lambda: now[0])

    def read(image, *, config, timeout):
        timeouts.append(timeout)
        now[0] += timeout + 0.01
        return "61:54"

    backend = object.__new__(TesseractBackend)
    backend._module = SimpleNamespace(image_to_string=read)
    events = []
    with pytest.raises(OcrTimeout):
        probe_clock(probe_frames(), backend, saved=CONFIG, budget=0.25, on_progress=events.append)
    assert timeouts == [pytest.approx(0.25)]
    assert events[-1].state == "timeout"
    assert not any(event.state == "locked" for event in events)


@pytest.mark.parametrize("message", ["Tesseract process timeout", "executable failed"])
def test_tesseract_distinguishes_call_timeout_from_backend_failure(message):
    def read(*args, **kwargs):
        raise RuntimeError(message)

    backend = object.__new__(TesseractBackend)
    backend._module = SimpleNamespace(image_to_string=read)
    with pytest.raises(OcrError) as error:
        backend.read(np.zeros((2, 2), dtype=np.uint8))
    assert isinstance(error.value, OcrTimeout) == ("timeout" in message)


def test_generic_backend_receives_no_tesseract_options_and_cannot_lock_after_deadline(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("footboy.probe.ocr.time.monotonic", lambda: now[0])

    def read(image):
        now[0] += 1
        return "61:54"

    with pytest.raises(OcrTimeout):
        probe_clock(probe_frames(), SimpleNamespace(read=read), saved=CONFIG, budget=0.5)


def test_cancellation_during_backend_read_is_not_reported_as_a_reading():
    cancellation = threading.Event()
    events = []

    def read(image):
        cancellation.set()
        return "61:54"

    with pytest.raises(OcrCancelled):
        probe_clock(
            probe_frames(),
            SimpleNamespace(read=read),
            saved=CONFIG,
            stop_event=cancellation,
            on_progress=events.append,
        )
    assert events[-1].state == "cancelled"
    assert not any(event.state in {"reading", "locked"} for event in events)


@pytest.mark.parametrize("start", range(6))
def test_quick_screen_preserves_a_later_stopped_run(monkeypatch, start):
    frames = [(float(i * 2), np.full((2, 2), i)) for i in range(8)]
    monkeypatch.setattr(
        "footboy.probe.ocr.read_with_config",
        lambda frame, *_: 500 if start <= int(frame[0, 0]) < start + 3 else None,
    )
    with pytest.raises(StoppedClock):
        _validate_series(_read_series(frames, CONFIG, object()), CONFIG)


def test_nearby_search_does_not_mark_unread_candidates_as_tried(monkeypatch):
    rois = [(index / 20, 0.0, 0.04, 0.2) for index in range(10)]
    monkeypatch.setattr("footboy.probe.ocr._text_rois", lambda *args, **kwargs: rois)
    monkeypatch.setattr("footboy.probe.ocr._candidate_rois", lambda: [])
    monkeypatch.setattr("footboy.probe.ocr._has_edges", lambda _: True)
    attempts = []
    monkeypatch.setattr(
        "footboy.probe.ocr.read_with_config", lambda frame, config, _: attempts.append(config)
    )
    tried = set()
    frame = np.zeros((32, 64, 3), dtype=np.uint8)
    assert list(_discover_candidates(frame, object(), near=CONFIG, tried=tried)) == []
    assert len(attempts) == 8 and tried == set(attempts)
    expected = ProbeConfig(rois[6], "none", False)
    monkeypatch.setattr(
        "footboy.probe.ocr.read_with_config",
        lambda frame, config, _: 500 if config == expected else None,
    )
    assert next(_discover_candidates(frame, object(), tried=tried)) == expected


def test_user_cancellation_takes_precedence_over_backend_timeout():
    cancellation = threading.Event()

    def read(image):
        cancellation.set()
        raise OcrTimeout("backend timeout")

    with pytest.raises(OcrCancelled):
        probe_clock(
            probe_frames(), SimpleNamespace(read=read), saved=CONFIG, stop_event=cancellation
        )


def test_explicit_manual_selection_does_not_spend_validation_budget_while_user_chooses(monkeypatch):
    now = [0.0]
    monkeypatch.setattr("footboy.probe.ocr.time.monotonic", lambda: now[0])
    monkeypatch.setattr("footboy.probe.ocr._discover_candidates", lambda *a, **k: [])
    monkeypatch.setattr("footboy.probe.ocr.preprocess", lambda image, *args: image)

    def select(*args):
        now[0] += 60
        return CONFIG

    monkeypatch.setattr("footboy.probe.ocr._manual_config", select)
    frames = [(float(i), np.full((12, 32, 3), i, dtype=np.uint8)) for i in range(3)]
    backend = SimpleNamespace(read=lambda image: f"01:{int(image[0, 0, 0]):02}")
    result = probe_clock(frames, backend, allow_manual=True)
    assert [sample.clock for sample in result.samples] == [60, 61, 62]


def test_source_sampling_failures_publish_independent_ocr_states():
    events = {"video": [], "bili": []}

    def collect(source):
        return []

    with pytest.raises(MeasurementError) as error:
        measure_offset(
            Source("https://video.example/live"),
            Source("https://bili.example/live"),
            frame_collector=collect,
            on_progress=lambda label, event: events[label].append(event),
        )
    assert not error.value.needs_roi
    assert [event.state for event in events["video"]] == ["sampling", "error"]
    assert [event.state for event in events["bili"]] == ["sampling", "error"]
