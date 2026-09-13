from __future__ import annotations

import datetime as dt
import logging
import math
import queue
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import cv2
import numpy as np

from footboy.environment import binary_crash_reason
from footboy.i18n import exception_message, tr
from footboy.mux.ffmpeg import AudioMix, FfmpegMuxer, MuxError, _sanitize_ffmpeg_line
from footboy.probe.ocr import OcrProgress, ProbeConfig, StoppedClock
from footboy.probe.offset import MeasurementError, OffsetMeasurement, measure_offset
from footboy.probe.timeline import TimelineProbeError, estimate_initial_offset
from footboy.serve.http import ControlServer
from footboy.sources.bili import BiliResolver
from footboy.sources.lines import validate_line_text
from footboy.sources.media_probe import MediaProbeError, ffprobe_source
from footboy.sources.models import Source
from footboy.sources.sniffer import StreamSniffer
from footboy.state import StateStore

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class SupervisorConfig:
    video_page_url: str
    bili_room_url: str
    output_dir: Path
    state_file: Path
    host: str = "0.0.0.0"
    port: int = 8080
    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    cookies_file: Path | None = None
    ocr_backend: str = "auto"
    tesseract_command: str | None = None
    auto_measure: bool = True
    initial_offset: float | None = None
    verify_interval: float = 120.0
    video_direct: bool = False
    bili_direct: bool = False
    video_headers: dict[str, str] = field(default_factory=dict)
    bili_headers: dict[str, str] = field(default_factory=dict)
    headless_sniff: bool = False
    video_no_proxy: bool = False
    video_line_text: str | None = None


class Supervisor:
    """Own one session, applying worker results only to their source generation.

    The event loop owns ffmpeg. Network/OCR workers cannot overwrite a newer
    manual revision, and never block the HTTP controller.
    """

    def __init__(self, config: SupervisorConfig) -> None:
        self.config = config
        self.store = StateStore(config.state_file)
        self._stop = threading.Event()
        self.muxer = FfmpegMuxer(config.output_dir, ffmpeg=config.ffmpeg, stop_event=self._stop)
        self.bili_resolver = BiliResolver(config.cookies_file)
        self.sniffer = StreamSniffer(ffprobe=config.ffprobe, no_proxy=config.video_no_proxy)
        self.video: Source | None = None
        self.bili: Source | None = None
        stored = self.store.offset(self._offset_key)
        self.offset = config.initial_offset if config.initial_offset is not None else stored or 0.0
        if not math.isfinite(self.offset):
            raise ValueError(tr("Offset must be a finite number"))
        self.applied_offset: float | None = None
        self.aligned = config.initial_offset is not None and not config.auto_measure
        self.confidence: dict[str, Any] | None = {"method": "manual"} if self.aligned else None
        self._manual_offset = self.aligned
        self.last_verified_at: str | None = None
        self.message = tr("Not started")
        self.phase = "INIT"
        self._events: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._lock = threading.RLock()
        self._adjust_deadline: float | None = None
        self._revision = 0
        self.audio = AudioMix()
        self._audio_revision = 0
        self._source_generation = 0
        self._verify_candidate: float | None = None
        self._video_http_failures = 0
        self._last_dts_count = 0
        self._last_segment_change = time.monotonic()
        self._last_segment_signature: tuple[str, int] | None = None
        self._last_anomaly_signature: tuple[str, int] | None = None
        self._anomaly_active = False
        self._next_verify = float("inf")
        self._next_recover = 0.0
        self._recovery_attempts = 0
        self._measurement_thread: threading.Thread | None = None
        self._measurement_cancel = threading.Event()
        self._pending_measurement: str | None = None
        self._refresh_thread: threading.Thread | None = None
        self._line_switch_pending = False
        self._refresh_revision: int | None = None
        self._preview_bytes: dict[str, bytes] = {}
        self._preview_meta: dict[str, dict[str, Any]] = {}
        self._ocr_progress: dict[str, dict[str, Any]] = {}
        self._ocr_images: dict[str, dict[str, bytes]] = {}
        self._needs_roi: list[str] = []

    def run(self, *, serve: bool = True) -> None:
        server = None
        try:
            if serve:
                server = ControlServer(
                    self, self.config.output_dir, host=self.config.host, port=self.config.port
                )
                server.start()
            self._bootstrap()
            self._run_loop()
        except Exception as exc:
            if not self._stop.is_set():
                self._set_phase(
                    "ERROR",
                    tr("Session failed: {0}", _sanitize_ffmpeg_line(exception_message(exc))),
                )
        finally:
            self._stop.set()
            self.sniffer.stop()
            self._measurement_cancel.set()
            self.muxer.stop()
            for worker in (self._measurement_thread, self._refresh_thread):
                if worker is not None:
                    worker.join(timeout=8)
            if server is not None:
                server.stop()
            if self.phase != "ERROR":
                self._set_phase("STOPPED", tr("Session stopped"))

    def stop(self) -> None:
        self._stop.set()
        self.sniffer.stop()
        self._measurement_cancel.set()
        self._events.put(("stop", None))
        if self.phase not in {"STOPPED", "ERROR"}:
            self._set_phase("STOPPING", tr("Stopping stream capture and flushing the playlist"))

    def request_offset_delta(self, delta_ms: int) -> None:
        self._queue_adjust(delta_ms)

    def request_remeasure(self) -> None:
        self._events.put(("remeasure", None))

    def request_audio(self, body: dict[str, Any]) -> None:
        with self._lock:
            values = self.audio.public_dict()
            if not body or set(body) - set(values):
                raise ValueError(tr("Invalid audio settings"))
            values.update(body)
            audio = AudioMix(**values)
            if self.video is None or self._stop.is_set():
                raise RuntimeError(tr("Wait for the streams to connect"))
            if audio.original_enabled and not self.video.has_audio:
                raise ValueError(tr("The match stream has no audio track"))
            self.audio = audio
            self._audio_revision += 1
            self._adjust_deadline = time.monotonic() + 1.5

    def request_resniff(self) -> None:
        self._events.put(("resniff", None))

    def request_select_source(self, identifier: int | None) -> None:
        self.sniffer.select(identifier)

    def request_switch_line(self, text: str) -> None:
        text = validate_line_text(text)
        assert text is not None
        with self._lock:
            if self.config.video_direct:
                raise ValueError(
                    tr(
                        "Direct media URLs have no page stream options; stop and reconnect to change the URL"
                    )
                )
            if self._stop.is_set():
                raise RuntimeError(tr("The session is stopping"))
            if self.sniffer.running:
                self.sniffer.select_line(text)
                return
            if self.video is None or self._refresh_thread is not None or self._line_switch_pending:
                raise RuntimeError(tr("Fetching streams; wait for the stream list before choosing"))
            self._line_switch_pending = True
            self._events.put(("switch_line", text))

    def request_roi(self, label: str, value: dict[str, Any]) -> None:
        if label not in {"video", "bili"}:
            raise ValueError(tr("source must be video or bili"))
        config = ProbeConfig.from_dict(value)
        key = self._video_probe_key if label == "video" else self._bili_probe_key
        with self._lock:
            saved = self.store.source_probe(key) or {}
            self.store.set_source_probe(key, {**saved, **config.to_dict()})
            self._revision += 1
            self._verify_candidate = None
            self._needs_roi = [item for item in self._needs_roi if item != label]
            self._measurement_cancel.set()
            self._ocr_progress[label] = {"state": "queued"}
            self._ocr_images.pop(label, None)
        self._events.put(("remeasure", None))

    def preview(self, label: str) -> bytes | None:
        with self._lock:
            return self._preview_bytes.get(label)

    def ocr_image(self, label: str, kind: str, version: str | None) -> bytes | None:
        with self._lock:
            reading = self._ocr_progress.get(label, {}).get("reading") or {}
            if version != reading.get("version"):
                return None
            return self._ocr_images.get(label, {}).get(kind)

    def public_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": self.phase,
                "aligned": self.aligned,
                "offset_seconds": round(self.offset, 3),
                "applied_offset_seconds": self.applied_offset,
                "adjust_pending": self._adjust_deadline is not None,
                "audio": self.audio.public_dict(),
                "applied_audio": self.muxer.audio.public_dict(),
                "confidence": self.confidence,
                "last_verified_at": self.last_verified_at,
                "message": self.message,
                "ffmpeg": self.muxer.health.public_dict(),
                "estimated_latency_seconds": tr(
                    "Slower source latency + approximately 6–10 seconds of HLS buffering"
                ),
                "video": self.video.public_dict() if self.video else None,
                "bili": self.bili.public_dict() if self.bili else None,
                "auto_measure": self.config.auto_measure,
                "video_direct": self.config.video_direct,
                "source_switching": self._line_switch_pending or self._refresh_thread is not None,
                "sniffer": self.sniffer.public_status(),
                "measurement": {
                    "running": self._measurement_thread is not None,
                    "needs_roi": list(self._needs_roi),
                    "sources": {
                        label: {
                            **self._preview_meta.get(label, {}),
                            "config": self.store.source_probe(key),
                            "ocr": dict(self._ocr_progress.get(label, {})),
                        }
                        for label, key in (
                            ("video", self._video_probe_key),
                            ("bili", self._bili_probe_key),
                        )
                    },
                },
            }

    @property
    def _offset_key(self) -> str:
        domain = urlsplit(self.config.video_page_url).hostname or "unknown"
        return f"{_bili_room_id(self.config.bili_room_url)}@{domain.lower()}"

    @property
    def _video_probe_key(self) -> str:
        return f"video:{(urlsplit(self.config.video_page_url).hostname or 'unknown').lower()}"

    @property
    def _bili_probe_key(self) -> str:
        return f"bili:{_bili_room_id(self.config.bili_room_url)}"

    def _check_cancelled(self) -> None:
        if self._stop.is_set():
            raise RuntimeError(tr("Session cancelled"))

    def _resolve_bili(self) -> Source:
        self._check_cancelled()
        source = (
            _direct_source(self.config.bili_room_url, self.config.bili_headers)
            if self.config.bili_direct
            else self.bili_resolver.resolve(self.config.bili_room_url)
        )
        self._check_cancelled()
        ffprobe_source(source, ffprobe=self.config.ffprobe)
        if not source.has_audio:
            raise MediaProbeError(
                tr("The Bilibili input has no audio; choose another room or direct media URL")
            )
        return source

    def _sniff_video(
        self, *, headless: bool, line_text: str | None = None, reuse_line: bool = True
    ) -> Source:
        self._check_cancelled()
        saved = self.store.source_probe(self._video_probe_key) or {}
        preferred = line_text
        if preferred is None and reuse_line:
            preferred = (
                self.video.line_text
                if self.video
                else (self.config.video_line_text or saved.get("line_text"))
            )
        source = self.sniffer.sniff(
            self.config.video_page_url,
            preferred_line_text=preferred,
            headless=headless,
            auto_select=reuse_line or line_text is not None,
        )
        self._check_cancelled()
        if source.line_text:
            latest = self.store.source_probe(self._video_probe_key) or {}
            self.store.set_source_probe(
                self._video_probe_key, {**latest, "line_text": source.line_text}
            )
        return source

    def _bootstrap(self) -> None:
        self._set_phase("RESOLVE", tr("Resolving and validating the Bilibili live stream"))
        self.bili = self._resolve_bili()
        self._check_cancelled()
        self._set_phase(
            "SNIFF", tr("Fetching match video; play the desired stream in the browser window")
        )
        if self.config.video_direct:
            self.video = _direct_source(self.config.video_page_url, self.config.video_headers)
            self.video.no_proxy = self.config.video_no_proxy
            ffprobe_source(self.video, ffprobe=self.config.ffprobe)
        else:
            self.video = self._sniff_video(headless=self.config.headless_sniff)
        self._check_cancelled()
        timeline_message = ""
        if (
            self.config.initial_offset is None
            and self.store.offset(self._offset_key) is None
            and self._revision == 0
        ):
            revision = self._revision
            self._set_phase("INIT", tr("Estimating the starting timelines of both streams"))
            try:
                initial = estimate_initial_offset(self.video, self.bili, stop_event=self._stop)
                with self._lock:
                    if revision == self._revision:
                        self.offset = initial
                        self.aligned = False
                        self.confidence = {"method": "timeline-estimate"}
                        timeline_message = tr(
                            "; timelines roughly joined, but match content is not yet aligned"
                        )
            except TimelineProbeError as exc:
                logger.warning(
                    tr("Initial timeline sampling failed: %s"),
                    _sanitize_ffmpeg_line(exception_message(exc)),
                )
                timeline_message = tr(
                    "; initial timeline sampling failed; audio may be temporarily silent, so remeasure or set an offset manually"
                )
        self._check_cancelled()
        with self._lock:
            self._source_generation += 1
            value, revision = self.offset, self._revision
            audio_revision = self._audio_revision
            self.muxer.audio = self.audio
        self.muxer.start(self.video, self.bili, value, fresh=True)
        self._check_cancelled()
        with self._lock:
            self.applied_offset = value
            if revision == self._revision and audio_revision == self._audio_revision:
                self._adjust_deadline = None
        self._reset_health_window()
        self._set_phase(
            "RUN", tr("Muxing started; you can adjust audio timing at any time") + timeline_message
        )
        if self.config.auto_measure:
            # Keep this check and the worker's revision snapshot atomic with
            # manual changes, including clicks made before playback was ready.
            with self._lock:
                if self.confidence is not None and self.confidence.get("method") == "manual":
                    self._schedule_verify()
                else:
                    self._start_measurement("initial")

    def _run_loop(self) -> None:
        while not self._stop.is_set():
            try:
                action, payload = self._events.get(timeout=0.2)
            except queue.Empty:
                action, payload = "tick", None
            if action == "stop":
                break
            if action == "remeasure":
                self._start_measurement("manual")
            elif action == "resniff":
                self._start_refresh(force_sniff=True)
            elif action == "switch_line":
                try:
                    self._start_refresh(force_sniff=True, line_text=payload)
                finally:
                    self._line_switch_pending = False
            elif action == "measurement_done":
                self._finish_measurement(*payload)
            elif action == "refresh_done":
                self._finish_refresh(*payload)
            now = time.monotonic()
            if self._adjust_deadline is not None and now >= self._adjust_deadline:
                if self._refresh_thread is None and self.muxer.health.running:
                    try:
                        self._apply_adjustment()
                    except Exception as exc:
                        self._recover(tr("Adjustment failed: {0}", exc))
            if now >= self._next_verify and self._adjust_deadline is None:
                if self._refresh_thread is None and self.muxer.health.running:
                    self._start_measurement("verify")
            self._check_health(time.monotonic())

    def _queue_adjust(self, delta_ms: int) -> None:
        if isinstance(delta_ms, bool) or not isinstance(delta_ms, int) or abs(delta_ms) > 300_000:
            raise ValueError(tr("delta_ms must be an integer between -300000 and 300000"))
        with self._lock:
            self.offset = round(self.offset + delta_ms / 1000.0, 3)
            self._revision += 1
            self._verify_candidate = None
            self._pending_measurement = None
            self._measurement_cancel.set()
            for progress in self._ocr_progress.values():
                progress["state"] = "cancelled"
                progress["reason"] = tr("Using the latest manual offset")
            self.aligned = True
            self.confidence = {"method": "manual"}
            self._manual_offset = True
            self.message = tr("Manual offset updated; applying in 1.5 seconds")
            self._adjust_deadline = time.monotonic() + 1.5
            self.store.set_offset(self._offset_key, self.offset)

    def _apply_adjustment(self) -> None:
        assert self.video is not None and self.bili is not None
        with self._lock:
            value, revision = self.offset, self._revision
            audio_revision = self._audio_revision
            self.muxer.audio = self.audio
        self._set_phase("ADJUST", tr("Applying offset D={0:.3f}s", value))
        self.muxer.restart(self.video, self.bili, value)
        with self._lock:
            self.applied_offset = value
            if revision == self._revision and audio_revision == self._audio_revision:
                self._adjust_deadline = None
        self._reset_health_window()
        self._set_phase(
            "RUN", tr("Offset applied; playback will continue when new segments arrive")
        )

    def _start_measurement(self, mode: str) -> None:
        if self.video is None or self.bili is None or self._stop.is_set():
            return
        if self._refresh_thread is not None:
            self._pending_measurement = mode
            self._next_verify = float("inf")
            return
        if self._measurement_thread is not None:
            if mode == "manual":
                self._pending_measurement = mode
                self._measurement_cancel.set()
            return
        with self._lock:
            self._measurement_cancel = threading.Event()
            cancellation = self._measurement_cancel
            revision, generation = self._revision, self._source_generation
            video, bili = self.video, self.bili
            self._next_verify = float("inf")
            self.message = tr(
                "Sampling both match clocks; playback and manual adjustments remain available"
            )
            self._ocr_progress = {label: {"state": "sampling"} for label in ("video", "bili")}
            self._ocr_images.clear()

        def is_current() -> bool:
            return (
                not cancellation.is_set()
                and not self._stop.is_set()
                and revision == self._revision
                and generation == self._source_generation
            )

        def capture_preview(label: str, frame: np.ndarray) -> None:
            if not is_current():
                return
            height, width = frame.shape[:2]
            image = frame
            if width > 1280:
                image = cv2.resize(frame, (1280, round(height * 1280 / width)))
            ok, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 85])
            if ok:
                with self._lock:
                    if is_current():
                        self._preview_bytes[label] = encoded.tobytes()
                        self._preview_meta[label] = {
                            "available": True,
                            "width": width,
                            "height": height,
                            "version": time.time_ns() // 1_000_000,
                            "captured_at": _now_iso(),
                        }

        def capture_progress(label: str, event: OcrProgress) -> None:
            if not is_current():
                return
            details = event.public_dict()
            if details.get("reason"):
                details["reason"] = _sanitize_ffmpeg_line(details["reason"])
            if details.get("text") is not None:
                details["text"] = _sanitize_ffmpeg_line(details["text"][:256])
            images = {}
            # Keep the actual selected crop, or a candidate with a clock reading.
            # Failed global guesses must not replace useful diagnostic images.
            if event.state == "reading" and (event.phase == "saved" or event.clock is not None):
                for kind, image in (("crop", event.crop), ("processed", event.image)):
                    if image is None or not image.size:
                        continue
                    height, width = image.shape[:2]
                    scale = min(1.0, 640 / width, 256 / height)
                    if scale < 1:
                        image = cv2.resize(
                            image, (max(1, round(width * scale)), max(1, round(height * scale)))
                        )
                    ok, encoded = cv2.imencode(".png", image)
                    if ok:
                        images[kind] = encoded.tobytes()
            with self._lock:
                if not is_current():
                    return
                previous = self._ocr_progress.get(label, {})
                reading = previous.get("reading")
                if images:
                    reading = {
                        "version": str(time.time_ns()),
                        "config": details["config"],
                        "frame_index": details["frame_index"],
                        "clock": details["clock"],
                        "text": _sanitize_ffmpeg_line((details["text"] or "")[:256]),
                        "seconds": details["seconds"],
                    }
                    self._ocr_images[label] = images
                self._ocr_progress[label] = {**details, "reading": reading}

        def worker() -> None:
            result, error = None, None
            try:
                result = measure_offset(
                    video,
                    bili,
                    state=self.store,
                    video_key=self._video_probe_key,
                    bili_key=self._bili_probe_key,
                    ocr_backend=self.config.ocr_backend,
                    tesseract_command=self.config.tesseract_command,
                    allow_manual=False,
                    on_preview=capture_preview,
                    on_progress=capture_progress,
                    stop_event=cancellation,
                    persist=False,
                )
            except Exception as exc:
                error = exc
            self._events.put(("measurement_done", (mode, revision, generation, result, error)))

        self._measurement_thread = threading.Thread(target=worker, name="ocr-measure", daemon=True)
        self._measurement_thread.start()

    def _finish_measurement(
        self,
        mode: str,
        revision: int,
        generation: int,
        result: OffsetMeasurement | None,
        error: Exception | None,
    ) -> None:
        with self._lock:
            self._apply_measurement_result(mode, revision, generation, result, error)

    def _apply_measurement_result(
        self,
        mode: str,
        revision: int,
        generation: int,
        result: OffsetMeasurement | None,
        error: Exception | None,
    ) -> None:
        self._measurement_thread = None
        self._schedule_verify()
        if self._stop.is_set():
            return
        if self._pending_measurement:
            if self._refresh_thread is not None:
                return
            pending, self._pending_measurement = self._pending_measurement, None
            self._start_measurement(pending)
            return
        if revision != self._revision or generation != self._source_generation:
            self.message = tr("Kept the latest manual settings; ignored outdated OCR results")
            self._verify_candidate = None
            for progress in self._ocr_progress.values():
                if progress.get("state") not in {
                    "queued",
                    "locked",
                    "error",
                    "timeout",
                    "not_found",
                    "stopped",
                }:
                    progress["state"] = "cancelled"
            return
        self.last_verified_at = _now_iso()
        keep_manual = mode == "verify" and self._manual_offset
        if error is not None:
            self._verify_candidate = None
            self.aligned = False
            for progress in self._ocr_progress.values():
                if progress.get("state") in {
                    "sampling",
                    "searching",
                    "candidate",
                    "reading",
                    "validating",
                    "rejected",
                }:
                    progress["state"] = "stopped" if isinstance(error, StoppedClock) else "error"
                    progress["reason"] = _sanitize_ffmpeg_line(exception_message(error))
            if isinstance(error, StoppedClock):
                self.message = tr(
                    "Clock stopped; keeping D={0:.3f}s and retrying in 60 seconds", self.offset
                )
                self.confidence = {"method": "ocr", "stopped": True}
                self._schedule_verify(delay=60)
            else:
                self.message = tr(
                    "Not aligned; keeping D={0:.3f}s: {1}",
                    self.offset,
                    _sanitize_ffmpeg_line(exception_message(error)),
                )
                self.confidence = None
                self._needs_roi = error.needs_roi if isinstance(error, MeasurementError) else []
            if keep_manual:
                self.aligned = True
                self.confidence = {"method": "manual"}
                self.message = tr(
                    "Automatic verification incomplete; keeping manual offset D={0:.3f}s: {1}",
                    self.offset,
                    _sanitize_ffmpeg_line(exception_message(error)),
                )
            return
        assert result is not None
        for label, key, source_result in (
            ("video", self._video_probe_key, result.video),
            ("bili", self._bili_probe_key, result.bili),
        ):
            saved = self.store.source_probe(key) or {}
            self.store.set_source_probe(key, {**saved, **source_result.config.to_dict()})
            self._ocr_progress[label] = {
                **self._ocr_progress.get(label, {}),
                "state": "locked",
                "config": source_result.config.to_dict(),
                "samples": len(source_result.samples),
                "residual": source_result.residual,
            }
        self._needs_roi = []
        self.confidence = {"method": "ocr", **result.confidence.public_dict()}
        difference = result.offset - self.offset
        if keep_manual:
            self._verify_candidate = None
            self.aligned = True
            self.confidence = {"method": "manual"}
            self.message = tr(
                "Keeping manual offset D={0:.3f}s; OCR suggests {1:+.3f}s. Select Sync now to realign automatically",
                self.offset,
                difference,
            )
            return
        if mode == "verify":
            if abs(difference) <= 1.0:
                self._verify_candidate = None
                self.aligned = True
                self.message = tr("Verification passed; offset change {0:+.3f}s", difference)
                return
            previous = self._verify_candidate
            if previous is None or abs(result.offset - previous) > 1.0:
                self._verify_candidate = result.offset
                self.aligned = False
                self.message = tr(
                    "Detected {0:+.3f}s drift; waiting for the next verification", difference
                )
                return
        self._verify_candidate = None
        with self._lock:
            self._manual_offset = False
            self.offset = round(result.offset, 3)
            self.aligned = True
            self.store.set_offset(self._offset_key, self.offset)
            if (
                mode == "manual"
                or self.applied_offset is None
                or abs(self.offset - self.applied_offset) > 0.001
            ):
                self._adjust_deadline = time.monotonic() + 1.5
            self.message = tr("OCR locked, D={0:.3f}s", self.offset)

    def _schedule_verify(self, delay: float | None = None) -> None:
        self._next_verify = (
            time.monotonic() + (delay if delay is not None else self.config.verify_interval)
            if self.config.auto_measure
            else float("inf")
        )

    def _start_refresh(self, *, force_sniff: bool = False, line_text: str | None = None) -> None:
        if self._refresh_thread is not None or self._stop.is_set():
            return
        assert self.video is not None
        previous = self.video
        if force_sniff and self.config.video_direct:
            self.message = tr("Using direct media URLs; stop and reconnect to change the URL")
            return
        self._set_phase("SNIFF" if force_sniff else "RECOVER", tr("Fetching live streams again"))
        with self._lock:
            self._measurement_cancel.set()
            self._source_generation += 1
            self._refresh_revision = self._revision
            self._verify_candidate = None
            self.aligned = False
            self.confidence = None

        def worker() -> None:
            sources, error = None, None
            try:
                bili = self._resolve_bili()
                self._check_cancelled()
                if force_sniff:
                    video = self._sniff_video(
                        headless=self.config.headless_sniff, line_text=line_text, reuse_line=False
                    )
                else:
                    video = previous
                    try:
                        ffprobe_source(video, ffprobe=self.config.ffprobe)
                    except MediaProbeError:
                        self._video_http_failures += 1
                        if self._video_http_failures < 2 or self.config.video_direct:
                            raise
                        video = self._sniff_video(headless=True)
                    self._video_http_failures = 0
                self._check_cancelled()
                sources = (video, bili)
            except Exception as exc:
                error = exc
            self._events.put(("refresh_done", (sources, error, force_sniff)))

        self._refresh_thread = threading.Thread(target=worker, name="source-refresh", daemon=True)
        self._refresh_thread.start()

    def _finish_refresh(
        self,
        sources: tuple[Source, Source] | None,
        error: Exception | None,
        forced: bool,
    ) -> None:
        self._refresh_thread = None
        refresh_revision, self._refresh_revision = self._refresh_revision, None
        if self._stop.is_set():
            return
        if error is not None:
            if forced and self.muxer.health.running:
                self._set_phase(
                    "RUN",
                    tr(
                        "Stream switch failed; keeping the original stream and current offset: {0}",
                        _sanitize_ffmpeg_line(exception_message(error)),
                    ),
                )
                self._schedule_verify()
                return
            self._recovery_attempts += 1
            delay = min(30, 5 * self._recovery_attempts)
            self._next_recover = time.monotonic() + delay
            self._set_phase(
                "RUN" if forced and self.muxer.health.running else "RECOVER",
                tr(
                    "Stream capture failed; keeping settings and retrying in {0} seconds: {1}",
                    delay,
                    _sanitize_ffmpeg_line(exception_message(error)),
                ),
            )
            return
        assert sources is not None
        self.video, self.bili = sources
        with self._lock:
            self._preview_bytes.clear()
            self._preview_meta.clear()
            self._ocr_progress.clear()
            self._ocr_images.clear()
            self._needs_roi = []
        try:
            with self._lock:
                value, revision = self.offset, self._revision
                audio_revision = self._audio_revision
                if self.audio.original_enabled and not self.video.has_audio:
                    self.audio = AudioMix(
                        False, self.audio.original_volume, self.audio.commentary_volume
                    )
                self.muxer.audio = self.audio
            self.muxer.restart(self.video, self.bili, value)
            with self._lock:
                self.applied_offset = value
                if revision == self._revision and audio_revision == self._audio_revision:
                    self._adjust_deadline = None
        except Exception as exc:
            self._next_recover = time.monotonic() + 5
            self.message = tr("Muxer restart failed; retrying in 5 seconds: {0}", exc)
            return
        self._recovery_attempts = 0
        self._reset_health_window()
        self._set_phase(
            "RUN", tr("Resumed with the previous offset; the source timeline needs verification")
        )
        with self._lock:
            # As in bootstrap, the manual-change decision and measurement
            # revision snapshot must be atomic with HTTP adjustment requests.
            manual_changed = (
                refresh_revision is not None
                and refresh_revision != self._revision
                and self.confidence is not None
                and self.confidence.get("method") == "manual"
            )
            if manual_changed and self._pending_measurement is None:
                self.message = tr(
                    "Stream switched; keeping the latest manual offset set during the switch"
                )
                self._schedule_verify()
            elif self.config.auto_measure or self._pending_measurement == "manual":
                if self._measurement_thread is not None:
                    self._pending_measurement = "initial"
                else:
                    pending, self._pending_measurement = self._pending_measurement, None
                    self._start_measurement(pending or "initial")

    def _reset_health_window(self) -> None:
        self._last_segment_change = time.monotonic()
        self._last_segment_signature = None
        self._last_anomaly_signature = None
        self._anomaly_active = False
        self._last_dts_count = 0

    def _check_health(self, now: float) -> None:
        if self._refresh_thread is not None or now < self._next_recover:
            return
        code = self.muxer.poll()
        if code is not None or not self.muxer.health.running:
            crash = binary_crash_reason(code)
            if crash:
                self.aligned = False
                self.confidence = None
                raise MuxError(
                    tr(
                        "FFmpeg crashed ({0}); automatic retries stopped. Check or replace the FFmpeg build, then reconnect",
                        crash,
                    )
                )
            self._recover(tr("FFmpeg exited, code={0}", code))
            return
        signature = self._latest_segment_signature()
        if signature is not None and signature != self._last_segment_signature:
            self._last_segment_signature = signature
            self._last_segment_change = now
        # D contains the PTS origin difference, so abs(D) is not buffer time.
        deadline = 150 if self._last_segment_signature is None else 30
        if now - self._last_segment_change > deadline:
            self._recover(tr("No new HLS segment for {0} seconds", deadline))
            return
        dts_count = self.muxer.health.non_monotonic_dts
        if dts_count - self._last_dts_count >= 20:
            self._last_dts_count = dts_count
            if self.config.auto_measure and self._measurement_thread is None:
                self._next_verify = min(self._next_verify, now)
        if signature != self._last_anomaly_signature:
            self._last_anomaly_signature = signature
            anomalous = self._playlist_duration_anomaly()
            if anomalous and not self._anomaly_active and self.config.auto_measure:
                self._next_verify = min(self._next_verify, now)
            self._anomaly_active = anomalous

    def _recover(self, reason: str) -> None:
        if self._stop.is_set() or self._refresh_thread is not None:
            return
        self._set_phase("RECOVER", reason)
        self.muxer.stop()
        self.muxer.keep_playlist_live()
        self._start_refresh()

    def _latest_segment_signature(self) -> tuple[str, int] | None:
        try:
            paths = list(self.config.output_dir.iterdir())
        except OSError:
            return None
        newest: tuple[str, int] | None = None
        pattern = re.compile(rf"seg_{self.muxer.health.generation}_(\d+)\.(?:ts|m4s)")
        for path in paths:
            match = pattern.fullmatch(path.name)
            if match and path.is_file():
                # FFmpeg atomically renames completed segments. Their sequence
                # advances even if the wall clock moves backwards during a run.
                sequence = int(match[1])
                if newest is None or sequence > newest[1]:
                    newest = (path.name, sequence)
        return newest

    def _playlist_duration_anomaly(self) -> bool:
        try:
            text = (self.config.output_dir / "live.m3u8").read_text(encoding="utf-8")
            durations = [float(value) for value in re.findall(r"#EXTINF:([0-9.]+)", text)]
        except (OSError, UnicodeError, ValueError):
            return False
        return bool(durations and max(durations) > 8.0)

    def _set_phase(self, phase: str, message: str) -> None:
        with self._lock:
            self.phase, self.message = phase, message
        print(f"[{phase}] {message}", flush=True)


def _direct_source(url: str, headers: dict[str, str]) -> Source:
    path = urlsplit(url).path.lower()
    kind = "hls" if ".m3u8" in path else "flv" if ".flv" in path else "unknown"
    return Source(url=url, headers=dict(headers), kind=kind)


def _bili_room_id(url: str) -> str:
    parts = urlsplit(url)
    match = re.fullmatch(r"/(?:blanc/)?(\d+)/?", parts.path)
    if parts.hostname == "live.bilibili.com" and match:
        return match.group(1)
    return (parts.hostname or "unknown") + parts.path


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")
