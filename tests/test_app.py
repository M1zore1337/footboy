from __future__ import annotations

import signal
import threading
from pathlib import Path

import pytest

from footboy.app import Application, SessionConflict, session_config
from footboy.supervisor import SupervisorConfig


def defaults(tmp_path: Path) -> SupervisorConfig:
    return SupervisorConfig("", "", tmp_path / "hls", tmp_path / "state.json")


def body(**changes):
    return {
        "video_url": "https://video.example/match",
        "bili_url": "https://live.bilibili.com/0",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"video_url": "file:///etc/passwd"},
        {"video_url": "http://x.invalid:70000/"},
        {"bili_url": "file:///etc/passwd"},
        {"bili_url": "https://user:password@live.example/room"},
        {"bili_url": "https://live.example:70000/room"},
        {"commentary_url": "https://live.example/room"},
        {"commentary_direct": "false"},
        {"auto_measure": "false"},
        {"video_no_proxy": "false"},
        {"video_line_text": ["高清直播5"]},
        {"video_line_text": "https://cdn.example/live.m3u8"},
        {"offset_seconds": float("nan")},
        {"video_headers": {"Referer": "value\r\nInjected: true"}},
    ],
)
def test_invalid_web_configuration_is_rejected(tmp_path, changes) -> None:
    with pytest.raises(ValueError):
        session_config(defaults(tmp_path), body(**changes))


def test_direct_mode_carries_cookie_header_and_manual_mode(tmp_path) -> None:
    config = session_config(
        defaults(tmp_path),
        body(
            bili_url="https://cdn.example/bili.flv?token=secret",
            bili_direct=True,
            video_direct=True,
            auto_measure=False,
            offset_seconds=-12.5,
            video_headers={"Cookie": "sid=abc"},
        ),
    )
    assert config.video_headers["Cookie"] == "sid=abc"
    assert config.initial_offset == -12.5
    assert config.auto_measure is False


@pytest.mark.parametrize(
    "url",
    [
        "https://www.douyu.com/12345",
        "https://www.huya.com/example",
        "https://www.twitch.tv/example",
        "https://www.youtube.com/watch?v=example",
        "https://live.bilibili.com/12345",
        "https://live.example/room",
        "https://cdn.example/live.m3u8?sign=temporary",
    ],
)
@pytest.mark.parametrize("key", ["commentary_url", "bili_url"])
def test_commentary_accepts_other_platforms_with_new_and_legacy_fields(tmp_path, url, key):
    config = session_config(
        defaults(tmp_path), {"video_url": "https://video.example/match", key: url}
    )
    assert config.bili_room_url == url
    assert not config.bili_direct


def test_commentary_aliases_carry_direct_mode_headers_and_defaults(tmp_path):
    config = defaults(tmp_path)
    config.bili_direct = True
    config.bili_headers = {"Referer": "https://www.huya.com/"}
    values = {
        "video_url": "https://video.example/match",
        "commentary_url": "https://cdn.example/live.m3u8",
    }
    result = session_config(config, values)
    assert result.bili_direct
    assert result.bili_headers == config.bili_headers
    result = session_config(
        config,
        {**values, "commentary_direct": False, "commentary_headers": {"Cookie": "sid=fixture"}},
    )
    assert not result.bili_direct
    assert result.bili_headers == {"Cookie": "sid=fixture"}
    assert Application(config).public_status()["defaults"]["commentary_direct"] is True


@pytest.mark.parametrize(
    ("new", "legacy"),
    [
        ({"commentary_direct": False}, {"bili_direct": True}),
        ({"commentary_headers": {"Cookie": "sid=first"}}, {"bili_headers": {}}),
    ],
)
def test_conflicting_commentary_aliases_are_rejected(tmp_path, new, legacy):
    with pytest.raises(ValueError):
        session_config(defaults(tmp_path), body(**new, **legacy))


def test_video_proxy_setting_inherits_cli_default_and_allows_web_override(tmp_path) -> None:
    config = defaults(tmp_path)
    config.video_no_proxy = True
    assert Application(config).public_status()["defaults"]["video_no_proxy"] is True
    assert session_config(config, body()).video_no_proxy is True
    assert session_config(config, body(video_no_proxy=False)).video_no_proxy is False


def test_named_line_inherits_defaults_and_supports_web_override(tmp_path):
    config = defaults(tmp_path)
    config.video_line_text = "高清直播5"
    assert Application(config).public_status()["defaults"]["video_line_text"] == "高清直播5"
    assert session_config(config, body()).video_line_text == "高清直播5"
    assert session_config(config, body(video_line_text="中文高清")).video_line_text == "中文高清"
    assert session_config(config, body(video_line_text="")).video_line_text is None
    assert session_config(config, body(video_direct=True)).video_line_text is None


def test_console_survives_stop_and_allows_another_session(tmp_path, monkeypatch) -> None:
    app = Application(defaults(tmp_path))
    entered = threading.Event()
    monkeypatch.setattr("footboy.app.check_binary", lambda *args, **kwargs: None)

    def run(session, *, serve):
        assert not serve
        entered.set()
        session._stop.wait(2)
        session.phase = "STOPPED"

    monkeypatch.setattr("footboy.app.Supervisor.run", run)
    assert app.public_status()["state"] == "IDLE"
    app.request_start(body())
    assert entered.wait(1)
    with pytest.raises(SessionConflict):
        app.request_start(body())
    app.request_offset_delta(500)
    assert app.public_status()["offset_seconds"] == 0.5
    app.request_stop()
    app._thread.join(2)
    assert app.public_status()["state"] == "STOPPED"
    assert app.public_status()["active"] is False
    entered.clear()
    app.request_start(body())
    assert entered.wait(1)
    app.close()


def test_environment_failure_is_visible_in_console(tmp_path, monkeypatch) -> None:
    app = Application(defaults(tmp_path))

    def missing(*args, **kwargs):
        raise RuntimeError("找不到 ffmpeg")

    monkeypatch.setattr("footboy.app.check_binary", missing)
    app.request_start(body())
    app._thread.join(2)
    assert app.public_status()["state"] == "ERROR"
    assert "ffmpeg" in app.public_status()["message"]
    assert not app.public_status()["active"]


def test_native_crash_leaves_console_available_for_a_new_session(tmp_path, monkeypatch) -> None:
    app = Application(defaults(tmp_path))
    monkeypatch.setattr("footboy.app.check_binary", lambda *args, **kwargs: None)
    monkeypatch.setattr("footboy.app.Supervisor._bootstrap", lambda _: None)
    monkeypatch.setattr("footboy.supervisor.FfmpegMuxer.poll", lambda _: -signal.SIGSEGV)
    try:
        for _ in range(2):
            previous = app.session
            app.request_start(body())
            app._thread.join(2)
            assert app.session is not previous
            status = app.public_status()
            assert status["state"] == "ERROR" and not status["active"]
            assert "SIGSEGV" in status["message"]
    finally:
        app.close()
