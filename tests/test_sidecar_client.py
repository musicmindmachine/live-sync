from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path

from ableton.LiveSyncRemoteScript.models import Operation
from ableton.LiveSyncRemoteScript.sidecar_client import LocalSidecarClient


class FakeManager:
    def __init__(self) -> None:
        self.log_path = Path("/tmp/live-sync-sidecar-test.log")
        self._watch_responses = [
            {
                "updated": True,
                "eventCounter": 1,
                "version": {
                    "latestSequence": 7,
                    "compactedThroughSequence": 3,
                    "maxLamport": 11,
                    "mediaVersion": 2,
                    "updatedAt": 99,
                },
            }
        ]

    def post_json(self, path, payload, timeout=30.0):
        if path == "/push_ops":
            return {
                "roomId": payload["roomId"],
                "accepted": [
                    {
                        "opId": payload["ops"][0]["opId"],
                        "sequence": 5,
                        "duplicate": False,
                        "applied": True,
                    }
                ],
                "lastSequence": 5,
                "snapshotState": {"song": {"tempo": 128.0}},
                "clockState": {"/song/tempo": {"lamport": 4, "client_id": "left", "op_id": "abc"}},
                "maxLamport": 4,
            }
        if path == "/pull_ops":
            return {
                "roomExists": True,
                "latestSequence": 5,
                "compactedThroughSequence": 2,
                "resetRequired": False,
                "snapshotState": None,
                "clockState": None,
                "snapshotSequence": 5,
                "maxLamport": 4,
                "ops": [
                    {
                        "opId": "remote-op",
                        "clientId": "right",
                        "lamport": 4,
                        "kind": "set",
                        "path": "/song/tempo",
                        "valueJson": "128.0",
                        "sequence": 5,
                    }
                ],
            }
        if path == "/watch_room_version":
            if self._watch_responses:
                return self._watch_responses.pop(0)
            time.sleep(0.05)
            return {
                "updated": False,
                "eventCounter": 1,
                "version": {
                    "latestSequence": 7,
                    "compactedThroughSequence": 3,
                    "maxLamport": 11,
                    "mediaVersion": 2,
                    "updatedAt": 99,
                },
            }
        raise AssertionError("Unexpected path %s" % path)

    def stop(self) -> None:
        return


class LocalSidecarClientTests(unittest.TestCase):
    def test_push_and_pull_deserialize_transport_payloads(self) -> None:
        client = LocalSidecarClient(
            deployment_url="https://example.convex.cloud",
            room_id="jam",
            client_id="left",
            script_directory=Path("/tmp"),
            manager=FakeManager(),
        )
        operation = Operation(
            op_id="abc",
            client_id="left",
            lamport=4,
            kind="set",
            path="/song/tempo",
            value=128.0,
        )

        push_result = client.push_ops("jam", "left", [operation])
        pull_result = client.pull_ops("jam", 4, 20)

        self.assertEqual(push_result.last_sequence, 5)
        self.assertEqual(push_result.snapshot_state["song"]["tempo"], 128.0)
        self.assertEqual(len(pull_result.ops), 1)
        self.assertEqual(pull_result.ops[0].path, "/song/tempo")
        self.assertEqual(pull_result.ops[0].value, 128.0)

    def test_watch_thread_forwards_sidecar_events(self) -> None:
        client = LocalSidecarClient(
            deployment_url="https://example.convex.cloud",
            room_id="jam",
            client_id="left",
            script_directory=Path("/tmp"),
            manager=FakeManager(),
        )
        event = threading.Event()
        seen = {}

        def on_version(payload) -> None:
            seen.update(payload)
            event.set()

        client.start_room_watch("jam", on_version)
        self.assertTrue(event.wait(timeout=1.0))
        client.stop()

        self.assertEqual(seen["latestSequence"], 7)
        self.assertEqual(seen["mediaVersion"], 2)


if __name__ == "__main__":
    unittest.main()
