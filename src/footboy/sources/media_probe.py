from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .models import Source


class MediaProbeError(RuntimeError):
    pass


def ffprobe_source(
    source: Source,
    *,
    ffprobe: str | Path = "ffprobe",
    timeout: float = 15,
) -> Source:
    command = [
        str(ffprobe),
        "-v",
        "error",
        "-rw_timeout",
        "15000000",
        "-user_agent",
        source.user_agent,
    ]
    headers = source.ffmpeg_headers(include_cookies=False)
    if headers:
        command.extend(["-headers", headers])
    cookies = source.ffmpeg_cookies()
    if cookies:
        command.extend(["-cookies", cookies])
    if source.kind == "hls":
        command.extend(["-allowed_extensions", "ALL"])
    command.extend(
        [
            "-show_entries",
            "stream=index,codec_type,codec_name,width,height",
            "-of",
            "json",
            source.url,
        ]
    )
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            creationflags=flags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MediaProbeError(f"ffprobe 启动或探测失败: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip().splitlines()[-1:] or ["未知错误"]
        raise MediaProbeError(f"ffprobe 拒绝该线路: {detail[0]}")
    try:
        streams: list[dict[str, Any]] = json.loads(result.stdout).get("streams", [])
    except (ValueError, AttributeError) as exc:
        raise MediaProbeError("ffprobe 输出无法解析") from exc
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        raise MediaProbeError("候选线路不含视频")
    source.video_codec = str(video.get("codec_name") or "") or None
    source.width = int(video["width"]) if video.get("width") else None
    source.height = int(video["height"]) if video.get("height") else None
    source.audio_codec = (str(audio.get("codec_name") or "") or None) if audio else None
    source.has_audio = audio is not None
    return source
