from __future__ import annotations

import io
import json
import signal
import subprocess
import traceback
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from footboy.sources.bili import BiliResolveError, BiliResolver, _room_id
from footboy.sources.lines import is_line_label, normalize_line_text
from footboy.sources.media_probe import MediaProbeError, ffprobe_source
from footboy.sources.models import Source
from footboy.sources.sniffer import Candidate, SniffError, StreamSniffer, _playlist_segments


def test_stopping_before_sniff_cannot_reopen_browser():
    sniffer = StreamSniffer()
    sniffer.stop()
    with pytest.raises(SniffError, match="嗅探已取消"):
        sniffer.sniff("https://video.example/match")
    assert not sniffer.running


@pytest.mark.parametrize("no_proxy", [False, True])
def test_browser_fallback_keeps_the_requested_proxy_policy(no_proxy):
    launches = []
    browser = object()

    def launch(**kwargs):
        launches.append(kwargs)
        if "channel" in kwargs:
            raise RuntimeError("Browser channel not installed")
        return browser

    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    assert (
        StreamSniffer._launch_browser(playwright, RuntimeError, headless=True, no_proxy=no_proxy)
        is browser
    )
    assert len(launches) == 3
    assert all(("--no-proxy-server" in call["args"]) == no_proxy for call in launches)


def test_sniffed_source_retains_direct_access_for_probe_and_subsequent_readers(monkeypatch):
    sniffer = StreamSniffer(no_proxy=True)
    sniffer._context = SimpleNamespace(cookies=lambda _: [])
    probed = []

    def probe(source, **kwargs):
        probed.append(source)
        return source

    monkeypatch.setattr("footboy.sources.sniffer.ffprobe_source", probe)
    candidate = Candidate("https://cdn.example/live.m3u8", "hls", {}, 0, 0)
    source = sniffer._confirm(candidate)
    assert source.no_proxy and probed == [source]
    assert source.pyav_options()["http_proxy"]
    assert Source(**source.to_dict()).no_proxy


def test_probe_crash_reports_binary_failure_instead_of_rejecting_the_source(monkeypatch):
    monkeypatch.setattr(
        "footboy.sources.media_probe.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=-signal.SIGSEGV, stderr="", stdout=""),
    )
    with pytest.raises(MediaProbeError, match="ffprobe 异常终止.*SIGSEGV"):
        ffprobe_source(Source("https://cdn.example/live.m3u8"))


def test_probe_timeout_never_reports_argv_credentials_or_chained_exception(monkeypatch):
    source = Source(
        "https://cdn.example/live.m3u8?sign=private-signature",
        headers={"Authorization": "Bearer private-auth"},
        cookies=[{"name": "sid", "value": "private-cookie"}],
    )

    def timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr("footboy.sources.media_probe.subprocess.run", timeout)
    with pytest.raises(MediaProbeError, match="超时.*15") as captured:
        ffprobe_source(source)
    rendered = "".join(traceback.format_exception(captured.value))
    assert "private-" not in rendered
    assert "-headers" not in rendered and "-cookies" not in rendered


@pytest.mark.parametrize(
    "detail",
    [
        "https://cdn.example/live.m3u8?sign=private-secret: Server returned 403",
        "Request failed: Authorization: Bearer private-secret",
        "Request failed: Cookie: sid=private-secret",
    ],
)
def test_rejected_probe_diagnostics_are_redacted_before_reaching_the_sniffer(monkeypatch, detail):
    monkeypatch.setattr(
        "footboy.sources.media_probe.subprocess.run",
        lambda *a, **k: SimpleNamespace(returncode=1, stderr=detail, stdout=""),
    )
    with pytest.raises(MediaProbeError) as captured:
        ffprobe_source(Source("https://cdn.example/live.m3u8"))
    assert "private-secret" not in str(captured.value)


def response(url, body="", content_type="application/vnd.apple.mpegurl", status=200):
    return SimpleNamespace(
        url=url,
        status=status,
        headers={"content-type": content_type},
        body=lambda: body.encode(),
        request=SimpleNamespace(
            all_headers=lambda: {"user-agent": "Fixture", "referer": "https://page.example/"}
        ),
    )


def test_api_reads_live_status_at_top_level_and_returns_request_context(monkeypatch):
    requests = []
    payload = {
        "code": 0,
        "data": {
            "live_status": 1,
            "playurl_info": {
                "playurl": {
                    "stream": [
                        {
                            "protocol_name": "http_stream",
                            "format": [
                                {
                                    "format_name": "flv",
                                    "codec": [
                                        {
                                            "codec_name": "avc",
                                            "current_qn": 10000,
                                            "base_url": "/room.flv",
                                            "url_info": [
                                                {
                                                    "host": "https://cdn.example",
                                                    "extra": "?expires=123",
                                                }
                                            ],
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }
            },
        },
    }

    def urlopen(request, timeout):
        requests.append(request)
        assert timeout == 15
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    source = BiliResolver()._resolve_api("https://live.bilibili.com/0")
    assert source.url == "https://cdn.example/room.flv?expires=123"
    params = parse_qs(urlsplit(requests[0].full_url).query)
    assert params["protocol"] == ["0,1"]
    assert params["qn"] == ["10000"]
    assert source.header("referer") == "https://live.bilibili.com/0"
    assert source.header("cookie") is None
    payload["data"]["live_status"] = 2
    with pytest.raises(BiliResolveError, match="未开播或轮播"):
        BiliResolver()._resolve_api("https://live.bilibili.com/0")


@pytest.mark.parametrize(
    "url",
    [
        "https://evil.example/live.bilibili.com/123",
        "https://live.bilibili.com.evil.example/123",
        "file:///live.bilibili.com/123",
        "https://live.bilibili.com/not-a-room",
    ],
)
def test_bili_room_hostname_and_complete_path_are_validated(url):
    with pytest.raises(BiliResolveError):
        _room_id(url)


def test_hls_activity_uses_exact_segment_uris_not_directory_prefix():
    sniffer = StreamSniffer()
    a, b = "https://cdn.example/live/a.m3u8", "https://cdn.example/live/b.m3u8"
    sniffer._on_response(response(a, "#EXTM3U\n#EXTINF:2,\na_1.ts\n"))
    sniffer._on_response(response(b, "#EXTM3U\n#EXTINF:2,\nb_1.ts\n"))
    sniffer._on_request(SimpleNamespace(url="https://cdn.example/live/a_1.ts"))
    assert sniffer._candidates[a].last_segment_at > 0
    assert sniffer._candidates[b].last_segment_at == 0


@pytest.mark.parametrize("duration", [4, 6, 10])
@pytest.mark.parametrize("target_tag", [True, False])
@pytest.mark.parametrize("byte_ranges", [False, True], ids=["separate-files", "byte-ranges"])
def test_long_hls_segments_can_be_selected_automatically(
    monkeypatch, duration, target_tag, byte_ranges
):
    clock = [100.0]
    monkeypatch.setattr("footboy.sources.sniffer.time.monotonic", lambda: clock[0])
    sniffer = StreamSniffer(timeout=30)
    sniffer._started = clock[0]
    url = "https://cdn.example/live.m3u8"
    target = f"#EXT-X-TARGETDURATION:{duration}\n" if target_tag else ""
    body = "#EXTM3U\n" + target
    for index in range(4):
        body += f"#EXTINF:{duration},\n"
        body += (
            f"#EXT-X-BYTERANGE:1000@{index * 1000}\nstream.ts\n"
            if byte_ranges
            else f"seg_{index}.ts\n"
        )
    sniffer._on_response(response(url, body))
    requested = set()

    def step(milliseconds):
        clock[0] += milliseconds / 1000
        index = int((clock[0] - 100) // duration)
        if index not in requested:
            name = "stream.ts" if byte_ranges else f"seg_{index}.ts"
            sniffer._on_request(
                SimpleNamespace(
                    url=f"https://cdn.example/{name}",
                    headers={"Range": f"bytes={index * 1000}-{(index + 1) * 1000 - 1}"}
                    if byte_ranges
                    else {},
                )
            )
            requested.add(index)

    sniffer._page = SimpleNamespace(wait_for_timeout=step)
    sniffer._context = object()
    monkeypatch.setattr(sniffer, "_print_candidates", lambda *a: None)
    monkeypatch.setattr(sniffer, "_confirm", lambda candidate: Source(candidate.url))
    assert sniffer._selection_loop().url == url
    assert len(requested) >= 2 and clock[0] < 120


@pytest.mark.parametrize("retry_same_segment", [False, True])
def test_one_hls_segment_or_repeated_retries_do_not_prove_playback(monkeypatch, retry_same_segment):
    clock = [100.0]
    monkeypatch.setattr("footboy.sources.sniffer.time.monotonic", lambda: clock[0])
    sniffer = StreamSniffer(timeout=25)
    sniffer._started = clock[0]
    url = "https://cdn.example/live.m3u8"
    body = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6,\nonly.ts\n"
    sniffer._on_response(response(url, body))
    previous = [-1]

    def step(milliseconds):
        clock[0] += milliseconds / 1000
        index = int((clock[0] - 100) // 6)
        if index != previous[0] and (previous[0] == -1 or retry_same_segment):
            sniffer._on_request(SimpleNamespace(url="https://cdn.example/only.ts"))
            previous[0] = index

    sniffer._page = SimpleNamespace(wait_for_timeout=step)
    sniffer._context = object()
    monkeypatch.setattr(sniffer, "_print_candidates", lambda *a: None)
    monkeypatch.setattr(sniffer, "_confirm", lambda _: pytest.fail("No advancing live segments"))
    with pytest.raises(SniffError, match="未确认可用直播线路"):
        sniffer._selection_loop()


def test_extensionless_and_cross_host_segments_are_resolved():
    assert _playlist_segments(
        "https://one.example/live/index.m3u8",
        """
#EXTM3U
#EXT-X-KEY:METHOD=AES-128,URI="key"
#EXTINF:2,
../chunks/1?token=abc
#EXT-X-PART:DURATION=0.5,URI="https://two.example/part/2"
""",
    ) == {"https://one.example/chunks/1?token=abc", "https://two.example/part/2"}


def test_master_vod_and_failed_responses_cannot_be_selected():
    sniffer = StreamSniffer()
    for item in (
        response(
            "https://cdn.example/master.m3u8", "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000\nv.m3u8"
        ),
        response("https://cdn.example/vod.m3u8", "#EXTM3U\n#EXTINF:2,\na.ts\n#EXT-X-ENDLIST"),
        response("https://cdn.example/denied.flv", status=403),
    ):
        sniffer._on_response(item)
    assert not sniffer._candidates


def test_closed_flv_request_is_no_longer_playing():
    sniffer = StreamSniffer()
    url = "https://cdn.example/live.flv"
    sniffer._on_response(response(url, content_type="video/x-flv"))
    assert sniffer._candidates[url].active(1000)
    sniffer._on_request_finished(SimpleNamespace(url=url))
    assert not sniffer._candidates[url].active(1001)


def test_manual_probe_failure_leaves_sniffer_available_for_another_selection(monkeypatch):
    import time

    sniffer = StreamSniffer(timeout=1)
    sniffer._started = time.monotonic()
    candidates = [
        Candidate(f"https://cdn.example/{i}.flv", "flv", {}, 0, 0, identifier=i, request_open=True)
        for i in (1, 2)
    ]
    sniffer._candidates = {c.url: c for c in candidates}
    sniffer._page = SimpleNamespace(wait_for_timeout=lambda _: None)
    sniffer._context = object()
    sniffer._commands.put("enter")
    sniffer._commands.put("2")
    monkeypatch.setattr(sniffer, "_ranked", lambda now: candidates)

    def confirm(candidate):
        if candidate.identifier == 1:
            raise MediaProbeError("403")
        return Source(candidate.url)

    monkeypatch.setattr(sniffer, "_confirm", confirm)
    assert sniffer._selection_loop().url == candidates[1].url
    assert candidates[0].cancelled_until > time.monotonic()


def test_line_names_support_circled_digits_and_exclude_navigation():
    assert normalize_line_text(" 高清 直播⑤ ") == normalize_line_text("高清直播５")
    assert all(is_line_label(text) for text in ("中文高清", "高清直播⑤", "主播解说①"))
    assert not is_line_label("返回首页")
    assert not is_line_label("足球直播导航")


def test_old_line_responses_and_pending_default_candidates_are_ignored():
    sniffer = StreamSniffer()
    sniffer._activate_line("中文高清")
    old = response("https://cdn.example/old.flv", content_type="video/x-flv")
    old.request.url = old.url
    sniffer._on_request(old.request)
    sniffer._activate_line("高清直播⑤")
    sniffer._on_response(old)
    assert not sniffer._candidates
    current = response("https://cdn.example/new.flv", content_type="video/x-flv")
    sniffer._on_response(current)
    assert sniffer._ranked(0)[0].line_text == "高清直播⑤"
    sniffer._pending_line = "不存在的线路"
    assert sniffer._ranked(0) == []


def test_line_request_during_probe_prevents_accepting_previous_source(monkeypatch):
    sniffer = StreamSniffer()
    sniffer.running = True
    sniffer._activate_line("中文高清")
    sniffer._context = SimpleNamespace(cookies=lambda _: [])

    def probe(source, **kwargs):
        sniffer.select_line("高清直播5")
        return source

    monkeypatch.setattr("footboy.sources.sniffer.ffprobe_source", probe)
    candidate = Candidate("https://cdn.example/live.flv", "flv", {}, 0, 0)
    with pytest.raises(MediaProbeError, match="忽略旧线路"):
        sniffer._confirm(candidate)
    assert sniffer.public_status()["pending_line"] == "高清直播5"


def blocked_request(url="http://cdn.example/master.m3u8?token=private", failure="mixed-content"):
    return SimpleNamespace(
        url=url,
        failure=failure,
        all_headers=lambda: {"user-agent": "Fixture", "referer": "https://player.example/"},
    )


def api_response(url, body, status=200):
    return SimpleNamespace(url=url, status=status, body=lambda: body.encode(), dispose=lambda: None)


def test_mixed_content_fallback_checks_live_variant_and_preserves_context(monkeypatch):
    sniffer = StreamSniffer(no_proxy=True)
    sniffer._activate_line("高清直播⑤")
    request = blocked_request()
    calls = []

    def get(url, **kwargs):
        calls.append((url, kwargs))
        if len(calls) == 1:
            return api_response(url, "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=2000\nvideo.m3u8\n")
        return api_response(url, "#EXTM3U\n#EXTINF:2,\nchunk.ts\n")

    sniffer._context = SimpleNamespace(
        request=SimpleNamespace(get=get),
        cookies=lambda _: [{"name": "sid", "value": "fixture", "domain": "cdn.example"}],
    )
    sniffer._on_request(request)
    sniffer._on_request_failed(request)
    sniffer._read_blocked_request()
    candidate = next(iter(sniffer._candidates.values()))
    assert candidate.url == "http://cdn.example/video.m3u8"
    assert candidate.line_text == "高清直播⑤" and candidate.browser_blocked
    assert candidate.segments == {"http://cdn.example/chunk.ts"}
    assert not candidate.active(0)  # Do not claim the browser played blocked media.
    assert calls[1][1]["headers"]["referer"] == "https://player.example/"
    monkeypatch.setattr("footboy.sources.sniffer.ffprobe_source", lambda source, **_: source)
    source = sniffer._confirm(candidate)
    assert source.no_proxy and source.cookie_header() == "sid=fixture"
    assert source.header("referer") == "https://player.example/"


@pytest.mark.parametrize(
    "body,status",
    [
        ("#EXTM3U\n#EXTINF:2,\nchunk.ts\n#EXT-X-ENDLIST", 200),
        ("#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:2,\nchunk.ts", 200),
        ("#EXTM3U\n#EXTINF:2,\nchunk.ts", 403),
        ("<html>Unavailable</html>", 200),
    ],
)
def test_mixed_content_fallback_rejects_vod_and_bad_responses(body, status):
    sniffer = StreamSniffer()
    sniffer._context = SimpleNamespace(
        request=SimpleNamespace(get=lambda url, **_: api_response(url, body, status))
    )
    sniffer._on_request_failed(blocked_request())
    sniffer._read_blocked_request()
    assert not sniffer._candidates


def test_other_request_failures_do_not_bypass_response_checks():
    sniffer = StreamSniffer()
    sniffer._on_request_failed(blocked_request(failure="net::ERR_CONNECTION_REFUSED"))
    assert not sniffer._blocked_requests and not sniffer._candidates
