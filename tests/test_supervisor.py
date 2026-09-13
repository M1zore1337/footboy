from __future__ import annotations

import os
import signal
import threading
import time
from pathlib import Path

import cv2
import numpy as np
import pytest

from footboy.probe.ocr import ClockProbeResult, ClockSample, OcrProgress, ProbeConfig, StoppedClock
from footboy.probe.offset import MeasurementError, OffsetConfidence, OffsetMeasurement
from footboy.probe.timeline import TimelineProbeError
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


def test_periodic_ocr_does_not_undo_five_second_manual_correction(tmp_path, monkeypatch) -> None:
    supervisor = supervisor_at(tmp_path, initial_offset=-177.321)
    supervisor.video = Source("video.flv")
    supervisor.bili = Source("bili.flv")
    applied = []
    monkeypatch.setattr(supervisor.muxer, "restart", lambda _v, _b, value: applied.append(value))

    supervisor.request_offset_delta(5000)
    supervisor._apply_adjustment()
    finish(supervisor, -177.321)
    finish(supervisor, -177.4)

    status = supervisor.public_status()
    assert applied == [-172.321]
    assert status["offset_seconds"] == status["applied_offset_seconds"] == -172.321
    assert status["confidence"] == {"method": "manual"}
    assert status["aligned"] and not status["adjust_pending"]
    assert "保留手动偏移" in status["message"]
    assert supervisor.store.offset("0@video.example") == -172.321


@pytest.mark.parametrize("error", [StoppedClock("中场停表"), MeasurementError("无法识别", [])])
def test_failed_periodic_ocr_preserves_manual_alignment(tmp_path, error) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.request_offset_delta(5000)
    finish(supervisor, 0, error=error)
    finish(supervisor, 0)
    finish(supervisor, 0)

    assert supervisor.offset == 5
    assert supervisor.aligned
    assert supervisor.confidence == {"method": "manual"}


def test_explicit_successful_sync_returns_manual_offset_to_automatic_mode(tmp_path) -> None:
    supervisor = supervisor_at(tmp_path)
    supervisor.request_offset_delta(5000)
    finish(supervisor, 0, mode="manual", error=MeasurementError("暂时无法识别", []))
    finish(supervisor, 0)
    finish(supervisor, 0)
    assert supervisor.offset == 5

    finish(supervisor, 1, mode="manual")
    assert supervisor.offset == 1
    assert supervisor.confidence["method"] == "ocr"
    finish(supervisor, 3)
    finish(supervisor, 3)
    assert supervisor.offset == 3


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


@pytest.mark.parametrize("invalidate", ["roi", "offset", "source"])
def test_ocr_images_and_progress_cannot_overwrite_new_user_settings(
    tmp_path, monkeypatch, invalidate
):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("https://v.example/live.flv")
    supervisor.bili = Source("https://b.example/live.flv")
    entered, release = threading.Event(), threading.Event()
    callbacks = {}
    crop = np.full((12, 48, 3), (10, 100, 220), dtype=np.uint8)
    config = ProbeConfig((0.1, 0.1, 0.2, 0.1), "none", False)

    def measure(*args, **kwargs):
        callbacks.update(kwargs)
        kwargs["on_progress"](
            "video",
            OcrProgress(
                "reading",
                phase="saved",
                config=config,
                frame_index=2,
                clock=0,
                text="00:00\n",
                crop=crop,
                image=crop[:, :, 0],
            ),
        )
        entered.set()
        assert release.wait(3)
        kwargs["on_progress"]("video", OcrProgress("locked", config=config, samples=4))
        return measurement(15)

    monkeypatch.setattr("footboy.supervisor.measure_offset", measure)
    supervisor._start_measurement("manual")
    try:
        assert entered.wait(1)
        progress = supervisor.public_status()["measurement"]["sources"]["video"]["ocr"]
        assert progress["state"] == "reading" and progress["reading"]["clock"] == 0
        assert (
            not supervisor.aligned and supervisor.store.source_probe("video:video.example") is None
        )
        version = progress["reading"]["version"]
        encoded = supervisor.ocr_image("video", "crop", version)
        assert encoded is not None
        decoded = cv2.imdecode(np.frombuffer(encoded, dtype=np.uint8), cv2.IMREAD_COLOR)
        assert np.array_equal(decoded, crop)
        assert supervisor.ocr_image("video", "crop", "older") is None
        if invalidate == "roi":
            supervisor.request_roi(
                "video", {"roi": [0, 0, 0.3, 0.2], "flip": "h", "inverted": False}
            )
            assert supervisor.ocr_image("video", "crop", version) is None
        elif invalidate == "offset":
            supervisor.request_offset_delta(500)
        else:
            supervisor._source_generation += 1
        current = dict(supervisor.public_status()["measurement"]["sources"]["video"]["ocr"])
        callbacks["on_progress"]("video", OcrProgress("locked", config=config, samples=4))
        callbacks["on_preview"]("video", crop)
        assert supervisor.public_status()["measurement"]["sources"]["video"]["ocr"] == current
        assert supervisor.preview("video") is None
    finally:
        release.set()
        supervisor._measurement_thread.join(timeout=3)
    while True:
        action, payload = supervisor._events.get(timeout=1)
        if action == "measurement_done":
            supervisor._finish_measurement(*payload)
            break
    assert supervisor.offset == (0.5 if invalidate == "offset" else 0)
    if invalidate == "roi":
        assert supervisor.store.source_probe("video:video.example")["flip"] == "h"
    else:
        assert supervisor.store.source_probe("video:video.example") is None


def test_one_sources_validated_progress_does_not_apply_or_persist_offset(tmp_path, monkeypatch):
    supervisor = supervisor_at(tmp_path)
    supervisor.video = Source("https://v.example/live.flv")
    supervisor.bili = Source("https://b.example/live.flv")
    entered, release = threading.Event(), threading.Event()

    def measure(*args, **kwargs):
        kwargs["on_progress"](
            "video", OcrProgress("locked", config=measurement(15).video.config, samples=4)
        )
        entered.set()
        assert release.wait(3)
        kwargs["on_progress"]("bili", OcrProgress("error", reason="识别引擎失败"))
        raise MeasurementError("识别引擎失败", [])

    monkeypatch.setattr("footboy.supervisor.measure_offset", measure)
    supervisor._start_measurement("manual")
    try:
        assert entered.wait(1)
        assert not supervisor.aligned and supervisor.offset == 0
        assert supervisor.store.source_probe("video:video.example") is None
    finally:
        release.set()
        supervisor._measurement_thread.join(timeout=3)
    action, payload = supervisor._events.get(timeout=1)
    assert action == "measurement_done"
    supervisor._finish_measurement(*payload)
    status = supervisor.public_status()
    assert not status["aligned"] and not status["measurement"]["needs_roi"]
    assert status["measurement"]["sources"]["video"]["ocr"]["state"] == "locked"
    assert status["measurement"]["sources"]["bili"]["ocr"]["state"] == "error"
    assert supervisor.store.source_probe("video:video.example") is None


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


@pytest.mark.parametrize("change", ["offset", "audio", "both", "stop"])
def test_bootstrap_keeps_controls_responsive_during_mux_start(tmp_path, monkeypatch, change):
    supervisor = supervisor_at(
        tmp_path, video_direct=True, bili_direct=True, initial_offset=0, auto_measure=False
    )
    entered, release, responded = threading.Event(), threading.Event(), threading.Event()
    failures = []

    def probe(source, **kwargs):
        source.has_audio = True
        return source

    def start(*args, **kwargs):
        entered.set()
        assert release.wait(5)

    def bootstrap():
        try:
            supervisor._bootstrap()
        except Exception as exc:
            failures.append(exc)

    def control():
        try:
            assert supervisor.public_status()["video"] is not None
            if change in {"offset", "both"}:
                supervisor.request_offset_delta(500)
            if change in {"audio", "both"}:
                supervisor.request_audio({"original_enabled": True, "commentary_volume": 0.5})
            if change == "stop":
                supervisor.stop()
            responded.set()
        except Exception as exc:
            failures.append(exc)

    monkeypatch.setattr("footboy.supervisor.ffprobe_source", probe)
    monkeypatch.setattr(supervisor.muxer, "start", start)
    worker = threading.Thread(target=bootstrap)
    client = threading.Thread(target=control)
    worker.start()
    try:
        assert entered.wait(2)
        client.start()
        assert responded.wait(2), "HTTP controls must finish while mux start is still blocked"
    finally:
        release.set()
        worker.join(3)
        if client.ident is not None:
            client.join(3)
    assert not worker.is_alive() and not client.is_alive()
    if change == "stop":
        assert len(failures) == 1 and "已取消" in str(failures[0])
        assert supervisor.phase == "STOPPING"
        return
    assert not failures
    assert supervisor.applied_offset == 0
    assert supervisor._adjust_deadline is not None
    if change in {"offset", "both"}:
        assert supervisor.offset == 0.5
        assert supervisor.store.offset(supervisor._offset_key) == 0.5
        assert supervisor.confidence == {"method": "manual"}
    if change in {"audio", "both"}:
        assert supervisor.audio.original_enabled
        assert supervisor.audio.commentary_volume == 0.5
        assert not supervisor.muxer.audio.original_enabled


@pytest.mark.parametrize("expected_failure", [False, True])
def test_bootstrap_does_not_hide_programming_errors_as_sampling_failures(
    tmp_path, monkeypatch, caplog, expected_failure
):
    supervisor = supervisor_at(tmp_path, video_direct=True, bili_direct=True, auto_measure=False)

    def probe(source, **kwargs):
        source.has_audio = True
        return source

    def estimate(*args, **kwargs):
        if expected_failure:
            raise TimelineProbeError("https://cdn.example/live?token=private: unavailable")
        raise AssertionError("unexpected programming error")

    monkeypatch.setattr("footboy.supervisor.ffprobe_source", probe)
    monkeypatch.setattr("footboy.supervisor.estimate_initial_offset", estimate)
    monkeypatch.setattr(supervisor.muxer, "start", lambda *args, **kwargs: None)
    if expected_failure:
        supervisor._bootstrap()
        assert supervisor.phase == "RUN"
        assert "采样失败" in supervisor.message and "采样失败" in caplog.text
        assert "private" not in caplog.text
    else:
        with pytest.raises(AssertionError, match="programming error"):
            supervisor._bootstrap()


@pytest.mark.parametrize("extension", ["ts", "m4s"])
def test_health_uses_current_generation_and_sequence_despite_clock_rollback(
    tmp_path, monkeypatch, extension
):
    supervisor = supervisor_at(tmp_path)
    folder = supervisor.config.output_dir
    folder.mkdir()
    supervisor.muxer.health.generation = 42
    supervisor.muxer.health.started_at = 1000
    supervisor.muxer.health.running = True
    for name, stamp in [
        (f"seg_41_999.{extension}", 9999),
        (f"seg_42_9.{extension}", 1010),
        (f"seg_42_10.{extension}", 990),
        (f"seg_42_11.{extension}.tmp", 1020),
    ]:
        path = folder / name
        path.write_bytes(b"segment")
        os.utime(path, (stamp, stamp))
    (folder / f"seg_42_999.{extension}").mkdir()
    assert supervisor._latest_segment_signature() == (f"seg_42_10.{extension}", 10)
    now = time.monotonic()
    supervisor._last_segment_signature = (f"seg_42_9.{extension}", 9)
    supervisor._last_segment_change = now - 31
    recovered = []
    monkeypatch.setattr(supervisor, "_recover", recovered.append)
    supervisor._check_health(now)
    assert not recovered
    assert supervisor._last_segment_change == now
    supervisor._check_health(now + 31)
    assert recovered == ["30 秒没有新 HLS 分片"]


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
