from __future__ import annotations

import subprocess
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from itertools import count
from threading import Event
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
import streamlink
import yt_dlp
from requests.cookies import create_cookie
from streamlink.stream.dash import DASHStream
from streamlink.stream.ffmpegmux import MuxedStream
from streamlink.stream.hls import HLSStream
from streamlink.stream.hls.m3u8 import parse_m3u8
from streamlink.stream.http import HTTPStream

from footboy.i18n import get_language, set_language
from footboy.sources.live import LiveResolveError, LiveResolver, cookies_as_dicts, direct_kind
from footboy.sources.media_probe import MediaProbeError
from footboy.sources.models import Source


@pytest.fixture(autouse=True)
def english_errors(monkeypatch):
    previous = get_language()
    set_language("en")
    monkeypatch.setattr(
        yt_dlp.YoutubeDL,
        "extract_info",
        lambda *args, **kwargs: pytest.fail("Unexpected network extraction in a unit test"),
    )
    yield
    set_language(previous)


@pytest.fixture
def cookiefile(tmp_path):
    path = tmp_path / "cookies.txt"
    path.write_text(
        "# Netscape HTTP Cookie File\n"
        ".douyu.com\tTRUE\t/\tTRUE\t0\tlogin\tprivate-login\n"
        "cdn.example\tFALSE\t/live\tTRUE\t2147483647\tmedia\tprivate-media\n"
        "cdn.example\tFALSE\t/\tFALSE\t1\texpired\tprivate-expired\n",
        encoding="utf-8",
    )
    return path


def mock_ytdlp(monkeypatch, info, on_extract=None):
    calls = []

    def extract(downloader, url, *, download):
        calls.append((downloader.params, url, download))
        if on_extract:
            on_extract(downloader)
        return info

    monkeypatch.setattr(yt_dlp.YoutubeDL, "extract_info", extract)
    return calls


def mock_streamlink(monkeypatch, build_streams):
    session = streamlink.Streamlink()
    calls = []

    def streams(url):
        calls.append(url)
        return build_streams(session)

    monkeypatch.setattr(session, "streams", streams)
    monkeypatch.setattr(streamlink, "Streamlink", lambda: session)
    return session, calls


@pytest.mark.parametrize("first,second", [("streamlink", "yt_dlp"), ("yt_dlp", "streamlink")])
def test_extractors_can_share_requests_before_and_after_each_other_is_imported(first, second):
    # Both libraries wrap urllib3's URL normalizer at import time. The supported
    # dependency versions must work in either order, including later sessions.
    script = f"""
import importlib
from requests import Request, Session
importlib.import_module({first!r})
session = Session()
assert session.prepare_request(Request('GET', 'https://cdn.example/live.flv')).url
importlib.import_module({second!r})
assert session.prepare_request(Request('GET', 'https://cdn.example/live.flv')).url
from streamlink import Streamlink
from streamlink.stream.http import HTTPStream
assert HTTPStream(Streamlink(), 'https://cdn.example/live.flv', params={{'sign': 'test'}}).url.endswith('sign=test')
"""
    subprocess.run(
        [sys.executable, "-c", script], check=True, capture_output=True, text=True, timeout=15
    )


@pytest.mark.parametrize(
    ("url", "kind"),
    [
        ("https://cdn.example/live.M3U8?sign=private", "hls"),
        ("http://cdn.example/live.flv?expires=123", "flv"),
        ("https://cdn.example/live.ts", "mpegts"),
        ("https://cdn.example/live.webm", "webm"),
        ("https://room.example/watch?stream=https://cdn.example/live.m3u8", None),
        ("https://cdn.example/live.mpd", None),
        ("https://cdn.example/audio.mp3", None),
        ("file:///tmp/live.m3u8", None),
    ],
)
def test_direct_media_detection_uses_http_path(url, kind):
    assert direct_kind(url) == kind


def test_direct_media_retains_scoped_cookies_and_validates_without_extractors(
    monkeypatch, cookiefile
):
    def unexpected(*args):
        pytest.fail("Direct media should not invoke a room extractor")

    monkeypatch.setattr(LiveResolver, "_resolve_streamlink", unexpected)
    monkeypatch.setattr(LiveResolver, "_resolve_ytdlp", unexpected)
    accepted = []

    def validate(source):
        accepted.append(source)
        source.has_audio = True
        return source

    source = LiveResolver(
        cookies_file=cookiefile,
        headers={"user-agent": "Custom Agent", "Referer": "https://www.douyu.com/1"},
    ).resolve(" https://cdn.example/live/live.m3u8?sign=private ", validate=validate)
    assert source.kind == "hls" and source.has_audio is True
    assert source.user_agent == "Custom Agent"
    assert source.header("referer") == "https://www.douyu.com/1"
    assert source.cookie_header() == "media=private-media"
    assert {cookie["name"] for cookie in source.cookies} == {"login", "media"}
    assert accepted == [source]


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/media",
        "rtmp://host/live",
        "javascript:alert(1)",
        "https://",
        "https://host:bad/a",
    ],
)
def test_non_http_or_malformed_input_never_reaches_an_extractor(monkeypatch, url):
    monkeypatch.setattr(
        LiveResolver,
        "_resolve_streamlink",
        lambda *args: pytest.fail("Invalid input reached network"),
    )
    with pytest.raises(LiveResolveError, match="HTTP"):
        LiveResolver().resolve(url)


def test_headers_with_line_breaks_are_rejected_before_fetching():
    with pytest.raises(LiveResolveError, match="line breaks"):
        LiveResolver(headers={"User-Agent": "Agent\r\nCookie: private"}).resolve(
            "https://cdn.example/live.flv"
        )


def test_netscape_cookie_loading_preserves_session_cookies_and_safe_errors(cookiefile, tmp_path):
    cookies = cookies_as_dicts(cookiefile)
    login = next(cookie for cookie in cookies if cookie["name"] == "login")
    assert login["domain"] == ".douyu.com" and "expires" not in login
    malformed = tmp_path / "bad.txt"
    malformed.write_text("# Netscape HTTP Cookie File\nprivate-cookie malformed line\n")
    with pytest.raises(LiveResolveError, match="Netscape") as captured:
        cookies_as_dicts(malformed)
    assert "private-cookie" not in "".join(traceback.format_exception(captured.value))


def test_bilibili_keeps_its_dedicated_resolver_and_header_options(monkeypatch, cookiefile):
    calls = []
    source = Source("https://cdn.example/live.flv", has_audio=True)

    class Bili:
        def __init__(self, **options):
            calls.append(options)

        def resolve(self, url):
            calls.append(url)
            return source

    monkeypatch.setattr("footboy.sources.live.BiliResolver", Bili)
    result = LiveResolver(cookiefile, {"Referer": "https://custom.example/"}).resolve(
        "https://live.bilibili.com/123"
    )
    assert result is source
    assert calls == [
        {"cookies_file": cookiefile, "headers": {"Referer": "https://custom.example/"}},
        "https://live.bilibili.com/123",
    ]


@pytest.mark.parametrize(
    ("room", "canonical", "plugin_name"),
    [
        ("https://www.douyu.com/9999", "https://www.douyu.com/9999", "douyu"),
        ("https://m.douyu.com/9999", "https://www.douyu.com/9999", "douyu"),
        (
            "https://www.douyu.com/topic/test?rid=9999",
            "https://www.douyu.com/9999?rid=9999",
            "douyu",
        ),
        ("https://www.huya.com/660000", "https://www.huya.com/660000", "huya"),
        ("https://m.huya.com/660000", "https://www.huya.com/660000", "huya"),
    ],
)
def test_douyu_and_huya_use_real_streamlink_plugin_dispatch(
    monkeypatch, room, canonical, plugin_name
):
    session = streamlink.Streamlink()
    name, plugin_class, _ = session.resolve_url(canonical, follow_redirect=False)
    assert name == plugin_name
    called = []

    def get_streams(plugin):
        called.append(plugin.url)
        return {"source": HTTPStream(session, "https://cdn.example/live.flv")}

    monkeypatch.setattr(plugin_class, "_get_streams", get_streams)
    monkeypatch.setattr(streamlink, "Streamlink", lambda: session)
    source = LiveResolver().resolve(room)
    assert source.url == "https://cdn.example/live.flv"
    assert source.kind == "flv" and called == [canonical]
    assert session.get_option("http-timeout") == 15


def test_streamlink_exports_signed_params_headers_and_acquired_cookies(monkeypatch, cookiefile):
    def streams(session):
        assert session.http.cookies.get("login", domain=".douyu.com") == "private-login"
        assert session.http.headers["User-Agent"] == "Custom Agent"
        session.http.headers.update(
            {"Referer": "https://plugin.example/", "Origin": "https://huya.com"}
        )
        session.http.cookies.set("server", "private-server", domain="cdn.example", path="/live")
        assert (
            session.http.prepare_new_request(
                url="https://www.huya.com/1", headers={"Referer": "https://plugin.example/"}
            ).headers["Referer"]
            == "https://override.example/"
        )
        return {
            "source": HTTPStream(
                session,
                "https://cdn.example/live/room.flv",
                params={"wsSecret": "private-signature", "ratio": "0"},
                headers={"Referer": "https://www.huya.com/", "X-Media-Token": "private-token"},
                cookies={"extra": "private-extra"},
            )
        }

    mock_streamlink(monkeypatch, streams)
    source = LiveResolver(
        cookiefile,
        {"user-agent": "Custom Agent", "referer": "https://override.example/"},
    ).resolve("https://www.huya.com/1")
    assert parse_qs(urlsplit(source.url).query)["wsSecret"] == ["private-signature"]
    assert source.header("referer") == "https://override.example/"
    assert source.header("origin") == "https://huya.com"
    assert source.header("x-media-token") == "private-token"
    assert source.header("cookie") is None
    assert source.header("accept-encoding") is None
    assert "private-login" not in source.cookie_header()
    assert set(source.cookie_header().split("; ")) == {
        "media=private-media",
        "server=private-server",
        "extra=private-extra",
    }
    assert len([name for name in source.headers if name.lower() == "user-agent"]) == 1


def test_explicit_cookie_header_overrides_jar_in_streamlink_source(monkeypatch):
    mock_streamlink(
        monkeypatch, lambda session: {"best": HTTPStream(session, "https://cdn.example/live.flv")}
    )
    source = LiveResolver(headers={"cookie": "session=private-explicit"}).resolve(
        "https://room.example/1"
    )
    assert source.header("cookie") == "session=private-explicit"


def test_streamlink_prefers_1080p_and_does_not_lose_audio_only_identity_behind_best(monkeypatch):
    def streams(session):
        uhd = HLSStream(session, "https://cdn.example/uhd.m3u8")
        hd = HLSStream(session, "https://cdn.example/hd.m3u8")
        audio = HLSStream(session, "https://cdn.example/audio.m3u8")
        return {"2160p": uhd, "1080p": hd, "audio_only": audio, "best": audio}

    mock_streamlink(monkeypatch, streams)
    assert LiveResolver()._resolve_streamlink("https://room.example/1").url.endswith("hd.m3u8")


@pytest.mark.parametrize("kind", ["custom", "muxed", "dash", "post", "file", "audio"])
def test_streamlink_rejects_streams_that_cannot_be_replayed_from_a_single_http_url(
    monkeypatch, kind
):
    class CustomHLS(HLSStream):
        pass

    def streams(session):
        video = HLSStream(session, "https://cdn.example/video.m3u8")
        audio = HLSStream(session, "https://cdn.example/audio.m3u8")
        stream = {
            "custom": lambda: CustomHLS(session, "https://cdn.example/custom.m3u8"),
            "muxed": lambda: MuxedStream(session, video, audio),
            "dash": lambda: DASHStream(
                session, SimpleNamespace(url="https://cdn.example/live.mpd")
            ),
            "post": lambda: HTTPStream(
                session, "https://cdn.example/live.flv", method="POST", data="x"
            ),
            "file": lambda: HTTPStream(session, "file:///tmp/live.flv"),
            "audio": lambda: HTTPStream(session, "https://cdn.example/audio.mp3"),
        }[kind]()
        return {"best": stream}

    mock_streamlink(monkeypatch, streams)
    with pytest.raises(LiveResolveError, match="directly playable"):
        LiveResolver()._resolve_streamlink("https://room.example/1")


@pytest.mark.parametrize("codecs", ["avc1.4d401f", "mp4a.40.2"])
def test_streamlink_rejects_manifest_variants_declaring_only_one_track(monkeypatch, codecs):
    def streams(session):
        master = parse_m3u8(
            '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=500000,CODECS="' + codecs + '"\ntrack.m3u8\n',
            base_uri="https://cdn.example/master.m3u8",
        )
        stream = HLSStream(session, "https://cdn.example/track.m3u8")
        setattr(stream, "multivariant" if hasattr(stream, "multivariant") else "master", master)
        return {"best": stream}

    mock_streamlink(monkeypatch, streams)
    with pytest.raises(LiveResolveError, match="directly playable"):
        LiveResolver()._resolve_streamlink("https://room.example/1")


def test_streamlink_hls_retains_combined_variant_metadata(monkeypatch):
    def streams(session):
        master = parse_m3u8(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=1280x720,"
            'CODECS="avc1.4d401f,mp4a.40.2"\ntrack.m3u8\n',
            base_uri="https://cdn.example/master.m3u8",
        )
        stream = HLSStream(session, "https://cdn.example/track.m3u8")
        setattr(stream, "multivariant" if hasattr(stream, "multivariant") else "master", master)
        return {"720p": stream, "best": stream}

    mock_streamlink(monkeypatch, streams)
    source = LiveResolver()._resolve_streamlink("https://room.example/1")
    assert (source.width, source.height, source.has_audio) == (1280, 720, True)
    assert source.kind == "hls" and source.audio_codec == "mp4a.40.2"


def test_backend_failure_falls_back_to_ytdlp(monkeypatch):
    calls = []

    def unavailable(self, url):
        calls.append("streamlink")
        raise RuntimeError("private-signature in backend response")

    def fallback(self, url):
        calls.append("yt-dlp")
        return Source("https://cdn.example/live.m3u8", has_audio=True)

    monkeypatch.setattr(LiveResolver, "_resolve_streamlink", unavailable)
    monkeypatch.setattr(LiveResolver, "_resolve_ytdlp", fallback)
    assert LiveResolver().resolve("https://room.example/1").has_audio
    assert calls == ["streamlink", "yt-dlp"]


@pytest.mark.parametrize("room", ["https://douyu.com/9999", "https://m.douyu.com/9999"])
def test_douyu_probes_once_then_refreshes_the_url_for_each_independent_reader(monkeypatch, room):
    serial = count()
    calls = []
    validated = []
    headers = {"User-Agent": "Custom Agent"}

    def resolve(self, url):
        assert self.headers == headers
        calls.append(url)
        return Source(f"https://cdn.example/live.flv?connection={next(serial)}")

    def validate(source):
        validated.append(source)
        source.video_codec = "h264"
        source.audio_codec = "aac"
        source.has_audio = True
        return source

    monkeypatch.setattr(LiveResolver, "_resolve_streamlink", resolve)
    source = LiveResolver(headers=headers).resolve(room, validate=validate)
    assert callable(source.reader_factory)
    assert len(calls) == len(validated) == 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        readers = list(pool.map(lambda _: source.reader_factory(), range(2)))
    assert len({source.url, *(reader.url for reader in readers)}) == 3
    assert len(calls) == 3 and len(validated) == 1
    assert source.has_audio and source.video_codec == "h264"
    assert all(url.startswith(("https://douyu.com/", "https://www.douyu.com/")) for url in calls)


def test_cancelled_douyu_source_cannot_refresh_a_reader_url(monkeypatch):
    stop = Event()
    calls = []

    def resolve(self, url):
        calls.append(url)
        return Source("https://cdn.example/live.flv")

    monkeypatch.setattr(LiveResolver, "_resolve_streamlink", resolve)
    source = LiveResolver(stop_event=stop).resolve("https://www.douyu.com/9999")
    stop.set()
    with pytest.raises(LiveResolveError, match="cancelled"):
        source.reader_factory()
    assert len(calls) == 1


@pytest.mark.parametrize("room", ["https://www.huya.com/1", "https://room.example/1"])
def test_other_platforms_keep_their_existing_reusable_url(monkeypatch, room):
    monkeypatch.setattr(
        LiveResolver, "_resolve_streamlink", lambda *args: Source("https://cdn.example/live.flv")
    )
    assert LiveResolver().resolve(room).reader_factory is None


def test_validation_failure_retries_a_different_backend(monkeypatch):
    monkeypatch.setattr(
        LiveResolver, "_resolve_streamlink", lambda *a: Source("https://cdn.example/silent.flv")
    )
    monkeypatch.setattr(
        LiveResolver, "_resolve_ytdlp", lambda *a: Source("https://cdn.example/combined.m3u8")
    )
    probed = []

    def validate(source):
        probed.append(source.url)
        if source.url.endswith("silent.flv"):
            raise RuntimeError("No audio")
        source.has_audio = True
        return source

    assert LiveResolver().resolve("https://room.example/1", validate=validate).has_audio
    assert probed == ["https://cdn.example/silent.flv", "https://cdn.example/combined.m3u8"]


def test_media_probe_diagnostics_remain_useful_and_redacted():
    def validate(source):
        raise MediaProbeError(
            "ffprobe rejected https://cdn.example/live.flv?sign=private: HTTP 403"
        )

    with pytest.raises(LiveResolveError) as captured:
        LiveResolver().resolve("https://cdn.example/live.flv", validate=validate)
    assert "ffprobe rejected" in str(captured.value)
    assert "private" not in str(captured.value)


def test_cancelled_validation_cannot_start_another_backend(monkeypatch):
    stop = Event()
    monkeypatch.setattr(
        LiveResolver, "_resolve_streamlink", lambda *a: Source("https://cdn.example/live.flv")
    )
    monkeypatch.setattr(
        LiveResolver, "_resolve_ytdlp", lambda *a: pytest.fail("Cancellation was swallowed")
    )

    def validate(source):
        stop.set()
        raise RuntimeError("Stopped")

    with pytest.raises(LiveResolveError, match="cancelled"):
        LiveResolver(stop_event=stop).resolve("https://room.example/1", validate=validate)


def test_final_backend_errors_do_not_disclose_urls_headers_or_chained_exceptions(monkeypatch):
    def failed(self, url):
        raise RuntimeError(
            "https://cdn.example/live.m3u8?sign=private-signature "
            "Authorization: Bearer private-auth Cookie: sid=private-cookie"
        )

    monkeypatch.setattr(LiveResolver, "_resolve_streamlink", failed)
    monkeypatch.setattr(LiveResolver, "_resolve_ytdlp", failed)
    url = "https://room.example/1?token=private-input"
    with pytest.raises(LiveResolveError) as captured:
        LiveResolver().resolve(url)
    rendered = "".join(traceback.format_exception(captured.value))
    assert "private-" not in rendered
    assert "Streamlink" in rendered and "yt-dlp" in rendered


def test_ytdlp_prefers_combined_video_under_1080p_and_keeps_format_headers(monkeypatch, cookiefile):
    initial = cookiefile.read_bytes()

    def acquired_cookie(downloader):
        assert any(cookie.name == "login" for cookie in downloader.cookiejar)
        downloader.cookiejar.set_cookie(
            create_cookie("server", "private-server", domain="cdn.example", path="/live")
        )

    calls = mock_ytdlp(
        monkeypatch,
        {
            "is_live": True,
            "http_headers": {
                "Referer": "https://extractor.example/",
                "Origin": "https://origin.example",
            },
            "formats": [
                {
                    "url": "https://cdn.example/uhd.mp4",
                    "height": 2160,
                    "vcodec": "avc1",
                    "acodec": "aac",
                },
                {
                    "url": "https://cdn.example/video.mp4",
                    "height": 1080,
                    "vcodec": "avc1",
                    "acodec": "none",
                },
                {"url": "https://cdn.example/audio.m4a", "vcodec": "none", "acodec": "aac"},
                {
                    "url": "https://cdn.example/hd.mp4",
                    "height": 720,
                    "vcodec": "avc1",
                    "acodec": "aac",
                },
                {
                    "url": "https://cdn.example/live/playlist",
                    "height": 1080,
                    "width": 1920,
                    "vcodec": "avc1",
                    "acodec": "mp4a",
                    "protocol": "m3u8_native",
                    "http_headers": {"X-Media": "private-media", "user-agent": "Extractor Agent"},
                },
            ],
        },
        acquired_cookie,
    )
    source = LiveResolver(cookiefile, {"USER-Agent": "Custom Agent"})._resolve_ytdlp(
        "https://room.example/1"
    )
    assert source.kind == "hls" and source.height == 1080 and source.has_audio
    assert source.url == "https://cdn.example/live/playlist"
    assert source.user_agent == "Custom Agent"
    assert source.header("referer") == "https://extractor.example/"
    assert source.header("origin") == "https://origin.example"
    assert source.header("x-media") == "private-media"
    assert set(source.cookie_header().split("; ")) == {
        "media=private-media",
        "server=private-server",
    }
    assert calls[0][0]["socket_timeout"] == 15
    assert calls[0][0]["http_headers"]["User-Agent"] == "Custom Agent"
    assert cookiefile.read_bytes() == initial


@pytest.mark.parametrize(
    "status",
    [{"is_live": False}, {"live_status": "was_live"}, {"live_status": "is_upcoming"}, {}],
)
def test_ytdlp_rejects_non_live_or_unconfirmed_recordings(monkeypatch, status):
    mock_ytdlp(monkeypatch, {**status, "url": "https://cdn.example/live.mp4"})
    with pytest.raises(LiveResolveError, match="offline or playing a rerun"):
        LiveResolver()._resolve_ytdlp("https://room.example/1")


@pytest.mark.parametrize(
    "invalid",
    [
        {"acodec": "none"},
        {"vcodec": "none"},
        {"audio_channels": 0},
        {"has_drm": True},
        {"url": "file:///tmp/live.mp4"},
        {"url": "https://cdn.example/live.mpd"},
        {"protocol": "http_dash_segments"},
        {"protocol": "rtmp"},
        {"requested_formats": [{"url": "https://cdn.example/audio.m4a"}]},
        {"fragments": [{"path": "1.m4s"}]},
        {"extra_param_to_segment_url": "token=private"},
        {"extra_param_to_key_url": "token=private"},
        {"format_note": "audio only"},
    ],
)
def test_ytdlp_rejects_unsupported_missing_audio_and_split_formats(monkeypatch, invalid):
    format_info = {"url": "https://cdn.example/live.mp4", "vcodec": "avc1", "acodec": "aac"}
    mock_ytdlp(monkeypatch, {"is_live": True, "formats": [{**format_info, **invalid}]})
    with pytest.raises(LiveResolveError, match="directly playable"):
        LiveResolver()._resolve_ytdlp("https://room.example/1")


def test_ytdlp_accepts_live_top_level_format_with_unknown_codecs_for_probe(monkeypatch):
    mock_ytdlp(
        monkeypatch,
        {"live_status": "is_live", "url": "https://cdn.example/live.flv", "protocol": "https"},
    )
    source = LiveResolver()._resolve_ytdlp("https://room.example/1")
    assert source.kind == "flv" and source.has_audio is None
