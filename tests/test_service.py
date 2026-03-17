from __future__ import annotations

import unittest

from ableton.LiveSyncRemoteScript.in_memory_backend import InMemoryConvexClient, InMemorySyncBackend
from ableton.LiveSyncRemoteScript.mock_adapter import MockLiveAdapter
from ableton.LiveSyncRemoteScript.service import SyncService
from ableton.LiveSyncRemoteScript.sync_core import set_json_value


def build_state():
    return {
        "song": {"tempo": 120.0, "groove_amount": 1.0},
        "groove_pool": [],
        "tracks": [
            {
                "name": "Drums",
                "mute": False,
                "solo": False,
                "back_to_arranger": False,
                "volume": 0.75,
                "panning": 0.0,
                "sends": [0.0],
                "devices": [],
                "arrangement_clips": [],
                "clip_slots": [
                    {
                        "has_clip": True,
                        "has_stop_button": True,
                        "clip": {
                            "name": "Beat",
                            "color": 8,
                            "color_index": 8,
                            "muted": False,
                            "has_envelopes": False,
                            "looping": False,
                            "loop_start": 0.0,
                            "loop_end": 4.0,
                            "start_marker": 0.0,
                            "end_marker": 4.0,
                            "signature_numerator": 4,
                            "signature_denominator": 4,
                            "legato": False,
                            "launch_mode": 0,
                            "launch_quantization": 0,
                            "velocity_amount": 0.0,
                            "length": 4.0,
                            "is_audio_clip": False,
                            "is_midi_clip": True,
                            "media_relative_path": None,
                            "groove_index": None,
                            "view": {
                                "grid_quantization": 3,
                                "grid_is_triplet": False,
                            },
                            "notes": [
                                {
                                    "pitch": 36,
                                    "start_time": 0.0,
                                    "duration": 1.0,
                                    "velocity": 100,
                                    "mute": False,
                                    "probability": 1.0,
                                    "velocity_deviation": 0.0,
                                    "release_velocity": 64,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
        "return_tracks": [],
        "master_track": {"volume": 0.9, "panning": 0.0, "sends": [], "devices": []},
    }


class TestScheduler:
    def __init__(self) -> None:
        self._pending = []

    def schedule(self, callback) -> None:
        self._pending.append(callback)

    def drain(self) -> None:
        while self._pending:
            callback = self._pending.pop(0)
            callback()


class FilteringMockLiveAdapter(MockLiveAdapter):
    def __init__(self, initial_state, blocked_paths) -> None:
        super(FilteringMockLiveAdapter, self).__init__(initial_state)
        self._blocked_paths = set(blocked_paths)

    def filter_outbound_operations(self, operations, previous_state=None, current_state=None):
        return [operation for operation in operations if operation.path not in self._blocked_paths]


class PollingMockLiveAdapter(MockLiveAdapter):
    def __init__(self, initial_state) -> None:
        super(PollingMockLiveAdapter, self).__init__(initial_state)
        self._pending_polled_change = False

    def set_path_via_poll(self, path, value) -> None:
        self._state = set_json_value(self._state, path, value)
        self._pending_polled_change = True

    def poll_for_clip_note_changes(self) -> bool:
        if not self._pending_polled_change:
            return False
        self._pending_polled_change = False
        return True


class SyncServiceTests(unittest.TestCase):
    def test_nested_clip_paths_do_not_force_full_reconcile(self) -> None:
        service = SyncService(
            adapter=MockLiveAdapter(build_state()),
            client=InMemoryConvexClient(InMemorySyncBackend()),
            room_id="jam",
            client_id="left",
        )

        self.assertFalse(service._requires_full_reconcile("/tracks/0/clip_slots/0/clip/name"))
        self.assertFalse(service._requires_full_reconcile("/tracks/0/clip_slots/0/clip/notes"))
        self.assertFalse(service._requires_full_reconcile("/tracks/0/clip_slots/0/clip/view/grid_quantization"))
        self.assertFalse(service._requires_full_reconcile("/tracks/0/clip_slots/0/has_stop_button"))
        self.assertTrue(service._requires_full_reconcile("/tracks/0/clip_slots/0/clip"))
        self.assertTrue(service._requires_full_reconcile("/tracks/0/clip_slots/0/has_clip"))

    def test_polled_note_changes_sync_without_listener_event(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = PollingMockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path_via_poll(
            "/tracks/0/clip_slots/0/clip/notes",
            [
                {
                    "pitch": 36,
                    "start_time": 0.0,
                    "duration": 1.0,
                    "velocity": 100,
                    "mute": False,
                    "probability": 1.0,
                    "velocity_deviation": 0.0,
                    "release_velocity": 64,
                },
                {
                    "pitch": 43,
                    "start_time": 1.0,
                    "duration": 0.5,
                    "velocity": 92,
                    "mute": False,
                    "probability": 1.0,
                    "velocity_deviation": 0.0,
                    "release_velocity": 64,
                },
            ],
        )

        self.assertTrue(left.poll_local_state())
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_filtered_local_operations_do_not_revert_remote_state(self) -> None:
        backend = InMemorySyncBackend()
        adapter = FilteringMockLiveAdapter(build_state(), {"/tracks/0/devices"})
        adapter.set_path(
            "/tracks/0/devices",
            [
                {
                    "name": "Auto Filter",
                    "class_name": "AutoFilter",
                    "class_display_name": "Auto Filter",
                    "type": 1,
                    "is_active": True,
                    "parameters": [],
                }
            ],
        )
        service = SyncService(
            adapter=adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
        )
        service._shadow_state = build_state()

        pushed = service._push_local_changes()

        self.assertEqual(pushed, 0)
        self.assertEqual(backend.pull_ops("jam", after_sequence=0).latest_sequence, 0)

    def test_device_state_reconciles_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_state = build_state()
        left_state["tracks"][0]["devices"] = [
            {
                "name": "Auto Filter",
                "class_name": "AutoFilter",
                "class_display_name": "Auto Filter",
                "type": 1,
                "is_active": True,
                "parameters": [
                    {
                        "name": "Frequency",
                        "original_name": "Frequency",
                        "value": 440.0,
                        "is_enabled": True,
                        "state": 0,
                        "min": 26.0,
                        "max": 19000.0,
                        "is_quantized": False,
                    }
                ],
            }
        ]
        left_adapter = MockLiveAdapter(left_state)
        right_adapter = MockLiveAdapter(build_state())

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/tracks/0/devices/0/parameters/0/value", 880.0)
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_device_insertions_reconcile_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter(build_state())

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path(
            "/tracks/0/devices",
            [
                {
                    "name": "Auto Filter",
                    "class_name": "AutoFilter",
                    "class_display_name": "Auto Filter",
                    "type": 1,
                    "is_active": True,
                    "parameters": [],
                },
                {
                    "name": "EQ Eight",
                    "class_name": "Eq8",
                    "class_display_name": "EQ Eight",
                    "type": 1,
                    "is_active": True,
                    "parameters": [],
                },
            ],
        )
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_second_client_bootstraps_from_first(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left = SyncService(
            adapter=MockLiveAdapter(build_state()),
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right_adapter = MockLiveAdapter({})
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), build_state())

    def test_remote_edits_reconcile_without_polling(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/song/tempo", 134.0)
        left_adapter.set_path("/tracks/0/name", "Lead")
        left_adapter.set_path("/tracks/0/clip_slots/0/clip/name", "Lead Clip")

        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_stale_client_recovers_from_compacted_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        left.start()
        scheduler.drain()

        for tempo in [121.0, 122.0, 123.0, 124.0, 125.0, 126.0]:
            left_adapter.set_path("/song/tempo", tempo)
        scheduler.drain()

        stale_adapter = MockLiveAdapter({})
        stale = SyncService(
            adapter=stale_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="stale",
            schedule_main_thread=scheduler.schedule,
        )
        stale.start()
        scheduler.drain()

        self.assertEqual(stale_adapter.capture_state(), left_adapter.capture_state())

    def test_concurrent_conflicting_writes_converge_with_lww(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/song/tempo", 124.0)
        right_adapter.set_path("/song/tempo", 128.0)
        scheduler.drain()

        self.assertEqual(left_adapter.capture_state(), right_adapter.capture_state())
        self.assertEqual(right_adapter.capture_state()["song"]["tempo"], 128.0)

    def test_arrangement_clip_changes_reconcile_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path(
            "/tracks/0/arrangement_clips",
            [
                {
                    "name": "Verse",
                    "color": 12,
                    "muted": False,
                    "start_time": 8.0,
                    "end_time": 12.0,
                    "length": 4.0,
                    "looping": False,
                    "loop_start": 0.0,
                    "loop_end": 4.0,
                    "start_marker": 0.0,
                    "end_marker": 4.0,
                    "is_audio_clip": True,
                    "is_midi_clip": False,
                    "media_relative_path": "Samples/Imported/verse.wav",
                }
            ],
        )
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_session_clip_note_changes_reconcile_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path(
            "/tracks/0/clip_slots/0/clip/notes",
            [
                {
                    "pitch": 36,
                    "start_time": 0.0,
                    "duration": 1.0,
                    "velocity": 100,
                    "mute": False,
                    "probability": 1.0,
                    "velocity_deviation": 0.0,
                    "release_velocity": 64,
                },
                {
                    "pitch": 43,
                    "start_time": 1.0,
                    "duration": 0.5,
                    "velocity": 92,
                    "mute": False,
                    "probability": 1.0,
                    "velocity_deviation": 0.0,
                    "release_velocity": 64,
                },
            ],
        )
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_session_clip_metadata_changes_sync(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/tracks/0/clip_slots/0/has_stop_button", False)
        left_adapter.set_path("/tracks/0/clip_slots/0/clip/color_index", 14)
        left_adapter.set_path("/tracks/0/clip_slots/0/clip/view/grid_quantization", 5)
        left_adapter.set_path("/tracks/0/clip_slots/0/clip/view/grid_is_triplet", True)
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_groove_pool_and_clip_assignment_sync(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/song/groove_amount", 0.65)
        left_adapter.set_path(
            "/groove_pool",
            [
                {
                    "name": "Swing 16-71",
                    "base": 0,
                    "quantization_amount": 0.2,
                    "random_amount": 0.1,
                    "timing_amount": 0.75,
                    "velocity_amount": 0.5,
                }
            ],
        )
        left_adapter.set_path("/tracks/0/clip_slots/0/clip/groove_index", 0)
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_session_clip_creation_reconcile_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        empty_state = build_state()
        empty_state["tracks"][0]["clip_slots"] = [{"has_clip": False, "clip": None}]
        left_adapter = MockLiveAdapter(empty_state)
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path("/tracks/0/clip_slots/0", build_state()["tracks"][0]["clip_slots"][0])
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())

    def test_arrangement_midi_note_changes_reconcile_from_full_snapshot(self) -> None:
        backend = InMemorySyncBackend()
        scheduler = TestScheduler()
        left_adapter = MockLiveAdapter(build_state())
        right_adapter = MockLiveAdapter({})

        left = SyncService(
            adapter=left_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="left",
            schedule_main_thread=scheduler.schedule,
        )
        right = SyncService(
            adapter=right_adapter,
            client=InMemoryConvexClient(backend),
            room_id="jam",
            client_id="right",
            schedule_main_thread=scheduler.schedule,
        )

        left.start()
        right.start()
        scheduler.drain()

        left_adapter.set_path(
            "/tracks/0/arrangement_clips",
            [
                {
                    "name": "Verse MIDI",
                    "color": 19,
                    "muted": False,
                    "start_time": 8.0,
                    "end_time": 12.0,
                    "length": 4.0,
                    "looping": False,
                    "loop_start": 0.0,
                    "loop_end": 4.0,
                    "start_marker": 0.0,
                    "end_marker": 4.0,
                    "signature_numerator": 4,
                    "signature_denominator": 4,
                    "legato": False,
                    "launch_mode": 0,
                    "launch_quantization": 0,
                    "velocity_amount": 0.0,
                    "is_audio_clip": False,
                    "is_midi_clip": True,
                    "media_relative_path": None,
                    "notes": [
                        {
                            "pitch": 60,
                            "start_time": 0.0,
                            "duration": 1.0,
                            "velocity": 100,
                            "mute": False,
                            "probability": 1.0,
                            "velocity_deviation": 0.0,
                            "release_velocity": 64,
                        },
                        {
                            "pitch": 64,
                            "start_time": 1.0,
                            "duration": 1.0,
                            "velocity": 100,
                            "mute": False,
                            "probability": 1.0,
                            "velocity_deviation": 0.0,
                            "release_velocity": 64,
                        },
                    ],
                }
            ],
        )
        scheduler.drain()

        self.assertEqual(right_adapter.capture_state(), left_adapter.capture_state())


if __name__ == "__main__":
    unittest.main()
