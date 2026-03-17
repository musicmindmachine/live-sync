from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LiveSyncRemoteScript.convex_client import ConvexRealtimeClient
from LiveSyncRemoteScript.debug_log import log_debug
from LiveSyncRemoteScript.sidecar_watch_state import WatchStateStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveSync Convex room watch process.")
    parser.add_argument("--deployment-url", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--watch-state-path", required=True)
    parser.add_argument("--log-path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    def log(message: str) -> None:
        prefix = "LiveSyncWatch[%s]: %s" % (args.client_id, message)
        log_debug(prefix)
        if args.log_path:
            try:
                log_path = Path(args.log_path).expanduser()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(prefix + "\n")
            except Exception:
                pass

    store = WatchStateStore(Path(args.watch_state_path).expanduser())
    event_counter, _ = store.read()
    log("Starting watch process for room %s at counter %s." % (args.room_id, event_counter))
    client = ConvexRealtimeClient(args.deployment_url, logger=log)

    def on_version(version) -> None:
        nonlocal event_counter
        event_counter += 1
        store.write(event_counter, version)
        log(
            "Received room version %s at counter %s."
            % (int(version.get("latestSequence", 0)), event_counter)
        )

    try:
        client.start_room_watch(args.room_id, on_version)
        log("Convex room watch started.")
        while True:
            time.sleep(3600.0)
    except KeyboardInterrupt:
        log("Interrupted, stopping watch.")
    finally:
        try:
            client.stop()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
