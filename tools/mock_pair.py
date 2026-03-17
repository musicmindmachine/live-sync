from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ableton.LiveSyncRemoteScript.in_memory_backend import InMemoryConvexClient, InMemorySyncBackend
from ableton.LiveSyncRemoteScript.mock_adapter import MockLiveAdapter
from ableton.LiveSyncRemoteScript.service import SyncService


class ImmediateScheduler:
    def __init__(self) -> None:
        self._pending = []

    def schedule(self, callback) -> None:
        self._pending.append(callback)

    def drain(self) -> None:
        while self._pending:
            callback = self._pending.pop(0)
            callback()


def build_initial_state():
    return {
        "song": {"tempo": 120.0},
        "tracks": [
            {
                "name": "Drums",
                "mute": False,
                "solo": False,
                "back_to_arranger": False,
                "volume": 0.75,
                "panning": 0.0,
                "sends": [0.1, 0.0],
                "arrangement_clips": [],
                "clip_slots": [
                    {
                        "has_clip": True,
                        "clip": {
                            "name": "Beat",
                            "color": 12,
                            "muted": False,
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
                    },
                    {"has_clip": False, "clip": None},
                ],
            }
        ],
        "return_tracks": [],
        "master_track": {"volume": 0.85, "panning": 0.0, "sends": []},
    }


def main() -> None:
    backend = InMemorySyncBackend()
    scheduler = ImmediateScheduler()
    left_adapter = MockLiveAdapter(build_initial_state())
    right_adapter = MockLiveAdapter({})

    left = SyncService(
        adapter=left_adapter,
        client=InMemoryConvexClient(backend),
        room_id="demo-room",
        client_id="left",
        schedule_main_thread=scheduler.schedule,
        logger=print,
    )
    right = SyncService(
        adapter=right_adapter,
        client=InMemoryConvexClient(backend),
        room_id="demo-room",
        client_id="right",
        schedule_main_thread=scheduler.schedule,
        logger=print,
    )

    left.start()
    right.start()
    scheduler.drain()

    left_adapter.set_path("/song/tempo", 128.0)
    left_adapter.set_path("/tracks/0/name", "Lead Synth")
    left_adapter.set_path("/tracks/0/clip_slots/0/clip/name", "Intro Beat")
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

    print(json.dumps(right_adapter.capture_state(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
