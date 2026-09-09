from __future__ import annotations

import signal
from types import SimpleNamespace

import pytest

from footboy.environment import binary_crash_reason, check_binary


@pytest.mark.parametrize(
    ("output", "supported"),
    [
        ("ffmpeg version 6.0", True),
        ("ffmpeg version n7.1.2", True),
        ("ffmpeg version 5.1.8", False),
        ("ffmpeg version N-126455-gabc\nlibavformat    63.  6.100 / 63.  6.100", True),
        ("ffmpeg version N-100000-gabc\nlibavformat    59.  6.100 / 59.  6.100", False),
        ("ffmpeg version N-100000-gabc\nlibavformat    60.  6.100 / 59.  6.100", False),
        ("ffmpeg version N-126455-gabc", False),
        ("unknown program\nlibavformat    63.  6.100 / 63.  6.100", False),
    ],
)
def test_ffmpeg_release_and_git_build_compatibility(monkeypatch, output, supported):
    monkeypatch.setattr("footboy.environment.shutil.which", lambda _: "/fixture/ffmpeg")
    monkeypatch.setattr(
        "footboy.environment.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=output),
    )
    if supported:
        check_binary("ffmpeg", minimum_major=6)
    else:
        with pytest.raises(RuntimeError, match="需要 FFmpeg 6"):
            check_binary("ffmpeg", minimum_major=6)


@pytest.mark.parametrize("code", [None, 0, 1, 255, -signal.SIGTERM])
def test_normal_exit_or_termination_is_not_reported_as_native_crash(code):
    assert binary_crash_reason(code) is None


@pytest.mark.parametrize("code", [-signal.SIGSEGV, 0xC0000005, -1073741819])
def test_native_crash_codes_have_a_diagnostic(code):
    assert binary_crash_reason(code)
