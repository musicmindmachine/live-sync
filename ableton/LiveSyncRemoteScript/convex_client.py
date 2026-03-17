from __future__ import annotations

import json
import threading
import time
from typing import Any, Callable, Dict, Iterable, Optional

from .models import AcceptedOperation, Operation, PullResult, PushResult


class ConvexRealtimeClient:
    def __init__(
        self,
        deployment_url: str,
        logger: Optional[Callable[[str], None]] = None,
        auth_token: Optional[str] = None,
    ) -> None:
        try:
            from convex import ConvexClient
        except ImportError as error:
            raise RuntimeError(
                "The Convex Python client is not installed in the sidecar runtime: %s" % error
            ) from error

        self._deployment_url = deployment_url.rstrip("/")
        self._logger = logger or (lambda message: None)
        self._client = ConvexClient(self._deployment_url)
        if auth_token:
            self._client.set_auth(auth_token)

        self._watch_stop = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None
        self._watch_subscription = None
        self._watch_lock = threading.Lock()

    def push_ops(self, room_id: str, client_id: str, ops: Iterable[Operation]) -> PushResult:
        payload = {
            "roomId": room_id,
            "clientId": client_id,
            "ops": [operation.to_payload() for operation in ops],
        }
        response = self._client.mutation("sync:pushOps", payload)
        snapshot_state = json.loads(response["snapshotJson"])
        clock_state = self._parse_clock_state(response.get("clockJson"))
        return PushResult(
            room_id=response["roomId"],
            accepted=[
                AcceptedOperation(
                    op_id=item["opId"],
                    sequence=int(item["sequence"]) if item.get("sequence") is not None else None,
                    duplicate=bool(item["duplicate"]),
                    applied=bool(item.get("applied", False)),
                )
                for item in response["accepted"]
            ],
            last_sequence=int(response["lastSequence"]),
            snapshot_state=snapshot_state,
            clock_state=clock_state,
            max_lamport=int(response.get("maxLamport", 0)),
        )

    def pull_ops(self, room_id: str, after_sequence: int, limit: int = 200) -> PullResult:
        payload = {
            "roomId": room_id,
            "afterSequence": after_sequence,
            "limit": limit,
        }
        response = self._client.query("sync:pullOps", payload)
        snapshot_state = None
        if response.get("snapshotJson") is not None:
            snapshot_state = json.loads(response["snapshotJson"])
        clock_state = self._parse_clock_state(response.get("clockJson"))
        return PullResult(
            room_exists=bool(response["roomExists"]),
            latest_sequence=int(response["latestSequence"]),
            compacted_through_sequence=int(response.get("compactedThroughSequence", 0)),
            reset_required=bool(response.get("resetRequired", False)),
            snapshot_state=snapshot_state,
            clock_state=clock_state,
            snapshot_sequence=int(response.get("snapshotSequence", response["latestSequence"])),
            max_lamport=int(response.get("maxLamport", 0)),
            ops=[Operation.from_payload(item) for item in response["ops"]],
        )

    def start_room_watch(self, room_id: str, on_version: Callable[[Dict[str, int]], None]) -> None:
        self._log("Preparing room watch for %s." % room_id)
        self.stop()
        self._log("Cleared previous room watch for %s." % room_id)
        self._watch_stop.clear()

        def watch_loop() -> None:
            self._log("Room watch thread entered for %s." % room_id)
            while not self._watch_stop.is_set():
                subscription = None
                try:
                    self._log("Subscribing to room watch for %s." % room_id)
                    subscription = self._client.subscribe("sync:watchRoomVersion", {"roomId": room_id})
                    self._log("Subscribed to room watch for %s." % room_id)
                    with self._watch_lock:
                        self._watch_subscription = subscription

                    for result in subscription:
                        if self._watch_stop.is_set():
                            break
                        on_version(
                            {
                                "latestSequence": int(result.get("latestSequence", 0)),
                                "compactedThroughSequence": int(result.get("compactedThroughSequence", 0)),
                                "maxLamport": int(result.get("maxLamport", 0)),
                                "mediaVersion": int(result.get("mediaVersion", 0)),
                                "updatedAt": int(result.get("updatedAt", 0)),
                            }
                        )
                except Exception as error:
                    if self._watch_stop.is_set():
                        break
                    self._log("Room watch failed, retrying: %s" % error)
                    time.sleep(1.0)
                finally:
                    if subscription is not None:
                        try:
                            subscription.unsubscribe()
                        except Exception:
                            pass
                    with self._watch_lock:
                        self._watch_subscription = None

        self._watch_thread = threading.Thread(
            target=watch_loop,
            name="LiveSyncConvexWatch",
            daemon=True,
        )
        self._log("Starting room watch thread for %s." % room_id)
        self._watch_thread.start()
        self._log("Started room watch thread for %s." % room_id)

    def stop(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        with self._watch_lock:
            subscription = self._watch_subscription
        if subscription is not None:
            try:
                subscription.unsubscribe()
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._watch_thread = None

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)

    def _parse_clock_state(self, clock_json: Optional[str]) -> Optional[Dict[str, Dict[str, Any]]]:
        if clock_json is None:
            return None
        raw_clock_state = json.loads(clock_json)
        return {
            path: {
                "lamport": int(entry.get("lamport", 0)),
                "client_id": str(entry.get("clientId", "")),
                "op_id": str(entry.get("opId", "")),
                "kind": str(entry.get("kind", "set")),
            }
            for path, entry in raw_clock_state.items()
        }
