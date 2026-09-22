from __future__ import annotations

import json

import pytest

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
    assert source.ffmpeg_cookies() == "sid=abc; domain=.example;\r\n"
    assert "secret" not in json.dumps(source.public_dict())
    assert "abc" not in json.dumps(source.public_dict())


def test_reader_factory_keeps_verified_metadata_and_is_never_serialized():
    issued = []

    def issue():
        source = Source(f"https://cdn.example/{len(issued)}.flv", headers={"X-Token": "new"})
        issued.append(source)
        return source

    original = Source(
        "https://cdn.example/consumed.flv",
        kind="flv",
        video_codec="h264",
        audio_codec="aac",
        width=1920,
        height=1080,
        has_audio=True,
        no_proxy=True,
        reader_factory=issue,
    )
    first, second = original.for_reader(), original.for_reader()
    assert first.url != second.url != original.url
    assert first.video_codec == "h264" and first.audio_codec == "aac" and first.has_audio
    assert first.kind == "flv" and first.no_proxy and first.width == 1920
    assert first.header("X-Token") == "new"
    assert first.for_reader() is first and second.reader_factory is None
    assert len(issued) == 2
    assert "reader_factory" not in original.to_dict()
    assert "reader_factory" not in original.public_dict()
    assert json.loads(json.dumps(original.to_dict()))["url"] == original.url


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


@pytest.mark.parametrize("content", ["broken json", "[]", '{"offsets": [], "sources": {}}'])
def test_invalid_state_is_reported_without_changing_the_original(tmp_path, caplog, content):
    path = tmp_path / "state.json"
    path.write_text(content, encoding="utf-8")
    state = StateStore(path)
    assert state.offset("room@domain") is None
    assert "state.json" in caplog.text
    assert path.read_text(encoding="utf-8") == content


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
