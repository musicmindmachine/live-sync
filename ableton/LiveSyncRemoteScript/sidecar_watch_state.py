from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Tuple


DEFAULT_VERSION = {
    "latestSequence": 0,
    "compactedThroughSequence": 0,
    "maxLamport": 0,
    "mediaVersion": 0,
    "updatedAt": 0,
}


class WatchStateStore:
    def __init__(self, path: Path) -> None:
        self._path = Path(path)

    @property
    def path(self) -> Path:
        return self._path

    def ensure_exists(self) -> None:
        if self._path.exists():
            return
        self.write(0, DEFAULT_VERSION)

    def read(self) -> Tuple[int, Dict[str, Any]]:
        self.ensure_exists()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except Exception:
            self.write(0, DEFAULT_VERSION)
            return 0, dict(DEFAULT_VERSION)
        event_counter = int(payload.get("eventCounter", 0))
        raw_version = payload.get("version", {})
        version = self._normalize_version(raw_version)
        return event_counter, version

    def write(self, event_counter: int, version: Dict[str, Any]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "eventCounter": int(event_counter),
            "version": self._normalize_version(version),
        }
        temp_path = self._path.with_suffix(self._path.suffix + ".tmp")
        temp_path.write_text(
            json.dumps(payload, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )
        os.replace(str(temp_path), str(self._path))

    def _normalize_version(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        version = dict(DEFAULT_VERSION)
        version.update(payload or {})
        return {
            "latestSequence": int(version.get("latestSequence", 0)),
            "compactedThroughSequence": int(version.get("compactedThroughSequence", 0)),
            "maxLamport": int(version.get("maxLamport", 0)),
            "mediaVersion": int(version.get("mediaVersion", 0)),
            "updatedAt": int(version.get("updatedAt", 0)),
        }
