from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from footboy.diagnostics import redact_diagnostic
from footboy.environment import binary_crash_reason, resolve_binary
from footboy.i18n import tr

from .models import DIRECT_HTTP_PROXY, Source


class MediaProbeError(RuntimeError):
    pass


def ffprobe_source(
    source: Source,
    *,
    ffprobe: str | Path = "ffprobe",
    timeout: float = 15,
) -> Source:
    command = [
        resolve_binary(ffprobe),
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
    if source.no_proxy:
        command.extend(["-http_proxy", DIRECT_HTTP_PROXY])
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
    except subprocess.TimeoutExpired:
        # TimeoutExpired includes the complete argv, including cookies and
        # authorization headers. Suppress that exception's traceback as well.
        raise MediaProbeError(tr("ffprobe timed out after {0:g} seconds", timeout)) from None
    except OSError as exc:
        raise MediaProbeError(
            tr("Cannot start ffprobe: {0}", exc.strerror or type(exc).__name__)
        ) from None
    if result.returncode != 0:
        crash = binary_crash_reason(result.returncode)
        if crash:
            raise MediaProbeError(
                tr("ffprobe crashed ({0}); check or replace the FFmpeg/ffprobe build", crash)
            )
        detail = result.stderr.strip().splitlines()[-1:] or [tr("Unknown error")]
        raise MediaProbeError(tr("ffprobe rejected this stream: {0}", redact_diagnostic(detail[0])))
    try:
        streams: list[dict[str, Any]] = json.loads(result.stdout).get("streams", [])
    except (ValueError, AttributeError) as exc:
        raise MediaProbeError(tr("Cannot parse ffprobe output")) from exc
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if not video:
        raise MediaProbeError(tr("The candidate stream has no video"))
    source.video_codec = str(video.get("codec_name") or "") or None
    source.width = int(video["width"]) if video.get("width") else None
    source.height = int(video["height"]) if video.get("height") else None
    source.audio_codec = (str(audio.get("codec_name") or "") or None) if audio else None
    source.has_audio = audio is not None
    return source
