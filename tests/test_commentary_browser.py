"""Exercise generic commentary discovery against an authenticated local live page."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import test_media_integration as media
from test_media_integration import FFMPEG, FFPROBE, make_clip

from footboy.probe.frames import keyframes
from footboy.sources.live import LiveResolveError
from footboy.supervisor import Supervisor, SupervisorConfig

pytestmark = [
    pytest.mark.slow,
    pytest.mark.browser,
    pytest.mark.skipif(
        os.environ.get("FOOTBOY_BROWSER_TESTS") != "1" or not FFMPEG or not FFPROBE,
        reason="Requires FFmpeg and FOOTBOY_BROWSER_TESTS=1",
    ),
]


@pytest.fixture
def authenticated_media(tmp_path):
    yield from media.media_server.__wrapped__(tmp_path)


def test_commentary_browser_fallback_carries_dynamic_auth_and_cookie_file(
    tmp_path, authenticated_media
):
    base, requests = authenticated_media
    make_clip(tmp_path / "commentary.flv", 1000, duration=80)
    (tmp_path / "room.html").write_text(
        """<!doctype html><html><body><script>
        fetch('/commentary.flv?paced=1', {
          headers: {'Authorization': 'Bearer fixture', 'X-Live-Token': 'fixture'}
        });
        </script></body></html>""",
        encoding="utf-8",
    )
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        "# Netscape HTTP Cookie File\n127.0.0.1\tFALSE\t/\tFALSE\t0\tsid\tfixture\n",
        encoding="utf-8",
    )
    supervisor = Supervisor(
        SupervisorConfig(
            "https://unused.example/match",
            base + "/room.html",
            tmp_path / "hls",
            tmp_path / "state.json",
            ffmpeg=str(FFMPEG),
            ffprobe=str(FFPROBE),
            cookies_file=cookies,
            headless_sniff=True,
            auto_measure=False,
        )
    )
    supervisor.commentary_sniffer.timeout = 25

    def no_extractor(*args, **kwargs):
        raise LiveResolveError("Fixture requires browser discovery")

    supervisor.commentary_resolver = SimpleNamespace(resolve=no_extractor)
    try:
        source = supervisor._resolve_bili()
        assert source.has_audio and source.video_codec == "h264"
        assert source.header("authorization") == "Bearer fixture"
        assert source.header("x-live-token") == "fixture"
        assert source.cookie_header() == "sid=fixture"
        assert not supervisor.sniffer.running and not supervisor.commentary_sniffer.running
        assert list(keyframes(source, duration=3, max_frames=1))
        # The browser, ffprobe and decoder all request the media with its auth.
        authorized = [row for row in requests if row.get("Authorization") == "Bearer fixture"]
        assert len(authorized) >= 3
        assert all(row.get("Cookie") == "sid=fixture" for row in requests)
    finally:
        supervisor.stop()
