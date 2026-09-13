from __future__ import annotations

import logging
import math
import re
import time
from pathlib import Path
from urllib.parse import unquote, urlsplit

logger = logging.getLogger(__name__)
_GENERATED_MEDIA = re.compile(r"(?:seg_\d+(?:_\d+)?\.(?:ts|m4s)|init(?:_\d+)?\.mp4)(?:\.tmp)?")


class HlsOutputCleaner:
    """Retire unreferenced outputs across FFmpeg restarts without racing players."""

    def __init__(self, directory: Path, *, grace_period: float = 60.0) -> None:
        self.directory = directory
        self._retention = grace_period
        self._unreferenced: dict[str, tuple[float, int, int]] = {}

    def collect(self, generation: int | None, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        try:
            references, duration = self._playlist()
            paths = list(self.directory.iterdir())
        except (OSError, UnicodeError, ValueError):
            # An unreadable/replaced playlist is not evidence that its files
            # are unused. Restart the grace period after a valid read.
            self._unreferenced.clear()
            return
        # Keep enough time for a player holding the previous playlist, even
        # when the new source has much shorter segments.
        self._retention = max(self._retention, duration)
        waiting = {}
        failures = 0
        for path in paths:
            name = path.name
            if not _GENERATED_MEDIA.fullmatch(name) or name in references:
                continue
            if generation is not None and (
                name.startswith(f"seg_{generation}_")
                or name in {f"init_{generation}.mp4", f"init_{generation}.mp4.tmp"}
            ):
                continue
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                stat = path.stat()
                previous = self._unreferenced.get(name)
                since = (
                    previous[0]
                    if previous is not None and previous[1:] == (stat.st_mtime_ns, stat.st_size)
                    else now
                )
                if now - since >= self._retention:
                    path.unlink()
                else:
                    waiting[name] = (since, stat.st_mtime_ns, stat.st_size)
            except FileNotFoundError:
                continue  # FFmpeg or a fresh session already removed this file.
            except OSError:
                failures += 1
                if name in self._unreferenced:
                    waiting[name] = self._unreferenced[name]
        self._unreferenced = waiting
        if failures:
            logger.warning("无法回收 %s 个过期 HLS 文件，将稍后重试", failures)

    def _playlist(self) -> tuple[set[str], float]:
        lines = (self.directory / "live.m3u8").read_text(encoding="utf-8").splitlines()
        if not lines or lines[0].strip() != "#EXTM3U":
            raise ValueError("Invalid HLS playlist")
        references: set[str] = set()
        duration = 0.0
        for raw in lines[1:]:
            line = raw.strip()
            if line.startswith("#EXTINF:"):
                seconds = float(line.partition(":")[2].partition(",")[0])
                if not math.isfinite(seconds) or seconds < 0:
                    raise ValueError("Invalid segment duration")
                duration += seconds
            elif line.startswith("#EXT-X-MAP:"):
                match = re.search(r'URI="([^"]+)"', line)
                if match is None:
                    raise ValueError("Invalid HLS initialization reference")
                references.add(Path(unquote(urlsplit(match[1]).path)).name)
            elif line and not line.startswith("#"):
                references.add(Path(unquote(urlsplit(line).path)).name)
        if not math.isfinite(duration):
            raise ValueError("Invalid playlist duration")
        return references, duration
