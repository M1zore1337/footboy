from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from footboy.environment import binary_crash_reason, check_binary, resolve_binary


@pytest.fixture
def local_tools(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("footboy.environment.shutil.which", lambda _: None)
    monkeypatch.setattr(
        "footboy.environment.__file__", str(tmp_path / "src/footboy/environment.py")
    )
    return tmp_path


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize(
    ("tool", "relative"),
    [
        ("ffmpeg", "ffmpeg/bin/ffmpeg"),
        ("ffprobe", "ffmpeg/bin/ffprobe"),
        ("tesseract", "tesseract/tesseract"),
        ("tesseract", "tesseract/bin/tesseract"),
    ],
)
def test_local_tool_fallback(local_tools, tool, relative):
    suffix = ".exe" if os.name == "nt" else ""
    expected = executable(local_tools / "tools" / (relative + suffix))
    assert resolve_binary(tool) == expected


def test_system_path_precedes_tools(local_tools, monkeypatch):
    executable(local_tools / "tools/ffmpeg/bin/ffmpeg")
    system = executable(local_tools / "system/ffmpeg")
    monkeypatch.setattr("footboy.environment.shutil.which", lambda _: system)
    assert resolve_binary("ffmpeg") == system


def test_explicit_path_never_falls_back(local_tools, monkeypatch):
    monkeypatch.setattr("footboy.environment.shutil.which", lambda _: "/system/ffmpeg")
    assert resolve_binary("./custom/ffmpeg") == str(local_tools / "custom/ffmpeg")
    with pytest.raises(RuntimeError, match="无法执行"):
        check_binary("./custom/ffmpeg")


def test_source_tools_work_outside_project(local_tools, monkeypatch):
    source = local_tools / "project"
    executable(source / "src/footboy/environment.py")
    monkeypatch.setattr("footboy.environment.__file__", str(source / "src/footboy/environment.py"))
    expected = executable(
        source / "tools/ffmpeg/bin" / ("ffmpeg.exe" if os.name == "nt" else "ffmpeg")
    )
    assert resolve_binary("ffmpeg") == expected


def test_missing_tool_diagnostic(local_tools):
    with pytest.raises(RuntimeError, match="tools"):
        check_binary("ffprobe")


def test_local_tools_reach_check_probe_mux_and_ocr(local_tools, monkeypatch):
    from footboy.cli import main
    from footboy.mux.ffmpeg import build_ffmpeg_command
    from footboy.probe.ocr import TesseractBackend
    from footboy.sources.media_probe import ffprobe_source
    from footboy.sources.models import Source

    suffix = ".exe" if os.name == "nt" else ""
    ffmpeg = executable(local_tools / "tools/ffmpeg/bin" / ("ffmpeg" + suffix))
    ffprobe = executable(local_tools / "tools/ffmpeg/bin" / ("ffprobe" + suffix))
    tesseract = executable(local_tools / "tools/tesseract" / ("tesseract" + suffix))
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        output = (
            "ffmpeg version 6.0"
            if command[-1] == "-version"
            else ('{"streams":[{"codec_type":"video","codec_name":"h264"}]}')
        )
        return SimpleNamespace(returncode=0, stdout=output)

    monkeypatch.setattr("footboy.environment.subprocess.run", run)
    assert main(["--check"]) == 0
    assert [call[0] for call in calls] == [ffmpeg, ffprobe]
    source = Source("https://example.com/live.m3u8")
    ffprobe_source(source)
    assert calls[-1][0] == ffprobe
    assert build_ffmpeg_command(source, source, 0, local_tools)[0] == ffmpeg
    module = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd="old-command"),
        get_tesseract_version=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    monkeypatch.setenv("OMP_THREAD_LIMIT", os.environ.get("OMP_THREAD_LIMIT", "1"))
    TesseractBackend()
    assert module.pytesseract.tesseract_cmd == tesseract


@pytest.mark.parametrize("configured", [None, "2"], ids=["default", "user-setting"])
def test_tesseract_children_inherit_thread_limit_without_overriding_user_settings(
    monkeypatch, configured
):
    from footboy.probe.ocr import TesseractBackend

    monkeypatch.setenv("OMP_THREAD_LIMIT", configured or "")
    if configured is None:
        monkeypatch.delenv("OMP_THREAD_LIMIT")
    inherited = []

    def child_environment(*args, **kwargs):
        value = subprocess.check_output(
            [sys.executable, "-c", "import os; print(os.environ.get('OMP_THREAD_LIMIT', 'unset'))"],
            text=True,
            timeout=5,
        ).strip()
        inherited.append(value)
        return value

    module = SimpleNamespace(
        pytesseract=SimpleNamespace(tesseract_cmd="tesseract"),
        get_tesseract_version=child_environment,
        image_to_string=child_environment,
    )
    monkeypatch.setitem(sys.modules, "pytesseract", module)
    backend = TesseractBackend()
    backend.read(np.zeros((2, 2), dtype=np.uint8))
    assert inherited == [configured or "1", configured or "1"]


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
