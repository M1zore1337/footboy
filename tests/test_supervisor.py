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
    monkeypatch.setattr("footboy.supervisor.estimate_initial_offset", lambda *a, **k: -4719.0)
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
    assert supervisor.offset == -4719.0
    assert not supervisor.aligned
    assert supervisor.store.offset(supervisor._offset_key) is None


@pytest.mark.parametrize("manual_when", ["resolve", "estimate", "ready", None])
def test_bootstrap_keeps_manual_offset_instead_of_applying_initial_ocr(
    tmp_path, monkeypatch, manual_when
) -> None:
    supervisor = supervisor_at(tmp_path, video_direct=True, bili_direct=True)
    set_phase = supervisor._set_phase

    def probe(source, **kwargs):
        source.has_audio = True
        if manual_when == "resolve" and supervisor.bili is None:
            supervisor.request_offset_delta(500)
        return source

    def estimate(*args, **kwargs):
        if manual_when == "estimate":
            supervisor.request_offset_delta(500)
        return 0.0

    def phase(phase, message):
        set_phase(phase, message)
        if manual_when == "ready" and phase == "RUN":
            supervisor.request_offset_delta(500)

    monkeypatch.setattr("footboy.supervisor.ffprobe_source", probe)
    monkeypatch.setattr("footboy.supervisor.estimate_initial_offset", estimate)
    monkeypatch.setattr("footboy.supervisor.measure_offset", lambda *a, **k: measurement(12))
    monkeypatch.setattr(supervisor.muxer, "start", lambda *a, **k: None)
    monkeypatch.setattr(supervisor, "_set_phase", phase)
    supervisor._bootstrap()
    worker = supervisor._measurement_thread
    if worker is not None:
        worker.join(timeout=2)
        assert not worker.is_alive()
        action, payload = supervisor._events.get(timeout=1)
        assert action == "measurement_done"
        supervisor._finish_measurement(*payload)

    expected = 12 if manual_when is None else 0.5
    assert supervisor.offset == expected
    assert supervisor.store.offset(supervisor._offset_key) == expected
    assert supervisor.confidence["method"] == ("ocr" if manual_when is None else "manual")
    assert supervisor.aligned
    assert supervisor._next_verify - time.monotonic() == pytest.approx(
        supervisor.config.verify_interval, abs=1
    )
    if manual_when == "ready":
        assert supervisor.applied_offset == 0
        assert supervisor._adjust_deadline is not None


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


def test_named_line_routing_and_conflicts(tmp_path):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("https://cdn.example/old.flv", line_text="中文高清")
    supervisor.request_switch_line("高清直播5")
    assert supervisor._events.get_nowait() == ("switch_line", "高清直播5")
    assert supervisor.public_status()["source_switching"]
    with pytest.raises(RuntimeError, match="正在获取"):
        supervisor.request_switch_line("中文高清")
    supervisor.sniffer.running = True
    supervisor.request_switch_line("高清直播⑤")
    assert supervisor.sniffer.public_status()["pending_line"] == "高清直播⑤"
    supervisor.config.video_direct = True
    with pytest.raises(ValueError, match="媒体直链"):
        supervisor.request_switch_line("中文高清")


def test_explicit_line_overrides_saved_preference_and_resniff_waits_for_choice(
    tmp_path, monkeypatch
):
    supervisor = supervisor_at(tmp_path, video_line_text="高清直播5")
    supervisor.store.set_source_probe(supervisor._video_probe_key, {"line_text": "中文高清"})
    calls = []

    def sniff(url, **kwargs):
        calls.append(kwargs)
        return Source("https://cdn.example/new.flv", line_text="高清直播⑤")

    monkeypatch.setattr(supervisor.sniffer, "sniff", sniff)
    supervisor._sniff_video(headless=True)
    assert calls[-1]["preferred_line_text"] == "高清直播5"
    assert supervisor.store.source_probe(supervisor._video_probe_key)["line_text"] == "高清直播⑤"
    supervisor._sniff_video(headless=True, reuse_line=False)
    assert calls[-1]["preferred_line_text"] is None and not calls[-1]["auto_select"]
    supervisor._sniff_video(headless=True, line_text="中文高清", reuse_line=False)
    assert calls[-1]["preferred_line_text"] == "中文高清" and calls[-1]["auto_select"]


@pytest.mark.parametrize("manual_during_switch", [False, True])
def test_switch_keeps_playing_until_ready_and_invalidates_old_ocr(
    tmp_path, monkeypatch, manual_during_switch
):
    supervisor = supervisor_at(tmp_path, initial_offset=1)
    old = Source("https://cdn.example/old.flv", line_text="中文高清")
    new = Source("https://cdn.example/new.flv", line_text="高清直播⑤")
    supervisor.video = old
    supervisor.bili = Source("https://bili.example/live.flv")
    supervisor.muxer.health.running = True
    supervisor._preview_bytes["video"] = b"old-frame"
    entered, release = threading.Event(), threading.Event()
    restarts, measurements = [], []
    monkeypatch.setattr(supervisor, "_resolve_bili", lambda: supervisor.bili)

    def sniff(**kwargs):
        assert kwargs["line_text"] == "高清直播5" and not kwargs["reuse_line"]
        entered.set()
        assert release.wait(3)
        return new

    monkeypatch.setattr(supervisor, "_sniff_video", sniff)
    monkeypatch.setattr(supervisor.muxer, "restart", lambda *args: restarts.append(args))
    monkeypatch.setattr(supervisor, "_start_measurement", measurements.append)
    supervisor._start_refresh(force_sniff=True, line_text="高清直播5")
    assert entered.wait(1)
    assert supervisor.video is old and supervisor.muxer.health.running and not restarts
    supervisor._finish_measurement("initial", 0, 0, measurement(999), None)
    assert supervisor.offset == 1
    if manual_during_switch:
        supervisor.request_offset_delta(500)
    release.set()
    action, payload = supervisor._events.get(timeout=2)
    assert action == "refresh_done"
    supervisor._finish_refresh(*payload)
    expected = 1.5 if manual_during_switch else 1
    assert supervisor.video is new and restarts == [(new, supervisor.bili, expected)]
    assert supervisor.applied_offset == expected and not supervisor._preview_bytes
    assert measurements == ([] if manual_during_switch else ["initial"])
    if manual_during_switch:
        assert supervisor.confidence == {"method": "manual"}


def test_failed_line_switch_keeps_original_source_and_offset(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path, initial_offset=2)
    old = Source("https://cdn.example/old.flv", line_text="中文高清")
    supervisor.video = old
    supervisor.muxer.health.running = True
    monkeypatch.setattr(supervisor.muxer, "restart", lambda *_: pytest.fail("Keep old playback"))
    supervisor._finish_refresh(None, RuntimeError("目标线路暂不可用"), True)
    assert supervisor.video is old and supervisor.offset == 2
    assert supervisor.phase == "RUN" and "保留原线路" in supervisor.message
