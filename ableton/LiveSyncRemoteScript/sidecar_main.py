from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from LiveSyncRemoteScript.convex_client import ConvexRealtimeClient
from LiveSyncRemoteScript.debug_log import log_debug
from LiveSyncRemoteScript.sidecar_protocol import (
    deserialize_operation,
    serialize_pull_result,
    serialize_push_result,
    serialize_watch_payload,
)
from LiveSyncRemoteScript.sidecar_watch_state import WatchStateStore


class SidecarState:
    def __init__(
        self,
        deployment_url: str,
        room_id: str,
        client_id: str,
        token: str,
        watch_state_path: Path,
        logger,
    ) -> None:
        self.room_id = room_id
        self.client_id = client_id
        self.token = token
        self._logger = logger
        self._logger("Constructing ConvexRealtimeClient.")
        self._backend = ConvexRealtimeClient(deployment_url, logger=logger)
        self._logger("Constructed ConvexRealtimeClient.")
        self._watch_state = WatchStateStore(watch_state_path)
        self._watch_state.ensure_exists()

    def shutdown(self) -> None:
        self._backend.stop()

    def push_ops(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        room_id = str(payload.get("roomId", ""))
        client_id = str(payload.get("clientId", self.client_id))
        self._validate_room(room_id)
        operations = [deserialize_operation(item) for item in payload.get("ops", [])]
        return serialize_push_result(self._backend.push_ops(room_id, client_id, operations))

    def pull_ops(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        room_id = str(payload.get("roomId", ""))
        after_sequence = int(payload.get("afterSequence", 0))
        limit = int(payload.get("limit", 200))
        self._validate_room(room_id)
        return serialize_pull_result(self._backend.pull_ops(room_id, after_sequence, limit))

    def watch_room_version(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        room_id = str(payload.get("roomId", ""))
        since_counter = int(payload.get("sinceCounter", 0))
        timeout_seconds = max(0.0, float(payload.get("timeoutSeconds", 30.0)))
        self._validate_room(room_id)
        deadline = time.time() + timeout_seconds
        while True:
            event_counter, version = self._watch_state.read()
            if event_counter > since_counter:
                return serialize_watch_payload(event_counter, True, version)
            remaining = deadline - time.time()
            if remaining <= 0:
                return serialize_watch_payload(event_counter, False, version)
            time.sleep(min(0.05, remaining))

    def health_payload(self) -> Dict[str, Any]:
        event_counter, version = self._watch_state.read()
        return {
            "ok": True,
            "pid": os.getpid(),
            "roomId": self.room_id,
            "clientId": self.client_id,
            "eventCounter": event_counter,
            "version": version,
            "watchStatePath": str(self._watch_state.path),
        }

    def _validate_room(self, room_id: str) -> None:
        if room_id != self.room_id:
            raise ValueError("Unexpected room %s, expected %s" % (room_id, self.room_id))


class SidecarRequestHandler(BaseHTTPRequestHandler):
    server_version = "LiveSyncSidecar/0.1"

    def do_GET(self) -> None:
        try:
            if not self._check_auth():
                return
            if self.path != "/health":
                self._write_json(404, {"error": "not_found"})
                return
            self._write_json(200, self.server.state.health_payload())
        except Exception as error:
            self.server.logger("GET request failed for %s: %s" % (self.path, error))
            self._write_json(500, {"error": str(error)})

    def do_POST(self) -> None:
        if not self._check_auth():
            return

        try:
            payload = self._read_json()
            if self.path == "/push_ops":
                self._write_json(200, self.server.state.push_ops(payload))
                return
            if self.path == "/pull_ops":
                self._write_json(200, self.server.state.pull_ops(payload))
                return
            if self.path == "/watch_room_version":
                self._write_json(200, self.server.state.watch_room_version(payload))
                return
            if self.path == "/shutdown":
                self._write_json(200, {"ok": True})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            self._write_json(404, {"error": "not_found"})
        except Exception as error:
            self.server.logger("Request failed for %s: %s" % (self.path, error))
            self._write_json(500, {"error": str(error)})

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _check_auth(self) -> bool:
        token = self.headers.get("X-Live-Sync-Token", "")
        if token != self.server.state.token:
            self._write_json(403, {"error": "forbidden"})
            return False
        return True

    def _read_json(self) -> Dict[str, Any]:
        content_length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(content_length) if content_length > 0 else b"{}"
        if not raw:
            return {}
        return json.loads(raw.decode("utf-8"))

    def _write_json(self, status: int, payload: Dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class LiveSyncSidecarServer(ThreadingHTTPServer):
    def __init__(self, server_address, handler_class, state: SidecarState, logger) -> None:
        super(LiveSyncSidecarServer, self).__init__(server_address, handler_class)
        self.state = state
        self.logger = logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LiveSync local sidecar process.")
    parser.add_argument("--deployment-url", required=True)
    parser.add_argument("--room-id", required=True)
    parser.add_argument("--client-id", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--watch-state-path", required=True)
    parser.add_argument("--log-path")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    def log(message: str) -> None:
        prefix = "LiveSyncSidecar[%s]: %s" % (args.client_id, message)
        log_debug(prefix)
        if args.log_path:
            try:
                log_path = Path(args.log_path).expanduser()
                log_path.parent.mkdir(parents=True, exist_ok=True)
                with log_path.open("a", encoding="utf-8") as handle:
                    handle.write(prefix + "\n")
            except Exception:
                pass

    log("Starting sidecar on port %s for room %s." % (args.port, args.room_id))
    log("Initializing sidecar state.")
    state = SidecarState(
        deployment_url=args.deployment_url,
        room_id=args.room_id,
        client_id=args.client_id,
        token=args.token,
        watch_state_path=Path(args.watch_state_path).expanduser(),
        logger=log,
    )
    log("Creating HTTP server.")
    server = LiveSyncSidecarServer(("127.0.0.1", args.port), SidecarRequestHandler, state, log)
    log("HTTP server created.")
    try:
        log("Entering serve_forever.")
        server.serve_forever()
    finally:
        state.shutdown()
        server.server_close()
        log("Stopped sidecar.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
