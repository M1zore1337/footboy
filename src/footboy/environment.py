from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
from pathlib import Path


def resolve_binary(name: str | Path) -> str:
    """Resolve explicit paths, then PATH, then local tool installations."""
    command = str(name)
    if Path(command).is_absolute() or "/" in command or "\\" in command:
        return str(Path(command).expanduser().resolve())
    resolved = shutil.which(command)
    if resolved:
        return str(Path(resolved).resolve())
    tool = command.removesuffix(".exe")
    if tool not in {"ffmpeg", "ffprobe", "tesseract"}:
        return command
    roots = [Path.cwd()]
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "src" / "footboy" / "environment.py").is_file():
        roots.append(source_root)
    filename = tool + (".exe" if os.name == "nt" else "")
    package = "ffmpeg" if tool in {"ffmpeg", "ffprobe"} else "tesseract"
    for root in dict.fromkeys(roots):
        for directory in (root / "tools" / package / "bin", root / "tools" / package):
            candidate = directory / filename
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate.resolve())
    return command


def check_binary(name: str, minimum_major: int | None = None) -> None:
    resolved = resolve_binary(name)
    if resolved == name and not Path(resolved).is_file() and not shutil.which(resolved):
        raise RuntimeError(f"找不到 {name}；请检查自定义路径、PATH 或项目 tools 目录")
    try:
        result = subprocess.run(
            [str(resolved), "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"无法执行 {name}: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(f"{name} -version 返回 {result.returncode}")
    if minimum_major is not None:
        match = re.search(r"ffmpeg version\s+(?:n)?(\d+)", result.stdout, re.IGNORECASE)
        supported = bool(match and int(match.group(1)) >= minimum_major)
        if not match and minimum_major == 6 and re.search(r"ffmpeg version N-\d+", result.stdout):
            # Git builds have no release number. FFmpeg 6 requires libavformat
            # 60; check the loaded library (after '/'), not just the build headers.
            library = re.search(
                r"^libavformat\s+\d+\.\s*\d+\.\s*\d+\s*/\s*(\d+)\.",
                result.stdout,
                re.MULTILINE,
            )
            supported = bool(library and int(library.group(1)) >= 60)
        if not supported:
            version = match.group(1) if match else "无法识别"
            raise RuntimeError(f"需要 FFmpeg {minimum_major}.0+，当前主版本 {version}")


def binary_crash_reason(returncode: int | None) -> str | None:
    """Distinguish native crashes from ordinary input/network failures."""
    if returncode is None:
        return None
    if returncode < 0:
        try:
            name = signal.Signals(-returncode).name
        except ValueError:
            name = ""
        if name in {"SIGSEGV", "SIGBUS", "SIGABRT", "SIGILL", "SIGFPE", "SIGSYS"}:
            return name
    return {
        0xC0000005: "访问冲突 (0xC0000005)",
        0xC000001D: "非法指令 (0xC000001D)",
        0xC0000374: "堆损坏 (0xC0000374)",
        0xC0000409: "安全检查失败 (0xC0000409)",
    }.get(returncode & 0xFFFFFFFF)
