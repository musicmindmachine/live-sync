from __future__ import annotations

import unittest

from ableton.LiveSyncRemoteScript.models import Operation
from ableton.LiveSyncRemoteScript.sync_core import apply_lww_operation, apply_operation_to_state, diff_states


class SyncCoreTests(unittest.TestCase):
    def test_diff_round_trip_for_scalar_changes(self) -> None:
        previous = {
            "song": {"tempo": 120.0},
            "tracks": [{"name": "Drums", "mute": False}],
        }
        current = {
            "song": {"tempo": 128.0},
            "tracks": [{"name": "Drums Bus", "mute": True}],
        }

        ops, lamport = diff_states(previous, current, "left", 0)
        self.assertEqual(lamport, 3)

        rebuilt = previous
        for op in ops:
            rebuilt = apply_operation_to_state(rebuilt, op)

        self.assertEqual(rebuilt, current)

    def test_list_length_change_replaces_branch(self) -> None:
        previous = {"tracks": [{"name": "A"}]}
        current = {"tracks": [{"name": "A"}, {"name": "B"}]}

        ops, _ = diff_states(previous, current, "left", 0)

        self.assertEqual(len(ops), 1)
        self.assertEqual(ops[0].path, "/tracks")

    def test_lww_ignores_stale_scalar_write(self) -> None:
        state = {"song": {"tempo": 120.0}}
        clock_state = {}

        winning_op = Operation(
            op_id="winner",
            client_id="right",
            lamport=5,
            kind="set",
            path="/song/tempo",
            value=128.0,
        )
        stale_op = Operation(
            op_id="stale",
            client_id="left",
            lamport=5,
            kind="set",
            path="/song/tempo",
            value=124.0,
        )

        state, clock_state, applied = apply_lww_operation(state, clock_state, winning_op)
        self.assertTrue(applied)

        state, clock_state, applied = apply_lww_operation(state, clock_state, stale_op)
        self.assertFalse(applied)
        self.assertEqual(state["song"]["tempo"], 128.0)
        self.assertEqual(clock_state["/song/tempo"]["client_id"], "right")

    def test_newer_descendant_survives_older_parent_write(self) -> None:
        state = {"tracks": [{"name": "Lead", "mute": False}]}
        clock_state = {
            "/tracks/0/name": {
                "lamport": 7,
                "client_id": "right",
                "op_id": "descendant",
                "kind": "set",
            }
        }
        parent_op = Operation(
            op_id="parent",
            client_id="left",
            lamport=6,
            kind="set",
            path="/tracks",
            value=[{"name": "Drums", "mute": True}],
        )

        state, clock_state, applied = apply_lww_operation(state, clock_state, parent_op)

        self.assertTrue(applied)
        self.assertEqual(state["tracks"][0]["name"], "Lead")
        self.assertTrue(state["tracks"][0]["mute"])
        self.assertEqual(clock_state["/tracks/0/name"]["lamport"], 7)
        self.assertEqual(clock_state["/tracks"]["lamport"], 6)


if __name__ == "__main__":
    unittest.main()
