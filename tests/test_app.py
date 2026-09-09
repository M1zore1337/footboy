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
        {"bili_url": "https://evil.example/live.bilibili.com/123"},
        {"auto_measure": "false"},
        {"video_no_proxy": "false"},
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


def test_video_proxy_setting_inherits_cli_default_and_allows_web_override(tmp_path) -> None:
    config = defaults(tmp_path)
    config.video_no_proxy = True
    assert Application(config).public_status()["defaults"]["video_no_proxy"] is True
    assert session_config(config, body()).video_no_proxy is True
    assert session_config(config, body(video_no_proxy=False)).video_no_proxy is False


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
