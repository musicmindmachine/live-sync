from __future__ import annotations

from datetime import datetime
from pathlib import Path


def log_debug(message: str) -> None:
    try:
        log_path = Path.home() / "Library" / "Preferences" / "Ableton" / "LiveSyncRemoteScript.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write("%s %s\n" % (timestamp, message))
    except Exception:
        pass
