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
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import av
import numpy as np
import pytest

from footboy.mux.ffmpeg import FfmpegMuxer
from footboy.probe.frames import keyframes
from footboy.sources.media_probe import MediaProbeError, ffprobe_source
from footboy.sources.models import Source

FFMPEG = os.environ.get("FOOTBOY_FFMPEG") or shutil.which("ffmpeg")
FFPROBE = os.environ.get("FOOTBOY_FFPROBE") or shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(not FFMPEG or not FFPROBE, reason="需要 FFmpeg / ffprobe")


def make_clip(path: Path, origin: float, *, hevc: bool = False, duration: float = 8) -> None:
    command = [
        str(FFMPEG),
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=700:sample_rate=48000",
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
        "50",
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
            if self.headers.get("Cookie") != "sid=fixture":
                self.send_error(403)
                return
            if self.path.endswith("?paced=1"):
                # Emit FLV tags at 4x wall speed, preserving original PTS.
                self.send_response(200)
                self.send_header("Content-Type", "video/x-flv")
                self.end_headers()
                data = (tmp_path / self.path.split("?")[0].lstrip("/")).read_bytes()
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


@pytest.mark.parametrize("offset", [-60.0, -12.375, 0.0, 7.5, 60.0])
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


@pytest.mark.skipif(
    os.environ.get("FOOTBOY_BROWSER_TESTS") != "1",
    reason="设置 FOOTBOY_BROWSER_TESTS=1 并安装 Playwright Chromium 后运行",
)
@pytest.mark.parametrize("viewport", [(1440, 1080), (390, 844)], ids=["desktop", "mobile"])
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
                "request",
                lambda request: (
                    external_requests.append(request.url)
                    if not request.url.startswith(("http://127.0.0.1:", "blob:http://127.0.0.1:"))
                    else None
                ),
            )
            page.goto(f"http://127.0.0.1:{server.port}/")
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
            page.wait_for_function("document.getElementById('player').readyState >= 2")
            assert page.evaluate("hls !== null && document.getElementById('player').error === null")
            playback_started = page.evaluate("document.getElementById('player').currentTime")
            page.wait_for_function(
                "start => document.getElementById('player').currentTime > start + 0.5",
                arg=playback_started,
            )
            assert app.session is not None
            assert app.session.video.no_proxy and not app.session.bili.no_proxy
            initial_generation = app.session.muxer.generation

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
            page.wait_for_function(
                "generation => playerGeneration !== generation && playerGeneration !== null "
                "&& document.getElementById('player').readyState >= 2",
                arg=initial_generation,
            )
            assert app.session.applied_offset == 10.5
            resumed_at = page.evaluate("document.getElementById('player').currentTime")
            page.wait_for_function(
                "start => document.getElementById('player').currentTime > start + 0.5 "
                "&& document.getElementById('player').error === null",
                arg=resumed_at,
            )
            print(
                f"WebUI {viewport[0]}px: adjusted playback in {time.monotonic() - adjusted_at:.2f}s"
            )
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
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
