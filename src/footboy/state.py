from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any

from footboy.i18n import tr

logger = logging.getLogger(__name__)


class StateStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._data: dict[str, Any] = {"version": 1, "sources": {}, "offsets": {}}
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                return
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning(
                    tr(
                        "Cannot load state file %s (%s); using defaults without modifying the original file"
                    ),
                    self.path,
                    type(exc).__name__,
                )
                return
            if not isinstance(loaded, dict):
                logger.warning(tr("State file %s is not a JSON object; using defaults"), self.path)
                return
            for key in ("sources", "offsets"):
                if isinstance(loaded.get(key), dict):
                    self._data[key] = loaded[key]
                elif key in loaded:
                    logger.warning(
                        tr("State file %s has an invalid %s field; using defaults"), self.path, key
                    )

    def source_probe(self, key: str) -> dict[str, Any] | None:
        with self._lock:
            value = self._data["sources"].get(key)
            return dict(value) if isinstance(value, dict) else None

    def set_source_probe(self, key: str, value: dict[str, Any]) -> None:
        with self._lock:
            self._data["sources"][key] = dict(value)
            self._save()

    def offset(self, key: str) -> float | None:
        with self._lock:
            value = self._data["offsets"].get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value) if math.isfinite(value) else None
            return None

    def set_offset(self, key: str, value: float) -> None:
        if not math.isfinite(value):
            raise ValueError(tr("Offset must be a finite number"))
        with self._lock:
            self._data["offsets"][key] = float(value)
            self._save()

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(self._data, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
