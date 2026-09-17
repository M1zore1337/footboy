from __future__ import annotations

import importlib.util
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from footboy import i18n

REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    previous_language = i18n.get_language()
    monkeypatch.setattr(sys, "path", list(sys.path))
    spec = importlib.util.spec_from_file_location(
        "footboy_bootstrap", REPOSITORY / "scripts/bootstrap.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = tmp_path / "Footboy with spaces"
    venv = root / ".venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir(parents=True)
    python.touch()
    (venv / "pyvenv.cfg").write_text("version = 3.10\n", encoding="utf-8")
    (root / "pyproject.toml").write_text("[project]\nname = 'footboy'\n", encoding="utf-8")
    (root / "state.json").write_text('{"saved": true}\n', encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "VENV", venv)
    monkeypatch.setattr(module, "PYTHON", python)
    monkeypatch.setattr(module, "STAMP", venv / ".footboy-setup")
    monkeypatch.chdir(tmp_path)
    yield module
    i18n.set_language(previous_language)


def test_first_start_installs_into_project_and_preserves_saved_state(bootstrap, monkeypatch):
    commands = []
    monkeypatch.setattr(bootstrap, "run", commands.append)
    monkeypatch.setattr(bootstrap, "check_media", lambda *args: None)
    monkeypatch.setattr(bootstrap, "check_tesseract", lambda *args: None)

    def launch(arguments):
        assert bootstrap.STAMP.read_text().strip() == bootstrap.fingerprint()
        assert Path.cwd() == bootstrap.ROOT
        assert arguments == ["--port", "8090"]
        return 0

    monkeypatch.setattr(bootstrap, "launch", launch)
    assert bootstrap.main(["start", "--port", "8090"]) == 0
    assert commands and all(command[0] == str(bootstrap.PYTHON) for command in commands)
    assert json.loads((bootstrap.ROOT / "state.json").read_text()) == {"saved": True}


def test_ready_start_passes_arguments_and_exit_code_without_installing(bootstrap, monkeypatch):
    monkeypatch.setattr(bootstrap, "environment_ready", lambda: True)
    monkeypatch.setattr(bootstrap, "run", lambda _: pytest.fail("Ready launches must not install"))
    binaries = []
    monkeypatch.setattr(bootstrap, "check_media", lambda *args: binaries.append(args))
    arguments = [
        "--ffmpeg",
        "tools/custom ffmpeg",
        "--ffprobe",
        "tools/custom ffprobe",
        "--video-line",
        "高清直播 ⑤",
        "--port",
        "8091",
    ]

    def launch(received):
        assert received == arguments
        assert Path.cwd() == bootstrap.ROOT
        return 23

    monkeypatch.setattr(bootstrap, "launch", launch)
    assert bootstrap.main(["start", *arguments]) == 23
    assert binaries == [("tools/custom ffmpeg", "tools/custom ffprobe")]


def test_failed_install_invalidates_stamp_and_can_be_retried(bootstrap, monkeypatch):
    bootstrap.STAMP.write_text(bootstrap.fingerprint())
    monkeypatch.setattr(bootstrap, "check_media", lambda *args: None)
    monkeypatch.setattr(bootstrap, "check_tesseract", lambda *args: None)
    monkeypatch.setattr(bootstrap, "launch", lambda _: pytest.fail("Setup must not launch"))

    def interrupted_download(command):
        if "playwright" in command:
            raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(bootstrap, "run", interrupted_download)
    assert bootstrap.main(["setup"]) == 7
    assert not bootstrap.STAMP.exists()
    monkeypatch.setattr(bootstrap, "run", lambda _: None)
    assert bootstrap.main(["setup"]) == 0
    assert bootstrap.STAMP.read_text().strip() == bootstrap.fingerprint()
    assert json.loads((bootstrap.ROOT / "state.json").read_text()) == {"saved": True}


def test_browser_launch_failure_does_not_mark_setup_complete(bootstrap, monkeypatch):
    def missing_library(command):
        if bootstrap.BROWSER_CHECK in command:
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(bootstrap, "run", missing_library)
    with pytest.raises(subprocess.CalledProcessError):
        bootstrap.install_environment()
    assert not bootstrap.STAMP.exists()


@pytest.mark.parametrize(
    "failure", [None, "packages", "timeout", "metadata", "venv", "stamp", "corrupt_stamp"]
)
def test_environment_reuse_requires_unchanged_metadata_and_working_dependencies(
    bootstrap, monkeypatch, failure
):
    bootstrap.STAMP.write_text(bootstrap.fingerprint())
    if failure == "metadata":
        (bootstrap.ROOT / "pyproject.toml").write_text("[project]\nname = 'changed'\n")
    elif failure == "venv":
        (bootstrap.VENV / "pyvenv.cfg").unlink()
    elif failure == "stamp":
        bootstrap.STAMP.unlink()
    elif failure == "corrupt_stamp":
        bootstrap.STAMP.write_bytes(b"\xff\xfe")

    def probe(command, **kwargs):
        assert kwargs["cwd"] == bootstrap.ROOT
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 30)
        return subprocess.CompletedProcess(command, 1 if failure == "packages" else 0)

    monkeypatch.setattr(bootstrap.subprocess, "run", probe)
    assert bootstrap.environment_ready() is (failure is None)


def test_missing_media_prerequisites_fail_before_any_downloads(bootstrap, monkeypatch):
    def missing(*args):
        raise RuntimeError("missing ffmpeg")

    monkeypatch.setattr(bootstrap, "check_media", missing)
    monkeypatch.setattr(bootstrap, "run", lambda _: pytest.fail("Must check prerequisites first"))
    monkeypatch.setattr(bootstrap, "launch", lambda _: pytest.fail("Must not launch"))
    assert bootstrap.main(["start"]) == 1
    assert not bootstrap.STAMP.exists()


def test_setup_help_works_without_initialization(bootstrap, monkeypatch, capsys):
    monkeypatch.setattr(bootstrap, "run", lambda _: pytest.fail("Help must not install"))
    with pytest.raises(SystemExit) as error:
        bootstrap.main(["setup", "--help", "--lang", "en"])
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "Prepare .venv" in output and "--with-deps" in output
    assert not bootstrap.STAMP.exists()


def test_launched_service_flushes_output_before_exit_and_preserves_status(tmp_path):
    (tmp_path / "footboy.py").write_text(
        "import sys\nprint('READY')\nsys.stdin.read(1)\nraise SystemExit(7)\n"
    )
    helper = (
        "import importlib.util, sys\nfrom pathlib import Path\nimport os\n"
        f"spec = importlib.util.spec_from_file_location('bootstrap', {str(REPOSITORY / 'scripts/bootstrap.py')!r})\n"
        "bootstrap = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(bootstrap)\n"
        "bootstrap.PYTHON = Path(sys.executable)\n"
        f"bootstrap.ROOT = Path({str(tmp_path)!r})\nos.chdir(bootstrap.ROOT)\n"
        "raise SystemExit(bootstrap.launch([]))\n"
    )
    process = subprocess.Popen(
        [sys.executable, "-c", helper],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"},
    )
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(process.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=10) == "READY\n"
        _, error = process.communicate(input="\n", timeout=10)
        assert process.returncode == 7, error
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        reader.join(timeout=10)


@pytest.mark.skipif(os.name == "nt", reason="POSIX shell wrappers")
@pytest.mark.parametrize("action", ["setup", "start"])
def test_shell_entrypoints_preserve_spaces_arguments_process_and_exit_status(tmp_path, action):
    root = tmp_path / "项目 with spaces"
    (root / "scripts").mkdir(parents=True)
    entrypoint = root / f"{action}.sh"
    shutil.copy2(REPOSITORY / entrypoint.name, entrypoint)
    (root / "scripts/bootstrap.py").write_text(
        "import json, os, sys\n"
        "print(json.dumps({'argv': sys.argv, 'pid': os.getpid()}))\n"
        "raise SystemExit(19)\n",
        encoding="utf-8",
    )
    arguments = ["--video-line", "高清直播 ⑤", "--video-page", "https://example.test/?a=1&b=two"]
    process = subprocess.Popen(
        [str(entrypoint), *arguments],
        cwd=tmp_path,
        env={**os.environ, "FOOTBOY_PYTHON": sys.executable},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    output, error = process.communicate(timeout=10)
    assert process.returncode == 19, error
    payload = json.loads(output)
    assert payload["argv"] == [str(root / "scripts/bootstrap.py"), action, *arguments]
    assert payload["pid"] == process.pid
