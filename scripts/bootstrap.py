"""Shared setup/launch implementation; only the standard library is needed initially."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path

if sys.version_info < (3, 10):  # noqa: UP036 - this script runs before package installation
    raise SystemExit("Footboy 需要 Python 3.10+ / Python 3.10+ is required.")

ROOT = Path(__file__).resolve().parents[1]
VENV = ROOT / ".venv"
PYTHON = VENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
STAMP = VENV / ".footboy-setup"

# These two project modules use only the standard library, even before installation.
sys.path.insert(0, str(ROOT / "src"))
from footboy.environment import check_binary, resolve_binary  # noqa: E402
from footboy.i18n import ArgumentParser, configure_cli_language, tr  # noqa: E402

DEPENDENCY_CHECK = """
import sys
from pathlib import Path
import av, cv2, numpy, pytesseract, yt_dlp, footboy
from playwright.sync_api import sync_playwright
if sys.version_info < (3, 10) or sys.prefix == sys.base_prefix:
    raise SystemExit(1)
if Path(footboy.__file__).resolve() != Path('src/footboy/__init__.py').resolve():
    raise SystemExit(1)
with sync_playwright() as playwright:
    if not Path(playwright.chromium.executable_path).is_file():
        raise SystemExit(1)
"""
BROWSER_CHECK = """
from playwright.sync_api import sync_playwright
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    browser.close()
"""


def run(command: list[str]) -> None:
    subprocess.run(command, cwd=ROOT, check=True)


def fingerprint() -> str:
    return hashlib.sha256(
        str(ROOT).encode()
        + (ROOT / "pyproject.toml").read_bytes()
        + (VENV / "pyvenv.cfg").read_bytes()
    ).hexdigest()


def environment_ready() -> bool:
    try:
        if STAMP.read_text(encoding="utf-8").strip() != fingerprint():
            return False
        result = subprocess.run(
            [str(PYTHON), "-c", DEPENDENCY_CHECK + BROWSER_CHECK],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        return result.returncode == 0
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return False


def install_environment(*, with_deps: bool = False) -> None:
    STAMP.unlink(missing_ok=True)
    if not PYTHON.is_file():
        print(tr("Creating the project virtual environment…"), flush=True)
        try:
            run([sys.executable, "-m", "venv", str(VENV)])
        except subprocess.CalledProcessError:
            print(
                tr("Could not create .venv. On Ubuntu/Debian, install python3-venv and retry."),
                file=sys.stderr,
            )
            raise
    try:
        run(
            [
                str(PYTHON),
                "-c",
                "import sys; sys.exit(sys.version_info < (3, 10) or sys.prefix == sys.base_prefix)",
            ]
        )
    except (OSError, subprocess.CalledProcessError):
        print(
            tr("The existing .venv is unusable. Move it aside and run setup again."),
            file=sys.stderr,
        )
        raise

    print(tr("Installing Footboy and Python dependencies…"), flush=True)
    run([str(PYTHON), "-m", "ensurepip", "--upgrade"])
    run([str(PYTHON), "-m", "pip", "install", "--upgrade", "pip"])
    run([str(PYTHON), "-m", "pip", "install", "-e", ".[tesseract]"])
    run([str(PYTHON), "-m", "pip", "check"])

    print(tr("Installing and checking Playwright Chromium…"), flush=True)
    command = [str(PYTHON), "-m", "playwright", "install"]
    if with_deps:
        command.append("--with-deps")
    run([*command, "chromium"])
    try:
        run([str(PYTHON), "-c", DEPENDENCY_CHECK])
        run([str(PYTHON), "-c", BROWSER_CHECK])
    except subprocess.CalledProcessError:
        print(
            tr("Chromium is not ready. On Linux, run ./setup.sh --with-deps and retry."),
            file=sys.stderr,
        )
        raise
    STAMP.write_text(fingerprint() + "\n", encoding="utf-8")


def check_media(ffmpeg: str, ffprobe: str) -> None:
    try:
        check_binary(ffmpeg, minimum_major=6)
        check_binary(ffprobe)
    except RuntimeError:
        print(
            tr("Install FFmpeg 6.0+ (including ffprobe), then rerun this script:"),
            file=sys.stderr,
        )
        if sys.platform == "darwin":
            hint = "brew install ffmpeg tesseract"
        elif os.name == "nt":
            hint = "winget install Gyan.FFmpeg\nwinget install UB-Mannheim.TesseractOCR"
        else:
            hint = "sudo apt update && sudo apt install ffmpeg tesseract-ocr tesseract-ocr-eng"
        print(hint, file=sys.stderr)
        print(
            tr("See README for other systems and portable tools/ installations."),
            file=sys.stderr,
        )
        raise


def check_tesseract(command: str | None) -> None:
    try:
        result = subprocess.run(
            [resolve_binary(command or "tesseract"), "--list-langs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode == 0 and "eng" in result.stdout.split():
            return
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(
        tr(
            "Tesseract or its eng language data is unavailable. Install it for Tesseract OCR; "
            "use --no-auto-measure for manual sync, or configure RapidOCR separately."
        ),
        file=sys.stderr,
    )


def launch(arguments: list[str]) -> int:
    command = [str(PYTHON), "-u", "-m", "footboy", *arguments]
    if os.name != "nt":
        # Replace the launcher so Ctrl+C/SIGTERM reach Footboy directly.
        os.execv(str(PYTHON), command)
    process = subprocess.Popen(command, cwd=ROOT)
    while True:
        try:
            return process.wait()
        except KeyboardInterrupt:
            # The child receives the same Windows console event and shuts down gracefully.
            continue


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"setup", "start"}:
        print("Usage: python scripts/bootstrap.py {setup|start} [options]", file=sys.stderr)
        return 2
    action, arguments = arguments[0], arguments[1:]
    configure_cli_language(arguments)
    parser = ArgumentParser(
        prog="setup.bat" if os.name == "nt" else "./setup.sh",
        add_help=action == "setup",
        allow_abbrev=False,
        description=tr("Prepare .venv, Python dependencies and Chromium for Footboy."),
    )
    parser.add_argument("--ffmpeg", default="ffmpeg", help=tr("FFmpeg executable path"))
    parser.add_argument("--ffprobe", default="ffprobe", help=tr("ffprobe executable path"))
    parser.add_argument("--tesseract-command", help=tr("Tesseract executable path"))
    if action == "setup":
        parser.add_argument(
            "--with-deps",
            action="store_true",
            help=tr("Also install Chromium system libraries (may require sudo on Linux)"),
        )
        options = parser.parse_args(arguments)
    else:
        options, _ = parser.parse_known_args(arguments)

    os.chdir(ROOT)
    try:
        help_requested = action == "start" and any(arg in {"-h", "--help"} for arg in arguments)
        if not help_requested:
            check_media(options.ffmpeg, options.ffprobe)
        if action == "setup" or not environment_ready():
            install_environment(with_deps=getattr(options, "with_deps", False))
            check_tesseract(options.tesseract_command)
            print(
                tr(
                    "Setup complete. Start Footboy with start.sh (macOS/Linux) or start.bat (Windows)."
                ),
                flush=True,
            )
        if action == "setup":
            return 0
        return launch(arguments)
    except subprocess.CalledProcessError as exc:
        print(
            tr(
                "Setup failed (exit code {0}); fix the error above and rerun setup.", exc.returncode
            ),
            file=sys.stderr,
        )
        return exc.returncode if exc.returncode > 0 else 1
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(tr("Setup/start failed: {0}", exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(tr("Setup interrupted; rerun the script to continue."), file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
