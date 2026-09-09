from __future__ import annotations

import signal
import threading
import time
from pathlib import Path

import pytest

from footboy.probe.ocr import ClockProbeResult, ClockSample, ProbeConfig, StoppedClock
from footboy.probe.offset import OffsetConfidence, OffsetMeasurement
from footboy.sources.models import Source
from footboy.supervisor import Supervisor, SupervisorConfig


def supervisor_at(tmp_path: Path, **kwargs) -> Supervisor:
    return Supervisor(
        SupervisorConfig(
            video_page_url="https://video.example/match",
            bili_room_url="https://live.bilibili.com/0",
            output_dir=tmp_path / "hls",
            state_file=tmp_path / "state.json",
            **kwargs,
        )
    )


def measurement(offset: float) -> OffsetMeasurement:
    config = ProbeConfig((0, 0, 0.3, 0.2), "none", False)
    video = ClockProbeResult(100, (ClockSample(10, 110),) * 4, 0.2, config)
    bili = ClockProbeResult(100 + offset, (ClockSample(10, 110),) * 4, 0.1, config)
    return OffsetMeasurement(offset, OffsetConfidence(4, 4, 0.2, 0.1), video, bili)


def finish(supervisor: Supervisor, value: float, mode="verify", error=None) -> None:
    supervisor._finish_measurement(
        mode,
        supervisor._revision,
        supervisor._source_generation,
        measurement(value) if error is None else None,
        error,
    )


def test_manual_offset_is_relative_and_debounced(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.offset = -2.0
    supervisor.request_offset_delta(500)
    first_deadline = supervisor._adjust_deadline
    supervisor.request_offset_delta(-100)
    assert supervisor.offset == -1.6
    assert supervisor.confidence == {"method": "manual"}
    assert supervisor._adjust_deadline >= first_deadline
    assert supervisor.store.offset("0@video.example") == -1.6


def test_manual_adjustment_wins_over_in_flight_measurement(tmp_path, monkeypatch) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("https://v.example/live.flv")
    supervisor.bili = Source("https://b.example/live.flv")
    entered, release = threading.Event(), threading.Event()

    def slow_measure(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return measurement(15)

    monkeypatch.setattr("footboy.supervisor.measure_offset", slow_measure)
    supervisor._start_measurement("initial")
    assert entered.wait(1)
    supervisor.request_offset_delta(-500)
    assert supervisor.public_status()["offset_seconds"] == -0.5
    assert supervisor.public_status()["measurement"]["running"]
    release.set()
    action, payload = supervisor._events.get(timeout=2)
    assert action == "measurement_done"
    supervisor._finish_measurement(*payload)
    assert supervisor.offset == -0.5
    assert supervisor.store.offset("0@video.example") == -0.5
    assert supervisor.confidence == {"method": "manual"}


def test_drift_requires_two_consistent_successful_verifications(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.applied_offset = 0
    finish(supervisor, 3)
    assert supervisor.offset == 0
    assert supervisor._adjust_deadline is None
    finish(supervisor, -3)  # Opposite direction is not a confirmation.
    assert supervisor.offset == 0
    finish(supervisor, -3.2)
    assert supervisor.offset == -3.2
    assert supervisor._adjust_deadline is not None


def test_failed_verification_breaks_consecutive_drift_and_marks_unaligned(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    finish(supervisor, 4)
    finish(supervisor, 0, error=StoppedClock("中场停表"))
    assert not supervisor.aligned
    assert supervisor._next_verify - time.monotonic() == pytest.approx(60, abs=1)
    finish(supervisor, 4)
    assert supervisor.offset == 0
    assert supervisor._adjust_deadline is None


def test_small_drift_keeps_the_applied_offset(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.offset = supervisor.applied_offset = 2.0
    finish(supervisor, 2.5)
    assert supervisor.offset == 2.0
    assert supervisor.aligned
    assert supervisor._adjust_deadline is None


def test_remeasure_applies_one_good_result_and_persists_roi(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    finish(supervisor, -17.5, mode="manual")
    assert supervisor.offset == -17.5
    assert supervisor._adjust_deadline is not None
    assert supervisor.store.source_probe("video:video.example")["roi"] == (0, 0, 0.3, 0.2)


def test_old_source_measurement_cannot_change_new_source(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor._source_generation = 2
    supervisor._finish_measurement("initial", 0, 1, measurement(80), None)
    assert supervisor.offset == 0
    assert supervisor._adjust_deadline is None


def test_manual_mode_never_schedules_periodic_ocr(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path, auto_measure=False)
    finish(supervisor, 1, mode="manual")
    assert supervisor._next_verify == float("inf")
    finish(supervisor, 0, error=StoppedClock("暂停"))
    assert supervisor._next_verify == float("inf")


def test_manual_roi_invalidates_old_measurement(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.request_roi("video", {"roi": [0.1, 0.1, 0.2, 0.2], "flip": "h", "inverted": True})
    supervisor._finish_measurement("initial", 0, 0, measurement(50), None)
    assert supervisor.offset == 0
    assert supervisor.store.source_probe("video:video.example")["flip"] == "h"


def test_large_signed_pts_offset_does_not_trigger_30_second_startup_recovery(
    tmp_path, monkeypatch
) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.offset = 100000  # This number is not the amount of wall-clock buffering.
    supervisor.muxer.health.running = True
    supervisor._last_segment_change = time.monotonic() - 40
    recovered = []
    monkeypatch.setattr(supervisor, "_recover", recovered.append)
    supervisor._check_health(time.monotonic())
    assert recovered == []
    supervisor._last_segment_signature = ("seg_1.ts", 1)
    supervisor._check_health(time.monotonic())
    assert recovered == ["30 秒没有新 HLS 分片"]


def test_direct_mode_only_disables_proxy_for_the_video_source(tmp_path, monkeypatch) -> None:
    supervisor = supervisor_at(
        tmp_path, video_direct=True, bili_direct=True, video_no_proxy=True, auto_measure=False
    )
    probed, started = [], []

    def probe(source, **kwargs):
        source.has_audio = True
        probed.append(source)
        return source

    monkeypatch.setattr("footboy.supervisor.ffprobe_source", probe)
    monkeypatch.setattr(
        supervisor.muxer,
        "start",
        lambda video, bili, *_args, **_kwargs: started.extend((video, bili)),
    )
    supervisor._bootstrap()
    assert supervisor.sniffer.no_proxy
    assert started == [supervisor.video, supervisor.bili]
    assert supervisor.video.no_proxy and not supervisor.bili.no_proxy
    assert [source.no_proxy for source in probed] == [False, True]


def test_native_mux_crash_stops_recovery_and_cancels_stale_measurements(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path, initial_offset=3.5, auto_measure=False)
    monkeypatch.setattr(supervisor, "_bootstrap", lambda: None)
    monkeypatch.setattr(supervisor.muxer, "poll", lambda: -signal.SIGSEGV)
    monkeypatch.setattr(supervisor, "_recover", lambda *_: pytest.fail("Must not retry a crash"))
    supervisor.run(serve=False)
    assert supervisor.phase == "ERROR"
    assert "SIGSEGV" in supervisor.message and "更换 FFmpeg" in supervisor.message
    assert supervisor._stop.is_set() and supervisor._measurement_cancel.is_set()
    assert not supervisor.aligned
    finish(supervisor, 80, mode="initial")
    assert supervisor.offset == 3.5


def test_ordinary_mux_exit_still_uses_source_recovery(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path)
    monkeypatch.setattr(supervisor.muxer, "poll", lambda: 1)
    recovered = []
    monkeypatch.setattr(supervisor, "_recover", recovered.append)
    supervisor._check_health(time.monotonic())
    assert recovered == ["ffmpeg 已退出，code=1"]
