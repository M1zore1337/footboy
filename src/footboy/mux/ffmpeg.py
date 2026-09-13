from __future__ import annotations

import collections
import math
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from footboy.diagnostics import redact_diagnostic
from footboy.environment import resolve_binary
from footboy.i18n import tr
from footboy.probe.timeline import sample_audio_start
from footboy.sources.models import DIRECT_HTTP_PROXY, Source

SPEED_RE = re.compile(r"speed=\s*([0-9.]+)x")
DTS_RE = re.compile(r"non[- ]monoton(?:ic|ous).*dts", re.IGNORECASE)


class MuxError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AudioMix:
    original_enabled: bool = False
    original_volume: float = 1.0
    commentary_volume: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.original_enabled, bool):
            raise ValueError(tr("The original audio toggle must be a boolean"))
        for value in (self.original_volume, self.commentary_volume):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(tr("Volume must be a number between 0 and 1"))
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(tr("Volume must be a number between 0 and 1"))

    def public_dict(self) -> dict[str, object]:
        return {
            "original_enabled": self.original_enabled,
            "original_volume": self.original_volume,
            "commentary_volume": self.commentary_volume,
        }


@dataclass(slots=True)
class MuxHealth:
    running: bool = False
    pid: int | None = None
    returncode: int | None = None
    speed: float | None = None
    non_monotonic_dts: int = 0
    started_at: float | None = None
    generation: int = 0
    stderr_tail: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=20), repr=False
    )

    def public_dict(self) -> dict[str, object]:
        value = {
            "running": self.running,
            "pid": self.pid,
            "returncode": self.returncode,
            "speed": self.speed,
            "non_monotonic_dts": self.non_monotonic_dts,
            "started_at": self.started_at,
            "generation": self.generation,
            "stderr_tail": list(self.stderr_tail)[-5:],
        }
        if self.started_at:
            value["uptime_seconds"] = round(max(0.0, time.time() - self.started_at), 1)
        return value


def build_ffmpeg_command(
    video: Source,
    bili: Source,
    offset: float,
    output_dir: str | Path,
    *,
    ffmpeg: str | Path = "ffmpeg",
    generation: int | None = None,
    audio: AudioMix | None = None,
    audio_origin: float | None = None,
) -> list[str]:
    if not math.isfinite(offset):
        raise ValueError(tr("Offset must be a finite number"))
    output = Path(output_dir).resolve()
    hevc = (video.video_codec or "").lower() in {"hevc", "h265"}
    segment_extension = "m4s" if hevc else "ts"
    prefix = f"seg_{generation}_" if generation is not None else "seg_"
    command = [
        resolve_binary(ffmpeg),
        "-hide_banner",
        "-loglevel",
        "warning",
        "-stats",
        "-stats_period",
        "2",
        "-copyts",
    ]
    command.extend(_input_options(video, video_input=True))
    command.extend(["-i", video.url, "-itsoffset", _format_offset(offset)])
    command.extend(_input_options(bili, video_input=False))
    command.extend(["-i", bili.url])
    audio = audio or AudioMix()
    filtered = audio.original_enabled or audio.commentary_volume != 1
    command.extend(["-map", "0:v:0", "-c:v", "copy"])
    if audio.original_enabled:
        if not video.has_audio:
            raise ValueError(tr("The match stream has no audio track"))
        if audio_origin is None or not math.isfinite(audio_origin):
            raise ValueError(tr("Mixing requires a valid starting PTS for the original audio"))
        # amix combines samples, not timestamps. Give both inputs the same
        # source-PTS origin before mixing, including silence or trimming.
        # first_pts is interpreted at the input sample rate. Normalize that
        # rate first so 44.1 kHz sources use the same anchor as 48 kHz sources.
        resample = (
            f"aresample=48000,aresample=48000:async=1:first_pts={round(audio_origin * 48000)}"
        )
        command.extend(
            [
                "-filter_complex",
                f"[0:a:0]{resample},volume={audio.original_volume:.6f}[original];"
                f"[1:a:0]{resample},volume={audio.commentary_volume:.6f}[commentary];"
                "[original][commentary]amix=inputs=2:duration=longest:normalize=0,"
                "alimiter=limit=0.95:level=false:latency=1[mixed]",
                "-map",
                "[mixed]",
            ]
        )
    else:
        command.extend(["-map", "1:a:0"])
        if filtered:
            command.extend(["-af", f"volume={audio.commentary_volume:.6f}"])
    if not filtered and (bili.audio_codec or "").lower() in {"aac", "mp4a"}:
        command.extend(["-c:a", "copy"])
    else:
        command.extend(["-c:a", "aac", "-b:a", "128k"])
    command.extend(
        [
            "-max_interleave_delta",
            "0",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "8",
            "-hls_delete_threshold",
            "4",
            "-hls_flags",
            "delete_segments+discont_start+temp_file+independent_segments"
            + ("+append_list" if not hevc and _can_append_mpegts_playlist(output) else ""),
            "-hls_start_number_source",
            "epoch_us",
            "-hls_segment_type",
            "fmp4" if hevc else "mpegts",
        ]
    )
    if hevc:
        init_name = f"init_{generation}.mp4" if generation is not None else "init.mp4"
        command.extend(["-tag:v", "hvc1", "-hls_fmp4_init_filename", init_name])
    command.extend(
        [
            "-hls_segment_filename",
            str(output / f"{prefix}%06d.{segment_extension}"),
            str(output / "live.m3u8"),
        ]
    )
    return command


def _can_append_mpegts_playlist(output: Path) -> bool:
    try:
        content = (output / "live.m3u8").read_text(encoding="utf-8")
    except FileNotFoundError:
        return True
    except (OSError, UnicodeError):
        return False
    # MPEG-TS append_list drops EXT-X-MAP while keeping old fMP4 segments.
    # Inspect the on-disk playlist so a new muxer instance is safe as well.
    lines = [line.strip() for line in content.splitlines()]
    return (
        "#EXTM3U" in lines
        and not any(line.startswith("#EXT-X-MAP:") for line in lines)
        and all(line.endswith(".ts") for line in lines if line and not line.startswith("#"))
    )


def _input_options(source: Source, *, video_input: bool) -> list[str]:
    options = ["-thread_queue_size", "16384", "-rw_timeout", "15000000"]
    if source.url.startswith(("http://", "https://")):
        options.extend(["-user_agent", source.user_agent])
        if source.no_proxy:
            options.extend(["-http_proxy", DIRECT_HTTP_PROXY])
    headers = source.ffmpeg_headers(include_cookies=False)
    if headers:
        options.extend(["-headers", headers])
    cookies = source.ffmpeg_cookies()
    if cookies:
        options.extend(["-cookies", cookies])
    if video_input and source.kind == "hls":
        options.extend(["-allowed_extensions", "ALL"])
    if not video_input and source.url.startswith(("http://", "https://")):
        options.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
    return options


def _format_offset(value: float) -> str:
    rendered = f"{value:.3f}".rstrip("0").rstrip(".")
    return "0" if rendered in {"", "-0"} else rendered


class FfmpegMuxer:
    def __init__(
        self,
        output_dir: str | Path,
        *,
        ffmpeg: str | Path = "ffmpeg",
        stop_event: threading.Event | None = None,
    ) -> None:
        self.output_dir = Path(output_dir).resolve()
        self.ffmpeg = ffmpeg
        self.health = MuxHealth()
        self._process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._lock = threading.RLock()
        self.generation = 0
        self.audio = AudioMix()
        self.audio_origin: float | None = None
        self._stop_event = stop_event

    def start(self, video: Source, bili: Source, offset: float, *, fresh: bool = False) -> None:
        with self._lock:
            self._check_cancelled()
            if self._process and self._process.poll() is None:
                raise MuxError(tr("FFmpeg is already running"))
            self.output_dir.mkdir(parents=True, exist_ok=True)
            if fresh:
                self._clear_generated_outputs()
            self.generation = max(self.generation + 1, time.time_ns() // 1_000_000)
            self.audio_origin = (
                sample_audio_start(video, stop_event=self._stop_event)
                if self.audio.original_enabled
                else None
            )
            command = build_ffmpeg_command(
                video,
                bili,
                offset,
                self.output_dir,
                ffmpeg=self.ffmpeg,
                generation=self.generation,
                audio=self.audio,
                audio_origin=self.audio_origin,
            )
            flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._check_cancelled()
            try:
                self._process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=flags,
                )
            except OSError as exc:
                raise MuxError(tr("Cannot start FFmpeg: {0}", exc)) from exc
            self.health = MuxHealth(
                running=True,
                pid=self._process.pid,
                started_at=time.time(),
                generation=self.generation,
            )
            process = self._process
            health = self.health
            self._reader = threading.Thread(
                target=self._read_stderr,
                args=(process, health),
                name="ffmpeg-stderr",
                daemon=True,
            )
            self._reader.start()

    def _check_cancelled(self) -> None:
        if self._stop_event is not None and self._stop_event.is_set():
            raise MuxError(tr("Muxer startup cancelled"))

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            process = self._process
            if not process:
                return
            if process.poll() is None:
                try:
                    assert process.stdin is not None
                    process.stdin.write("q\n")
                    process.stdin.flush()
                    process.wait(timeout=timeout)
                except (OSError, subprocess.TimeoutExpired):
                    process.kill()
                    process.wait(timeout=2)
            self._refresh_exit()
        if self._reader and self._reader is not threading.current_thread():
            self._reader.join(timeout=2)
        for pipe in (process.stdin, process.stderr):
            if pipe:
                pipe.close()
        self._process = None

    def restart(self, video: Source, bili: Source, offset: float) -> None:
        self.stop()
        self.keep_playlist_live()
        self.start(video, bili, offset, fresh=False)

    def keep_playlist_live(self) -> None:
        playlist = self.output_dir / "live.m3u8"
        try:
            content = playlist.read_text(encoding="utf-8")
            temporary = playlist.with_suffix(".m3u8.tmp")
            temporary.write_text(content.replace("#EXT-X-ENDLIST\n", ""), encoding="utf-8")
            os.replace(temporary, playlist)
        except FileNotFoundError:
            pass

    def poll(self) -> int | None:
        with self._lock:
            self._refresh_exit()
            return self._process.poll() if self._process else self.health.returncode

    def command(self, video: Source, bili: Source, offset: float) -> list[str]:
        return build_ffmpeg_command(
            video,
            bili,
            offset,
            self.output_dir,
            ffmpeg=self.ffmpeg,
            audio=self.audio,
            audio_origin=self.audio_origin,
        )

    def _refresh_exit(self) -> None:
        if self._process:
            code = self._process.poll()
            if code is not None:
                self.health.running = False
                self.health.returncode = code

    def _read_stderr(self, process: subprocess.Popen[str], health: MuxHealth) -> None:
        if not process.stderr:
            return
        _consume_stderr(process.stderr, health)  # type: ignore[arg-type]
        with self._lock:
            if self._process is process:
                self._refresh_exit()

    def _clear_generated_outputs(self) -> None:
        for path in self.output_dir.iterdir():
            if path.is_file() and (
                path.name in {"live.m3u8", "live.m3u8.tmp"}
                or re.fullmatch(r"init(?:_\d+)?\.mp4(?:\.tmp)?", path.name)
                or re.fullmatch(r"seg_[\d_]+\.(?:ts|m4s)(?:\.tmp)?", path.name)
            ):
                path.unlink()


def _consume_stderr(stream: TextIO, health: MuxHealth) -> None:
    buffer = ""

    def consume(line: str) -> None:
        if not line:
            return
        health.stderr_tail.append(_sanitize_ffmpeg_line(line))
        match = SPEED_RE.search(line)
        if match:
            health.speed = float(match.group(1))
        if DTS_RE.search(line):
            health.non_monotonic_dts += 1

    while True:
        character = stream.read(1)
        if character == "":
            consume(buffer.strip())
            break
        if character not in {"\r", "\n"}:
            buffer = (buffer + character)[-8192:]
            continue
        line = buffer.strip()
        buffer = ""
        consume(line)


def _sanitize_ffmpeg_line(line: str) -> str:
    return redact_diagnostic(line)
