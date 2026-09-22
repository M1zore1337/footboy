from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from footboy import cli, i18n
from footboy.tools import p0


@pytest.fixture(autouse=True)
def restore_language():
    previous = i18n.get_language()
    yield
    i18n.set_language(previous)


@pytest.mark.parametrize("prefix", ["commentary", "bili"])
def test_commentary_options_start_session_with_new_and_legacy_names(monkeypatch, tmp_path, prefix):
    configs = []
    requests = []
    lifecycle = []
    cookies = tmp_path / "commentary cookies.txt"
    video_url = "https://media.example.test/match.m3u8"
    commentary_url = "https://media.example.test/commentary.flv"

    def application(config):
        configs.append(config)
        return SimpleNamespace(
            request_start=requests.append,
            close=lambda: lifecycle.append("app-close"),
        )

    monkeypatch.setattr(cli, "Application", application)
    monkeypatch.setattr(
        cli,
        "ControlServer",
        lambda *args, **kwargs: SimpleNamespace(
            start=lambda: lifecycle.append("server-start"),
            stop=lambda: lifecycle.append("server-stop"),
        ),
    )
    stopping = threading.Event()
    stopping.set()
    monkeypatch.setattr(cli.threading, "Event", lambda: stopping)
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)

    assert (
        cli.main(
            [
                "--lang",
                "en",
                "--video-page",
                video_url,
                "--video-direct",
                f"--{prefix}-room",
                commentary_url,
                f"--{prefix}-direct",
                f"--{prefix}-cookies",
                str(cookies),
                "--no-auto-measure",
                "--offset",
                "2.5",
            ]
        )
        == 0
    )

    assert len(configs) == 1
    config = configs[0]
    assert config.video_page_url == video_url
    assert config.bili_room_url == commentary_url
    assert config.video_direct is config.bili_direct is True
    assert config.cookies_file == cookies
    assert config.initial_offset == 2.5
    assert config.auto_measure is False
    assert requests == [{"video_url": video_url, "bili_url": commentary_url}]
    assert lifecycle == ["server-start", "app-close", "server-stop"]


@pytest.mark.parametrize("prefix", ["commentary", "bili"])
def test_p0_accepts_generic_and_legacy_commentary_urls_and_headers(prefix):
    arguments = [
        "--lang",
        "en",
        "--video-url",
        "https://media.example.test/match.m3u8",
        f"--{prefix}-url",
        "https://media.example.test/commentary.flv",
        f"--{prefix}-header",
        "Referer: https://www.huya.com/12345",
        f"--{prefix}-header",
        "User-Agent: Footboy test",
        "--offset",
        "-1.5",
    ]
    args = p0.parser(arguments).parse_args(arguments)
    assert args.bili_url == "https://media.example.test/commentary.flv"
    assert p0._headers(args.bili_header) == {
        "Referer": "https://www.huya.com/12345",
        "User-Agent": "Footboy test",
    }
    assert args.offset == -1.5
