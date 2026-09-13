"""Real FFmpeg checks; set FOOTBOY_FFMPEG / FOOTBOY_FFPROBE or put them on PATH."""

from __future__ import annotations

import functools
import json
import os
import shutil
import subprocess
import threading
import time
from http.client import HTTPConnection
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import av
import numpy as np
import pytest

from footboy.mux.cleanup import HlsOutputCleaner
from footboy.mux.ffmpeg import AudioMix, FfmpegMuxer
from footboy.probe.frames import keyframes
from footboy.sources.media_probe import MediaProbeError, ffprobe_source
from footboy.sources.models import Source

FFMPEG = os.environ.get("FOOTBOY_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FOOTBOY_FFPROBE") or shutil.which("ffprobe")
pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="需要 FFmpeg / ffprobe"),
]


def make_clip(
    path: Path,
    origin: float,
    *,
    hevc: bool = False,
    duration: float = 8,
    frequency: int = 700,
    sample_rate: int = 48000,
    frame_rate: int = 25,
    keyframe_interval: int = 50,
) -> None:
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={frame_rate}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency={frequency}:sample_rate={sample_rate}",
        "-t",
        str(duration),
        "-c:v",
        "libx265" if hevc else "libx264",
        "-preset",
        "ultrafast",
        "-threads",
        "1",
        "-bf",
        "0",
        "-g",
        str(keyframe_interval),
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-output_ts_offset",
        str(origin),
    ]
    if hevc:
        command += [
            "-x265-params",
            "pools=1:frame-threads=1:log-level=error",
            "-tag:v",
            "hvc1",
            "-movflags",
            "+faststart",
        ]
    else:
        command += ["-sc_threshold", "0"]
    subprocess.run(command + [str(path)], check=True, capture_output=True, timeout=30)


@pytest.fixture
def media_server(tmp_path):
    requests = []
    stopping = threading.Event()

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(dict(self.headers))
            cookie = SimpleCookie(self.headers.get("Cookie", "")).get("sid")
            if cookie is None or cookie.value != "fixture":
                self.send_error(403)
                return
            if self.path.endswith(("?paced=1", "?paced=1&finite=1")):
                # Emit FLV tags at 4x wall speed, preserving original PTS.
                data = (tmp_path / self.path.split("?")[0].lstrip("/")).read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                if self.path.endswith("&finite=1"):
                    self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data[:13])
                    cursor, first, start = 13, None, time.monotonic()
                    while cursor + 11 < len(data):
                        if stopping.is_set():
                            break
                        size = int.from_bytes(data[cursor + 1 : cursor + 4], "big")
                        stamp = int.from_bytes(data[cursor + 4 : cursor + 7], "big")
                        stamp += data[cursor + 7] << 24
                        # AVC/AAC sequence headers have timestamp zero even
                        # when the actual packets retain a nonzero PTS origin.
                        is_media = data[cursor] in (8, 9) and data[cursor + 12] == 1
                        if is_media:
                            first = stamp if first is None else first
                            remaining = (stamp - first) / 4000 - (time.monotonic() - start)
                            if remaining > 0 and stopping.wait(remaining):
                                break
                        self.wfile.write(data[cursor : cursor + size + 15])
                        self.wfile.flush()
                        cursor += size + 15
                except (BrokenPipeError, ConnectionResetError):
                    pass
                return
            super().do_GET()

    handler = functools.partial(Handler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        stopping.set()
        server.shutdown()
        server.server_close()
        worker.join(2)


@pytest.fixture
def recording_proxy(monkeypatch):
    requests = []

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            requests.append(self.path)
            target = urlsplit(self.path)
            # Only the Bilibili fixture can use the proxy. Video must go direct.
            if target.hostname != "127.0.0.1" or target.path != "/bili.flv":
                self.send_error(502, "Fixture proxy refuses video")
                return
            upstream = HTTPConnection(target.hostname, target.port, timeout=5)
            try:
                upstream.request("GET", target.path, headers=dict(self.headers))
                response = upstream.getresponse()
                data = response.read()
                self.send_response(response.status)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Content-Type", response.getheader("Content-Type"))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                upstream.close()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.setenv(name, f"http://127.0.0.1:{server.server_port}")
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.setenv(name, "")
    try:
        yield requests
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def first_pts(path: Path) -> dict[str, float]:
    result = subprocess.run(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_packets",
            "-show_entries",
            "packet=codec_type,pts_time",
            "-of",
            "json",
            *(["-allowed_extensions", "ALL"] if path.suffix == ".m3u8" else []),
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    times = {}
    for packet in json.loads(result.stdout)["packets"]:
        times.setdefault(packet["codec_type"], float(packet["pts_time"]))
    return times


def first_picture(path: Path) -> np.ndarray:
    options = {"allowed_extensions": "ALL"} if path.suffix == ".m3u8" else {}
    with av.open(str(path), options=options) as container:
        return next(container.decode(video=0)).to_ndarray(format="rgb24")


def wait_for(predicate, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail("等待 FFmpeg/HLS 超时")


def wait_for_page(page, expression, *, arg=None, timeout=20):
    # Keep polling in the test driver. Browser-side wait_for_function can use
    # eval from an animation callback, which the console's CSP correctly blocks.
    wait_for(lambda: page.evaluate(expression, arg=arg), timeout=timeout)


def source(url: str, *, hevc=False) -> Source:
    return Source(
        url,
        headers={
            "Cookie": "sid=fixture",
            "Referer": "https://page.example/",
            "Origin": "https://page.example",
            "User-Agent": "FootboyTest",
        },
        kind="unknown" if hevc else "flv",
        video_codec="hevc" if hevc else "h264",
        audio_codec="aac",
        has_audio=True,
    )


@pytest.mark.parametrize("offset", [-60.0, -12.375, 0.0, 5.0, 7.5, 60.0])
def test_signed_offsets_preserve_relative_pts_and_video_pixels(tmp_path, media_server, offset):
    base, requests = media_server
    video_file, bili_file = tmp_path / "video.flv", tmp_path / "bili.flv"
    make_clip(video_file, 1000)
    make_clip(bili_file, 1000 - offset)
    video, bili = source(base + "/video.flv"), source(base + "/bili.flv")
    ffprobe_source(video, ffprobe=str(FFPROBE))
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    try:
        mux.start(video, bili, offset)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        result = first_pts(output / "live.m3u8")
        expected = first_pts(video_file)["video"] - (first_pts(bili_file)["audio"] + offset)
        assert result["video"] - result["audio"] == pytest.approx(expected, abs=0.025)
        assert np.array_equal(first_picture(video_file), first_picture(output / "live.m3u8"))
        assert mux.health.non_monotonic_dts == 0
        assert all(row.get("Cookie") == "sid=fixture" for row in requests)
        assert all(row.get("Referer") == "https://page.example/" for row in requests)
    finally:
        mux.stop()


@pytest.mark.parametrize("original,commentary", [(1, 1), (0.25, 1), (1, 0.25), (0, 1), (1, 0)])
@pytest.mark.parametrize("sample_rate", [44100, 48000])
def test_independent_audio_gains_in_actual_mux(tmp_path, original, commentary, sample_rate):
    video_file, bili_file = tmp_path / "video.flv", tmp_path / "bili.flv"
    make_clip(video_file, 1000, frequency=700, sample_rate=sample_rate)
    make_clip(bili_file, 5719, frequency=1200)
    video, bili = source(str(video_file)), source(str(bili_file))
    video.headers.clear()
    bili.headers.clear()
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    mux.audio = AudioMix(True, original, commentary)
    try:
        mux.start(video, bili, -4719)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        pts = first_pts(output / "live.m3u8")
        assert abs(pts["video"] - pts["audio"]) < 0.1
        assert np.array_equal(first_picture(video_file), first_picture(output / "live.m3u8"))
        result = subprocess.run(
            [
                str(FFMPEG),
                "-v",
                "error",
                "-i",
                str(output / "live.m3u8"),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        samples = np.frombuffer(result.stdout, dtype=np.float32)[48000:144000]
        assert len(samples) == 96000
        spectrum = abs(np.fft.rfft(samples)) / len(samples) * 2
        for frequency, gain in [(700, original), (1200, commentary)]:
            amplitude = spectrum[frequency * 2 - 1 : frequency * 2 + 2].max()
            assert amplitude == pytest.approx(0.125 * gain, abs=0.012)
    finally:
        mux.stop()


def test_mixing_respects_delayed_commentary_pts(tmp_path):
    video_file, bili_file = tmp_path / "video.flv", tmp_path / "bili.flv"
    make_clip(video_file, 1000, frequency=700, sample_rate=44100)
    make_clip(bili_file, 5719, frequency=1200)
    video = Source(str(video_file), has_audio=True, video_codec="h264")
    bili = Source(str(bili_file), has_audio=True, audio_codec="aac")
    mux = FfmpegMuxer(tmp_path / "hls", ffmpeg=str(FFMPEG))
    mux.audio = AudioMix(True, 1, 1)
    try:
        mux.start(video, bili, -4717)  # Commentary starts two seconds after the video.
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        result = subprocess.run(
            [
                str(FFMPEG),
                "-v",
                "error",
                "-i",
                str(mux.output_dir / "live.m3u8"),
                "-vn",
                "-ac",
                "1",
                "-ar",
                "48000",
                "-f",
                "f32le",
                "-",
            ],
            capture_output=True,
            check=True,
            timeout=15,
        )
        samples = np.frombuffer(result.stdout, dtype=np.float32)

        def amplitude(start, frequency):
            clip = samples[int(start * 48000) : int((start + 1) * 48000)]
            spectrum = abs(np.fft.rfft(clip)) / len(clip) * 2
            return spectrum[frequency - 1 : frequency + 2].max()

        assert amplitude(0.5, 700) > 0.1
        assert amplitude(0.5, 1200) < 0.005
        assert amplitude(3, 1200) > 0.1
    finally:
        mux.stop()


def test_initial_timeline_estimate_handles_different_origins(tmp_path):
    from footboy.probe.timeline import estimate_initial_offset

    video_file, bili_file = tmp_path / "video.flv", tmp_path / "bili.flv"
    make_clip(video_file, 1000)
    make_clip(bili_file, 5719)
    offset = estimate_initial_offset(
        source(str(video_file)), source(str(bili_file)), stop_event=threading.Event()
    )
    assert offset == pytest.approx(-4719, abs=0.5)


def test_hevc_fmp4_keeps_init_files_immutable_across_restart(tmp_path, media_server):
    base, _ = media_server
    make_clip(tmp_path / "video.mp4", 1000, hevc=True)
    make_clip(tmp_path / "bili.flv", 1003)
    video, bili = source(base + "/video.mp4", hevc=True), source(base + "/bili.flv")
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    try:
        mux.start(video, bili, -3)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        old_init = next(output.glob("init_*.mp4"))
        original = old_init.read_bytes()
        old_names = {p.name for p in output.glob("seg_*.m4s")}
        mux.restart(video, bili, -2.5)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        assert old_init.read_bytes() == original
        playlist = (output / "live.m3u8").read_text()
        assert old_init.name not in playlist
        assert not any(name in playlist for name in old_names)
        assert "#EXT-X-MAP" in playlist and "#EXT-X-DISCONTINUITY" in playlist
        assert np.array_equal(
            first_picture(tmp_path / "video.mp4"), first_picture(output / "live.m3u8")
        )
    finally:
        mux.stop()


@pytest.mark.parametrize("hevc", [False, True], ids=["mpegts", "fmp4"])
def test_repeated_mux_restarts_do_not_accumulate_retired_outputs(tmp_path, hevc):
    clip = tmp_path / ("video.mp4" if hevc else "video.flv")
    make_clip(clip, 1000, hevc=hevc, duration=36)
    video = Source(str(clip), video_codec="hevc" if hevc else "h264", audio_codec="aac")
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    cleaner = HlsOutputCleaner(output)
    extension = "m4s" if hevc else "ts"
    now = 0
    try:
        mux.start(video, video, 0, fresh=True)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0
        baseline = len(list(output.glob(f"seg_*.{extension}")))
        assert baseline == 12
        for _ in range(3):
            before = set(output.iterdir())
            mux.restart(video, video, 0)
            wait_for(lambda: mux.poll() is not None)
            assert mux.health.returncode == 0, list(mux.health.stderr_tail)
            survivors = [
                path for path in before if path.exists() and path.suffix == f".{extension}"
            ]
            assert survivors
            picture = first_picture(output / "live.m3u8")
            cleaner.collect(mux.generation, now=now)
            assert all(path.exists() for path in survivors)
            now += 61
            cleaner.collect(mux.generation, now=now)
            assert not any(path.exists() for path in survivors)
            assert len(list(output.glob(f"seg_*.{extension}"))) == baseline
            assert len(list(output.glob("init_*.mp4"))) == int(hevc)
            assert np.array_equal(picture, first_picture(output / "live.m3u8"))
            now += 1
    finally:
        mux.stop()


@pytest.mark.parametrize("recreate_muxer", [False, True], ids=["restart", "new-muxer"])
def test_hevc_to_h264_switch_produces_decodable_hls(tmp_path, recreate_muxer):
    hevc_file, video_file, bili_file = (
        tmp_path / "hevc.mp4",
        tmp_path / "video.flv",
        tmp_path / "bili.flv",
    )
    make_clip(hevc_file, 1000, hevc=True)
    # Keep the new stream shorter than the playlist window so old entries
    # cannot disappear merely because enough new segments were generated.
    make_clip(video_file, 1000, duration=2)
    make_clip(bili_file, 1003)
    hevc = Source(str(hevc_file), video_codec="hevc")
    video = Source(str(video_file), video_codec="h264")
    bili = Source(str(bili_file), audio_codec="aac")
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    try:
        mux.start(hevc, bili, -3)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        old_files = {
            path: path.read_bytes() for path in output.iterdir() if path.suffix in {".mp4", ".m4s"}
        }
        assert old_files
        if recreate_muxer:
            mux.stop()
            mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
            mux.start(video, bili, -3)
        else:
            mux.restart(video, bili, -3)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        playlist = output / "live.m3u8"
        pts = first_pts(playlist)
        expected = first_pts(video_file)["video"] - (first_pts(bili_file)["audio"] - 3)
        assert pts["video"] - pts["audio"] == pytest.approx(expected, abs=0.025)
        assert np.array_equal(first_picture(video_file), first_picture(playlist))
        content = playlist.read_text()
        assert "#EXT-X-DISCONTINUITY" in content
        assert "#EXT-X-MAP" not in content
        assert all(path.name not in content for path in old_files)
        assert all(path.read_bytes() == data for path, data in old_files.items())
        assert mux.health.non_monotonic_dts == 0
    finally:
        mux.stop()


def test_live_restart_produces_new_segments_and_closes_process(tmp_path, media_server):
    base, _ = media_server
    make_clip(tmp_path / "video.flv", 1000, duration=36)
    make_clip(tmp_path / "bili.flv", 990, duration=36)
    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    video = source(base + "/video.flv?paced=1")
    bili = source(base + "/bili.flv?paced=1")
    try:
        mux.start(video, bili, 10)
        wait_for(lambda: len(list(output.glob("seg_*.ts"))) >= 2)
        first_generation = mux.generation
        mux.restart(video, bili, 10.5)
        assert mux.generation != first_generation
        assert "#EXT-X-ENDLIST" not in (output / "live.m3u8").read_text()
        wait_for(lambda: f"seg_{mux.generation}_" in (output / "live.m3u8").read_text())
        assert "#EXT-X-DISCONTINUITY" in (output / "live.m3u8").read_text()
        assert mux.poll() is None
    finally:
        mux.stop()
    assert not mux.health.running
    assert "#EXT-X-ENDLIST" in (output / "live.m3u8").read_text()


@pytest.mark.parametrize("encrypted", [False, True], ids=["plain", "aes128"])
def test_hls_direct_access_preserves_pts_and_bili_proxy(
    tmp_path, media_server, recording_proxy, encrypted
):
    base, requests = media_server
    make_clip(tmp_path / "video.flv", 1000)
    make_clip(tmp_path / "bili.flv", 990)
    playlist = tmp_path / "input.m3u8"
    encryption = []
    if encrypted:
        key = tmp_path / "key.bin"
        key.write_bytes(bytes(range(16)))
        info = tmp_path / "key-info.txt"
        info.write_text(f"{base}/key.bin\n{key}\n", encoding="utf-8")
        encryption = ["-hls_key_info_file", str(info)]
    subprocess.run(
        [
            str(FFMPEG),
            "-v",
            "error",
            "-copyts",
            "-i",
            str(tmp_path / "video.flv"),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "2",
            "-hls_list_size",
            "0",
            "-hls_playlist_type",
            "vod",
            *encryption,
            str(playlist),
        ],
        capture_output=True,
        check=True,
        timeout=30,
    )
    reference = playlist
    if encrypted:
        # Offline comparisons use the same encrypted packets with a local key.
        reference = tmp_path / "reference.m3u8"
        reference.write_text(
            playlist.read_text(encoding="utf-8").replace(f"{base}/key.bin", "key.bin"),
            encoding="utf-8",
        )
    video, bili = source(base + "/input.m3u8"), source(base + "/bili.flv")
    video.kind = "hls"
    with pytest.raises(MediaProbeError):
        ffprobe_source(video, ffprobe=str(FFPROBE))
    assert video.url in recording_proxy
    recording_proxy.clear()

    video.no_proxy = True
    ffprobe_source(video, ffprobe=str(FFPROBE))
    frames = list(keyframes(video, duration=3, max_frames=3))
    assert len(frames) == 3
    assert frames[0][0] == pytest.approx(first_pts(reference)["video"])
    assert recording_proxy == []
    ffprobe_source(bili, ffprobe=str(FFPROBE))

    output = tmp_path / "hls"
    mux = FfmpegMuxer(output, ffmpeg=str(FFMPEG))
    try:
        mux.start(video, bili, 10)
        wait_for(lambda: mux.poll() is not None)
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        result = first_pts(output / "live.m3u8")
        expected = first_pts(reference)["video"] - (first_pts(tmp_path / "bili.flv")["audio"] + 10)
        assert result["video"] - result["audio"] == pytest.approx(expected, abs=0.025)
        assert np.array_equal(first_picture(reference), first_picture(output / "live.m3u8"))
        assert mux.health.non_monotonic_dts == 0
        assert len(recording_proxy) >= 2
        assert all(url == bili.url for url in recording_proxy)
        assert all(row.get("Cookie") == "sid=fixture" for row in requests)
    finally:
        mux.stop()


def test_short_hls_window_survives_waiting_for_delayed_commentary(tmp_path, media_server):
    base, _ = media_server
    video_file = tmp_path / "video.flv"
    make_clip(video_file, 1000, frame_rate=50, keyframe_interval=25)
    make_clip(tmp_path / "bili.flv", 988, duration=24)
    upstream = tmp_path / "upstream"
    upstream.mkdir()
    reference = upstream / "all.m3u8"
    subprocess.run(
        [
            str(FFMPEG),
            "-v",
            "error",
            "-copyts",
            "-i",
            str(video_file),
            "-c",
            "copy",
            "-f",
            "hls",
            "-hls_time",
            "0.5",
            "-hls_list_size",
            "0",
            "-hls_segment_filename",
            str(upstream / "seg_%03d.ts"),
            str(reference),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    segments = sorted(upstream.glob("seg_*.ts"))
    started = []
    requests = []

    class RollingHls(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if not started:
                started.append(time.monotonic())
            available = min(len(segments), 3 + int((time.monotonic() - started[0]) / 0.5))
            first = max(0, available - 3)
            if self.path == "/live.m3u8":
                # A 1.5-second live window expires while the muxer initially
                # waits for the older commentary, even though its URL is valid.
                lines = [
                    "#EXTM3U",
                    "#EXT-X-VERSION:3",
                    "#EXT-X-TARGETDURATION:1",
                    f"#EXT-X-MEDIA-SEQUENCE:{first}",
                ]
                for segment in segments[first:available]:
                    lines.extend(("#EXTINF:0.5,", segment.name))
                if available == len(segments):
                    lines.append("#EXT-X-ENDLIST")
                data = ("\n".join(lines) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if self.path.startswith("/seg_"):
                index = int(Path(self.path).stem.split("_")[1])
                requests.append(index)
                if index < first:
                    self.send_error(404)
                    return
            super().do_GET()

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(RollingHls, directory=str(upstream))
    )
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    video = source(f"http://127.0.0.1:{server.server_port}/live.m3u8")
    video.kind = "hls"
    bili = source(base + "/bili.flv?paced=1&finite=1")
    mux = FfmpegMuxer(tmp_path / "output", ffmpeg=str(FFMPEG))
    recorded = tmp_path / "recorded"
    recorded.mkdir()

    def capture_finished_segments():
        # The normal output playlist retires early segments. Retain a copy of
        # each completed segment so the assertion covers the entire input.
        for segment in mux.output_dir.glob("*.ts"):
            destination = recorded / segment.name
            if not destination.exists():
                shutil.copy2(segment, destination)
        return mux.poll() is not None

    buffered = None
    try:
        mux.start(video, bili, 0)
        buffered = mux._buffered_input
        wait_for(capture_finished_segments, timeout=25)
        capture_finished_segments()
        assert mux.health.returncode == 0, list(mux.health.stderr_tail)
        output_segments = sorted(recorded.glob("*.ts"))
        pts = []
        for output in output_segments:
            with av.open(str(output)) as container:
                pts.extend(
                    float(packet.pts * packet.time_base)
                    for packet in container.demux(video=0)
                    if packet.pts is not None
                )
        pts.sort()
        assert len(pts) == 400
        assert max(np.diff(pts)) == pytest.approx(0.02, abs=0.00002)
        result = first_pts(output_segments[0])
        expected = first_pts(reference)["video"] - first_pts(tmp_path / "bili.flv")["audio"]
        assert result["video"] - result["audio"] == pytest.approx(expected, abs=0.025)
        assert np.array_equal(first_picture(reference), first_picture(output_segments[0]))
        assert set(requests) == set(range(len(segments)))
    finally:
        mux.stop()
        server.shutdown()
        server.server_close()
        worker.join(2)
    if buffered is not None:
        assert buffered.process.poll() is not None
        assert not buffered._reader.is_alive()


@pytest.mark.skipif(
    os.environ.get("FOOTBOY_BROWSER_TESTS") != "1",
    reason="设置 FOOTBOY_BROWSER_TESTS=1 并安装 Playwright Chromium 后运行",
)
@pytest.mark.parametrize("viewport", [(1440, 1080), (390, 844)], ids=["desktop", "mobile"])
@pytest.mark.browser
def test_webui_playback_roi_adjustment_and_stop(tmp_path, media_server, viewport):
    from playwright.sync_api import expect, sync_playwright

    from footboy.app import Application
    from footboy.serve.http import ControlServer
    from footboy.supervisor import SupervisorConfig

    expect.set_options(timeout=20_000)
    base, _ = media_server
    make_clip(tmp_path / "video.flv", 1000, duration=160)
    make_clip(tmp_path / "bili.flv", 990, duration=160)
    app = Application(
        SupervisorConfig(
            "",
            "",
            tmp_path / "hls",
            tmp_path / "state.json",
            ffmpeg=str(FFMPEG),
            ffprobe=str(FFPROBE),
            auto_measure=False,
            initial_offset=10,
            video_direct=True,
            video_no_proxy=True,
            bili_direct=True,
        )
    )
    server = ControlServer(app, tmp_path / "hls", host="127.0.0.1", port=0)
    server.start()
    errors, external_requests = [], []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=["--autoplay-policy=no-user-gesture-required"],
            )
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})
            page.set_default_timeout(20_000)
            # Recent Chromium advertises native HLS, but its demuxer can reject
            # real live TS playlists. Keep exercising the MSE player in that case.
            page.add_init_script(
                """
                const canPlayType = HTMLMediaElement.prototype.canPlayType;
                HTMLMediaElement.prototype.canPlayType = function(type) {
                  return type === 'application/vnd.apple.mpegurl'
                    ? 'probably' : canPlayType.call(this, type);
                };
                """
            )
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on(
                "console",
                lambda message: (
                    errors.append(message.text)
                    if "Content Security Policy" in message.text
                    else None
                ),
            )
            page.on(
                "request",
                lambda request: (
                    external_requests.append(request.url)
                    if not request.url.startswith(("http://127.0.0.1:", "blob:http://127.0.0.1:"))
                    else None
                ),
            )
            page.goto(f"http://127.0.0.1:{server.port}/")
            expect(page.locator("#access-panel")).to_be_visible()
            expect(page.locator("#workspace")).to_have_attribute("inert", "")
            page.locator("#access-token").fill("不是控制密钥")
            page.locator("#access-submit").click()
            expect(page.locator("#access-message")).to_contain_text("无效或已过期")
            page.locator("#access-token").fill("invalid-control-token")
            page.locator("#access-submit").click()
            expect(page.locator("#access-message")).to_contain_text("无效或已过期")
            expect(page.locator("#access-submit")).to_be_enabled()
            page.goto(f"http://127.0.0.1:{server.port}/#token={server.access_token}")
            expect(page.locator("#access-panel")).to_be_hidden()
            assert page.evaluate("location.hash") == ""
            assert page.locator("#access-token").input_value() == ""
            page.reload()
            expect(page.locator("#access-panel")).to_be_hidden()
            expect(page.locator("#phase-text")).to_have_text("等待连接")
            expect(page.locator('[data-delta="500"]')).to_be_disabled()
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")

            page.locator("#video-url").fill(base + "/video.flv?paced=1")
            page.locator("#bili-url").fill(base + "/bili.flv?paced=1")
            page.locator(".advanced > summary").click()
            expect(page.locator("#video-direct")).to_be_checked()
            expect(page.locator("#video-no-proxy")).to_be_checked()
            expect(page.locator("#bili-direct")).to_be_checked()
            expect(page.locator("#auto-measure")).not_to_be_checked()
            expect(page.locator("#initial-offset")).to_have_value("10")
            for label in ("video", "bili"):
                page.locator(f"#{label}-headers").fill("Cookie: sid=fixture")
            page.locator("#start").click()
            expect(page.locator("#phase-text")).to_have_text("直播运行中")
            wait_for_page(page, "document.getElementById('player').readyState >= 2")
            assert page.evaluate("hls !== null && document.getElementById('player').error === null")
            playback_started = page.evaluate("document.getElementById('player').currentTime")
            wait_for_page(
                page,
                "start => document.getElementById('player').currentTime > start + 0.5",
                arg=playback_started,
            )
            assert app.session is not None
            assert app.session.video.no_proxy and not app.session.bili.no_proxy
            initial_generation = app.session.muxer.generation

            page.locator("#language").select_option("en")
            expect(page.locator("#phase-text")).to_have_text("Live stream running")
            playing_at_switch = page.evaluate("document.getElementById('player').currentTime")
            wait_for_page(
                page,
                "start => document.getElementById('player').currentTime > start + 0.5",
                arg=playing_at_switch,
            )
            assert app.session.muxer.generation == initial_generation
            page.locator("#language").select_option("zh-CN")
            expect(page.locator("#phase-text")).to_have_text("直播运行中")

            page.locator("#remeasure").click()
            expect(page.locator("#roi-empty")).to_be_hidden()
            page.locator("#flip").select_option("h")
            canvas = page.locator("#roi-canvas")
            canvas.scroll_into_view_if_needed()
            bounds = canvas.bounding_box()
            assert bounds is not None
            page.mouse.move(
                bounds["x"] + bounds["width"] * 0.05, bounds["y"] + bounds["height"] * 0.05
            )
            page.mouse.down()
            page.mouse.move(
                bounds["x"] + bounds["width"] * 0.40, bounds["y"] + bounds["height"] * 0.30, steps=8
            )
            page.mouse.up()
            page.locator("#save-roi").click()
            wait_for(lambda: app.session.store.source_probe("video:127.0.0.1") is not None)
            roi = app.session.store.source_probe("video:127.0.0.1")
            assert roi["flip"] == "h"
            assert roi["roi"] == pytest.approx((0.05, 0.05, 0.35, 0.25), abs=0.01)

            adjusted_at = time.monotonic()
            page.locator('[data-delta="500"]').click()
            expect(page.locator("#offset-value")).to_have_text("+10.500")
            wait_for_page(
                page,
                "generation => playerGeneration !== generation && playerGeneration !== null "
                "&& document.getElementById('player').readyState >= 2",
                arg=initial_generation,
            )
            assert app.session.applied_offset == 10.5
            resumed_at = page.evaluate("document.getElementById('player').currentTime")
            wait_for_page(
                page,
                "start => document.getElementById('player').currentTime > start + 0.5 "
                "&& document.getElementById('player').error === null",
                arg=resumed_at,
            )
            print(
                f"WebUI {viewport[0]}px: adjusted playback in {time.monotonic() - adjusted_at:.2f}s"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.locator("#original-enabled").check()
            page.locator("#original-volume").fill("25")
            page.locator("#commentary-volume").fill("75")
            wait_for(lambda: app.session.muxer.audio == AudioMix(True, 0.25, 0.75))
            page.locator("#commentary-volume").fill("0")
            wait_for(lambda: app.session.muxer.audio == AudioMix(True, 0.25, 0))
            expect(page.locator("#original-volume")).to_have_value("25")
            page.locator("#commentary-volume").fill("75")
            wait_for(lambda: app.session.muxer.audio == AudioMix(True, 0.25, 0.75))
            wait_for_page(
                page,
                "document.getElementById('player').readyState >= 2 "
                "&& document.getElementById('player').error === null",
            )
            screenshots = os.environ.get("FOOTBOY_SCREENSHOT_DIR")
            if screenshots:
                folder = Path(screenshots)
                folder.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(folder / f"webui-{viewport[0]}.png"), full_page=True)

            page.locator("#stop").click()
            expect(page.locator("#phase-text")).to_have_text("任务已停止")
            expect(page.locator("#start")).to_be_enabled()
            expect(page.locator('[data-delta="500"]')).to_be_disabled()
            assert not app.public_status()["active"]
            assert not app.session.muxer.health.running
            assert not errors
            assert not external_requests
            browser.close()
    finally:
        app.close()
        server.stop()


@pytest.mark.skipif(
    os.environ.get("FOOTBOY_BROWSER_TESTS") != "1",
    reason="设置 FOOTBOY_BROWSER_TESTS=1 并安装 Playwright Chromium 后运行",
)
@pytest.mark.browser
def test_webui_switches_named_iframe_line_while_preserving_manual_offset(tmp_path, media_server):
    """Synthetic streams, real browser clicks, ffprobe, FFmpeg, and HLS playback."""
    from playwright.sync_api import expect, sync_playwright

    from footboy.app import Application
    from footboy.serve.http import ControlServer
    from footboy.supervisor import SupervisorConfig

    expect.set_options(timeout=40_000)
    base, _ = media_server
    make_clip(tmp_path / "video.flv", 1000, duration=160)
    make_clip(tmp_path / "alternate.flv", 1000, duration=160)
    make_clip(tmp_path / "bili.flv", 990, duration=160)

    class PageHandler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            if self.path == "/match":
                body = (
                    '<iframe name="player" src="/empty"></iframe><iframe src="/chooser"></iframe>'
                )
            elif self.path == "/chooser":
                links = '<a target="player" href="/player-1">中文高清</a> <a target="player" href="/player-5">高清直播⑤</a>'
                body = f"<script>setTimeout(() => document.body.innerHTML = {json.dumps(links)}, 200)</script>"
            elif self.path in {"/player-1", "/player-5"}:
                filename = "video.flv" if self.path == "/player-1" else "alternate.flv"
                url = json.dumps(f"{base}/{filename}?paced=1")
                body = f"<script>fetch({url}, {{mode:'no-cors',credentials:'include'}}).catch(() => {{}})</script>"
            else:
                body = "<body></body>"
            content = ("<!doctype html><html><body>" + body + "</body></html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Set-Cookie", "sid=fixture; Path=/; SameSite=Lax")
            self.end_headers()
            self.wfile.write(content)

    line_server = ThreadingHTTPServer(("127.0.0.1", 0), PageHandler)
    line_worker = threading.Thread(target=line_server.serve_forever, daemon=True)
    line_worker.start()
    app = Application(
        SupervisorConfig(
            "",
            "",
            tmp_path / "hls",
            tmp_path / "state.json",
            ffmpeg=str(FFMPEG),
            ffprobe=str(FFPROBE),
            auto_measure=False,
            initial_offset=10,
            bili_direct=True,
            headless_sniff=True,
            video_no_proxy=True,
            video_line_text="中文高清",
        )
    )
    server = ControlServer(app, tmp_path / "hls", host="127.0.0.1", port=0)
    server.start()
    errors = []
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True, args=["--autoplay-policy=no-user-gesture-required"]
            )
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.set_default_timeout(40_000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.goto(f"http://127.0.0.1:{server.port}/#token={server.access_token}")
            expect(page.locator("#video-line-text")).to_have_value("中文高清")
            page.locator("#video-url").fill(f"http://127.0.0.1:{line_server.server_port}/match")
            page.locator("#bili-url").fill(base + "/bili.flv?paced=1")
            page.locator(".advanced > summary").click()
            page.locator("#bili-headers").fill("Cookie: sid=fixture")
            page.locator("#start").click()
            expect(page.locator("#phase-text")).to_have_text("直播运行中")
            wait_for_page(page, "document.getElementById('player').readyState >= 2", timeout=40)
            assert app.session.video.line_text == "中文高清"
            initial_generation = app.session.muxer.generation
            expect(page.locator("#line-form")).to_be_visible()
            expect(page.locator("#line-choice option")).to_have_text(["中文高清", "高清直播⑤"])
            page.locator("#line-choice").select_option("高清直播⑤")
            page.locator("#switch-line").click()
            expect(page.locator("#phase-text")).to_have_text("选择比赛线路")
            assert app.session.muxer.health.running
            assert app.session.muxer.generation == initial_generation
            page.locator('[data-delta="500"]').click()
            expect(page.locator("#offset-value")).to_have_text("+10.500")
            expect(page.locator("#current-line")).to_contain_text("当前：高清直播⑤")
            wait_for_page(
                page,
                "generation => playerGeneration !== generation && playerGeneration !== null "
                "&& document.getElementById('player').readyState >= 2",
                arg=initial_generation,
                timeout=40,
            )
            assert app.session.video.line_text == "高清直播⑤"
            assert app.session.applied_offset == 10.5
            assert app.session.confidence == {"method": "manual"}
            assert app.session.video.no_proxy
            assert app.session.video.cookies
            sampled = list(keyframes(app.session.video, duration=8, max_frames=1))
            assert sampled and sampled[0][0] >= 1000
            assert app.session.store.source_probe("video:127.0.0.1")["line_text"] == "高清直播⑤"
            start = page.evaluate("document.getElementById('player').currentTime")
            wait_for_page(
                page,
                "start => document.getElementById('player').currentTime > start + 0.5",
                arg=start,
                timeout=40,
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            assert not errors
            page.locator("#stop").click()
            expect(page.locator("#phase-text")).to_have_text("任务已停止")
            assert not app.session.muxer.health.running
            browser.close()
    finally:
        app.close()
        server.stop()
        line_server.shutdown()
        line_server.server_close()
        line_worker.join(2)
