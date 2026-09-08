from __future__ import annotations

import io
import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from footboy.sources.bili import BiliResolveError, BiliResolver, _room_id
from footboy.sources.media_probe import MediaProbeError
from footboy.sources.models import Source
from footboy.sources.sniffer import Candidate, SniffError, StreamSniffer, _playlist_segments


def test_stopping_before_sniff_cannot_reopen_browser():
    sniffer = StreamSniffer()
    sniffer.stop()
    with pytest.raises(SniffError, match="嗅探已取消"):
        sniffer.sniff("https://video.example/match")
    assert not sniffer.running


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
