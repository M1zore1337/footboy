from __future__ import annotations

from footboy.mux.ffmpeg import (
    MuxHealth,
    _consume_stderr,
    _sanitize_ffmpeg_line,
    build_ffmpeg_command,
)
from footboy.sources.models import Source


def _sources(video_codec: str = "h264", audio_codec: str = "aac") -> tuple[Source, Source]:
    video = Source(
        "https://video.example/live.m3u8",
        headers={"User-Agent": "VUA", "Referer": "https://video.example/"},
        cookies=[{"name": "token", "value": "v"}],
        kind="hls",
        video_codec=video_codec,
    )
    bili = Source(
        "https://bili.example/live.flv",
        headers={"User-Agent": "BUA", "Referer": "https://live.bilibili.com/"},
        kind="flv",
        audio_codec=audio_codec,
    )
    return video, bili


def test_command_uses_copyts_and_signed_itsoffset(tmp_path) -> None:
    video, bili = _sources()
    command = build_ffmpeg_command(video, bili, -12.375, tmp_path)
    assert "-copyts" in command
    offset_index = command.index("-itsoffset")
    assert command[offset_index + 1] == "-12.375"
    assert offset_index > command.index(video.url)
    assert offset_index < command.index(bili.url)
    assert "-re" not in command
    assert "-start_at_zero" not in command
    assert command[command.index("-cookies") + 1] == "token=v; path=/; domain=video.example;\r\n"
    assert command[command.index("-c:v") + 1] == "copy"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("-hls_segment_type") + 1] == "mpegts"
    assert command[-2].endswith("seg_%06d.ts")


def test_hevc_uses_fmp4_and_non_aac_is_transcoded(tmp_path) -> None:
    video, bili = _sources("hevc", "opus")
    command = build_ffmpeg_command(video, bili, 3.0, tmp_path)
    assert command[command.index("-hls_segment_type") + 1] == "fmp4"
    assert command[-2].endswith("seg_%06d.m4s")
    assert "-hls_fmp4_init_filename" in command
    assert command[command.index("-c:a") + 1] == "aac"
    assert "128k" in command


def test_stderr_health_parser_handles_carriage_return_stats() -> None:
    from io import StringIO

    health = MuxHealth()
    _consume_stderr(
        StringIO("frame=1 speed=0.98x\rNon-monotonic DTS in output stream\n"),
        health,
    )
    assert health.speed == 0.98
    assert health.non_monotonic_dts == 1


def test_stderr_query_tokens_are_redacted() -> None:
    line = "https://cdn.example/live.flv?token=very-secret&expires=123: Server returned 403"
    sanitized = _sanitize_ffmpeg_line(line)
    assert "very-secret" not in sanitized
    assert "cdn.example" in sanitized
