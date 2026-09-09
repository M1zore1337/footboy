from __future__ import annotations

import json

from footboy.sources.models import Source
from footboy.state import StateStore


def test_source_serializes_request_context_without_duplicate_user_agent() -> None:
    source = Source(
        "https://cdn.example/live.m3u8?token=secret",
        headers={
            "User-Agent": "TestAgent",
            "referer": "https://page.example/",
            "ORIGIN": "https://page.example",
        },
        cookies=[{"name": "sid", "value": "abc", "domain": ".example"}],
        kind="hls",
    )
    assert source.user_agent == "TestAgent"
    assert source.ffmpeg_headers() == (
        "Referer: https://page.example/\r\nOrigin: https://page.example\r\nCookie: sid=abc\r\n"
    )
    assert source.ffmpeg_cookies() == "sid=abc; path=/; domain=.example;\r\n"
    assert "secret" not in json.dumps(source.public_dict())
    assert "abc" not in json.dumps(source.public_dict())


def test_state_store_round_trip(tmp_path) -> None:
    path = tmp_path / "state.json"
    state = StateStore(path)
    state.set_offset("room@domain", -4.25)
    state.set_source_probe("video:domain", {"roi": [0.1, 0.2, 0.3, 0.4], "flip": "h"})

    loaded = StateStore(path)
    assert loaded.offset("room@domain") == -4.25
    assert loaded.source_probe("video:domain") == {
        "roi": [0.1, 0.2, 0.3, 0.4],
        "flip": "h",
    }


def test_explicit_cookie_headers_reach_all_media_clients() -> None:
    source = Source("https://cdn.example/live.flv", headers={"Cookie": "sid=secret"})
    assert "Cookie: sid=secret\r\n" in source.ffmpeg_headers(include_cookies=False)
    assert "Cookie: sid=secret\r\n" in source.pyav_options()["headers"]


def test_cookie_jar_supports_explicit_ports_without_removing_domain_scope():
    source = Source(
        "https://cdn.example:8443/live/index.m3u8",
        cookies=[
            {"name": "sid", "value": "fixture", "domain": ".example", "path": "/live"},
            {"name": "other", "value": "private", "domain": "unrelated.test", "path": "/"},
        ],
    )
    jar = source.ffmpeg_cookies()
    assert "domain=.example;" in jar and "domain=.example:8443;" in jar
    assert "domain=unrelated.test;" in jar and "domain=unrelated.test:8443;" not in jar
    assert source.cookie_header() == "sid=fixture"
    assert source.pyav_options()["cookies"] == jar


def test_cookies_for_unrelated_domains_are_not_sent_as_header() -> None:
    source = Source(
        "https://cdn.example/live/index.m3u8",
        cookies=[
            {"name": "one", "value": "1", "domain": "cdn.example", "path": "/live"},
            {"name": "two", "value": "2", "domain": "unrelated.example", "path": "/"},
            {"name": "three", "value": "3", "domain": "cdn.example", "path": "/admin"},
        ],
    )
    assert source.cookie_header() == "one=1"


def test_corrupt_state_sections_and_non_finite_offset_are_ignored(tmp_path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"sources": null, "offsets": {"bad": NaN, "ok": -3.5}}')
    store = StateStore(path)
    assert store.source_probe("missing") is None
    assert store.offset("bad") is None
    assert store.offset("ok") == -3.5
