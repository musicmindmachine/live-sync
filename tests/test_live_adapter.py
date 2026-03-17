import unittest
from typing import Optional

from ableton.LiveSyncRemoteScript.live_adapter import LiveSongAdapter
from ableton.LiveSyncRemoteScript.models import Operation


class DummySong:
    def __init__(self) -> None:
        self.tempo = 128.0
        self.groove_amount = 1.0
        self.tracks = []
        self.return_tracks = []
        self.master_track = None
        self.file_path = ""
        self.groove_pool = FakeGroovePool([])
        self.view = FakeSongView()


class FakeGroove:
    def __init__(self, name: str, base: int = 0) -> None:
        self.name = name
        self.base = base
        self.quantization_amount = 0.0
        self.random_amount = 0.0
        self.timing_amount = 0.0
        self.velocity_amount = 0.0


class FakeGroovePool:
    def __init__(self, grooves) -> None:
        self.grooves = grooves


class FakeSongView:
    def __init__(self) -> None:
        self.selected_track = None


class FakeBrowserItem:
    def __init__(self, name: str, children=None, is_loadable: bool = True) -> None:
        self.name = name
        self.children = children or []
        self.is_loadable = is_loadable


class FakeBrowser:
    def __init__(self, plugins=None) -> None:
        self.plugins = plugins or []
        self.loaded_item = None

    def load_item(self, item) -> None:
        self.loaded_item = item


class FakeApplication:
    def __init__(self, browser) -> None:
        self.browser = browser


class FakeParameter:
    def __init__(self, name: str, value: float = 0.0) -> None:
        self.name = name
        self.original_name = name
        self.value = value
        self.is_enabled = True
        self.state = 0
        self.min = 0.0
        self.max = 1.0
        self.is_quantized = False


class FakeClipView:
    def __init__(self) -> None:
        self.grid_quantization = 3
        self.grid_is_triplet = False


class FakeMidiClip:
    def __init__(self) -> None:
        self.name = "Clip"
        self.color = 8
        self.color_index = 8
        self.muted = False
        self.looping = True
        self.loop_start = 0.0
        self.loop_end = 4.0
        self.start_marker = 0.0
        self.end_marker = 4.0
        self.signature_numerator = 4
        self.signature_denominator = 4
        self.legato = False
        self.launch_mode = 0
        self.launch_quantization = 0
        self.velocity_amount = 0.0
        self.length = 4.0
        self.is_audio_clip = False
        self.is_midi_clip = True
        self.has_envelopes = False
        self.has_groove = False
        self.groove = None
        self.view = FakeClipView()

    def clear_all_envelopes(self) -> None:
        self.has_envelopes = False


class FakeClipSlot:
    def __init__(self) -> None:
        self.has_clip = True
        self.has_stop_button = True
        self.clip = FakeMidiClip()


class FakeDevice:
    def __init__(self, name: str, class_name: str, class_display_name: str) -> None:
        self.name = name
        self.class_name = class_name
        self.class_display_name = class_display_name
        self.type = 1
        self.is_active = True
        self.parameters = [FakeParameter("Value", 0.5)]


class FakeMixerDevice(FakeDevice):
    def __init__(self) -> None:
        super(FakeMixerDevice, self).__init__("Mixer", "MixerDevice", "Mixer")
        self.track_activator = FakeParameter("Track Activator", 1.0)
        self.volume = FakeParameter("Volume", 0.75)
        self.panning = FakeParameter("Panning", 0.0)
        self.sends = []
        self.crossfade_assign = 1
        self.panning_mode = 0


class FakeChainMixerDevice(FakeDevice):
    def __init__(self) -> None:
        super(FakeChainMixerDevice, self).__init__("Chain Mixer", "ChainMixerDevice", "Chain Mixer")
        self.chain_activator = FakeParameter("Chain Activator", 1.0)
        self.volume = FakeParameter("Volume", 0.5)
        self.panning = FakeParameter("Panning", 0.0)
        self.sends = []


class FakeTrackWithDevices:
    name = "Track"
    mute = False
    solo = False
    can_be_armed = False
    clip_slots = []
    arrangement_clips = []

    def __init__(self) -> None:
        self.mixer_device = FakeMixerDevice()
        self.devices = [self.mixer_device]

    def insert_device(self, device_name: str, index: int) -> None:
        class_name = {"Auto Filter": "AutoFilter", "EQ Eight": "Eq8"}.get(device_name, device_name.replace(" ", ""))
        self.devices.insert(index + 1, FakeDevice(device_name, class_name, device_name))

    def delete_device(self, index: int) -> None:
        del self.devices[index + 1]


class FakeTrackWithClipSlots:
    name = "Track"
    mute = False
    solo = False
    can_be_armed = False
    arrangement_clips = []

    def __init__(self) -> None:
        self.mixer_device = FakeMixerDevice()
        self.clip_slots = [FakeClipSlot()]


class FakeLegacyTrackWithDevices:
    name = "Track"
    mute = False
    solo = False
    can_be_armed = False
    clip_slots = []
    arrangement_clips = []

    def __init__(self) -> None:
        self.mixer_device = FakeMixerDevice()
        self.devices = [self.mixer_device, FakeDevice("Auto Filter", "AutoFilter", "Auto Filter")]


class FakeChain:
    def __init__(self, name: str = "Chain", note: Optional[int] = None) -> None:
        self.name = name
        self.mute = False
        self.solo = False
        self.color = 0
        self.is_auto_colored = False
        self.mixer_device = FakeChainMixerDevice()
        self.devices = []
        if note is not None:
            self.in_note = note
            self.out_note = note
            self.choke_group = 0

    def insert_device(self, device_name: str, index: int) -> None:
        class_name = {
            "Auto Filter": "AutoFilter",
            "EQ Eight": "Eq8",
            "Amp": "Amp",
        }.get(device_name, device_name.replace(" ", ""))
        self.devices.insert(index, FakeDevice(device_name, class_name, device_name))

    def delete_device(self, index: int) -> None:
        del self.devices[index]


class FakeDrumPad:
    def __init__(self, owner, note: int) -> None:
        self._owner = owner
        self.note = note
        self.mute = False
        self.solo = False

    @property
    def chains(self):
        return [chain for chain in self._owner.chains if getattr(chain, "in_note", None) == self.note]

    def delete_all_chains(self) -> None:
        self._owner.chains = [chain for chain in self._owner.chains if getattr(chain, "in_note", None) != self.note]


class FakeRackDevice(FakeDevice):
    def __init__(self, name: str = "Instrument Rack", class_name: str = "InstrumentGroupDevice") -> None:
        super(FakeRackDevice, self).__init__(name, class_name, name)
        self.is_showing_chains = False
        self.selected_variation_index = -1
        self.chains = [FakeChain("Chain 1")]
        self.return_chains = []
        self.drum_pads = []

    def insert_chain(self, index: Optional[int] = None) -> None:
        new_chain = FakeChain("New Chain")
        if index is None or index > len(self.chains):
            self.chains.append(new_chain)
            return
        self.chains.insert(index, new_chain)


class FakeDrumRackDevice(FakeRackDevice):
    def __init__(self) -> None:
        super(FakeDrumRackDevice, self).__init__("Drum Rack", "DrumGroupDevice")
        self.chains = []
        self.drum_pads = [FakeDrumPad(self, 36), FakeDrumPad(self, 38)]

    def insert_chain(self, index: Optional[int] = None) -> None:
        new_chain = FakeChain("New Chain", note=0)
        if index is None or index > len(self.chains):
            self.chains.append(new_chain)
            return
        self.chains.insert(index, new_chain)


class RaisingArrangementTrack:
    name = "Group"
    mute = False
    solo = False
    can_be_armed = False
    clip_slots = []

    def __init__(self) -> None:
        self.mixer_device = None

    @property
    def arrangement_clips(self):
        raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")

    def add_name_listener(self, listener) -> None:
        return

    def remove_name_listener(self, listener) -> None:
        return

    def add_mute_listener(self, listener) -> None:
        return

    def remove_mute_listener(self, listener) -> None:
        return

    def add_solo_listener(self, listener) -> None:
        return

    def remove_solo_listener(self, listener) -> None:
        return

    def add_back_to_arranger_listener(self, listener) -> None:
        return

    def remove_back_to_arranger_listener(self, listener) -> None:
        return

    def add_arrangement_clips_listener(self, listener) -> None:
        raise RuntimeError("Main, Group and Return Tracks have no arrangement clips")

    def remove_arrangement_clips_listener(self, listener) -> None:
        return


class SongWithGroupTrack(DummySong):
    def __init__(self) -> None:
        super(SongWithGroupTrack, self).__init__()
        self.tracks = [RaisingArrangementTrack()]


class LiveSongAdapterTests(unittest.TestCase):
    def test_accepts_concrete_song_object(self) -> None:
        adapter = LiveSongAdapter(DummySong())

        state = adapter.capture_state()

        self.assertEqual(state["song"]["tempo"], 128.0)
        self.assertEqual(state["tracks"], [])

    def test_ignores_tracks_without_arrangement_clips(self) -> None:
        adapter = LiveSongAdapter(SongWithGroupTrack())

        state = adapter.capture_state()
        adapter.start_listening(lambda: None)
        adapter.stop_listening()

        self.assertEqual(state["tracks"][0]["arrangement_clips"], [])

    def test_reconciles_track_devices_without_mixer_index_offset(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        track = FakeTrackWithDevices()

        adapter._reconcile_device_chain(
            track,
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
            "track 0",
            exclude_mixer=True,
        )

        self.assertEqual([device.name for device in track.devices[1:]], ["Auto Filter", "EQ Eight"])

    def test_applies_clip_slot_and_clip_view_metadata(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        track = FakeTrackWithClipSlots()

        adapter._apply_track_segments(track, ["clip_slots", "0", "has_stop_button"], False)
        adapter._apply_track_segments(track, ["clip_slots", "0", "clip", "color_index"], 14)
        adapter._apply_track_segments(track, ["clip_slots", "0", "clip", "view", "grid_quantization"], 5)
        adapter._apply_track_segments(track, ["clip_slots", "0", "clip", "view", "grid_is_triplet"], True)

        self.assertFalse(track.clip_slots[0].has_stop_button)
        self.assertEqual(track.clip_slots[0].clip.color_index, 14)
        self.assertEqual(track.clip_slots[0].clip.view.grid_quantization, 5)
        self.assertTrue(track.clip_slots[0].clip.view.grid_is_triplet)

    def test_applies_clip_groove_assignment_from_pool_index(self) -> None:
        song = DummySong()
        groove_a = FakeGroove("Swing 16-71")
        groove_b = FakeGroove("MPC 8-55")
        song.groove_pool = FakeGroovePool([groove_a, groove_b])
        clip = FakeMidiClip()
        track = FakeTrackWithClipSlots()
        track.clip_slots[0].clip = clip
        adapter = LiveSongAdapter(song)

        adapter._apply_clip_groove_index(clip, 1)

        self.assertIs(clip.groove, groove_b)

    def test_applies_groove_pool_snapshot(self) -> None:
        song = DummySong()
        groove = FakeGroove("Swing 16-71", base=0)
        song.groove_pool = FakeGroovePool([groove])
        adapter = LiveSongAdapter(song)

        adapter._apply_groove_pool_snapshot(
            song,
            [
                {
                    "name": "Tight Swing",
                    "base": 1,
                    "quantization_amount": 0.25,
                    "random_amount": 0.5,
                    "timing_amount": 0.75,
                    "velocity_amount": 1.0,
                }
            ],
        )

        self.assertEqual(groove.name, "Tight Swing")
        self.assertEqual(groove.base, 1)
        self.assertEqual(groove.quantization_amount, 0.25)
        self.assertEqual(groove.random_amount, 0.5)
        self.assertEqual(groove.timing_amount, 0.75)
        self.assertEqual(groove.velocity_amount, 1.0)

    def test_clears_envelopes_when_remote_state_has_none(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        clip = FakeMidiClip()
        clip.has_envelopes = True

        adapter._apply_shared_clip_property(clip, "has_envelopes", False)

        self.assertFalse(clip.has_envelopes)

    def test_legacy_tracks_filter_device_structure_operations(self) -> None:
        song = DummySong()
        song.tracks = [FakeLegacyTrackWithDevices()]
        adapter = LiveSongAdapter(song)

        operations = [
            Operation(
                op_id="struct",
                client_id="client-a",
                lamport=1,
                kind="set",
                path="/tracks/0/devices",
                value=[],
            ),
            Operation(
                op_id="param",
                client_id="client-a",
                lamport=2,
                kind="set",
                path="/tracks/0/devices/0/parameters/0/value",
                value=0.75,
            ),
        ]

        filtered = adapter.filter_outbound_operations(operations)

        self.assertEqual([operation.path for operation in filtered], ["/tracks/0/devices/0/parameters/0/value"])

    def test_filters_unsupported_plugin_device_structure_reverts(self) -> None:
        song = DummySong()
        song.tracks = [FakeTrackWithDevices()]
        adapter = LiveSongAdapter(song)
        previous_state = {
            "tracks": [
                {
                    "devices": [
                        {
                            "name": "Serum",
                            "class_name": "PluginDevice",
                            "class_display_name": "Serum",
                            "type": 4,
                            "is_active": True,
                            "parameters": [],
                        }
                    ]
                }
            ]
        }
        current_state = {"tracks": [{"devices": []}]}
        operations = [
            Operation(
                op_id="plugin-struct",
                client_id="client-a",
                lamport=1,
                kind="set",
                path="/tracks/0/devices",
                value=[],
            )
        ]

        filtered = adapter.filter_outbound_operations(
            operations,
            previous_state=previous_state,
            current_state=current_state,
        )

        self.assertEqual(filtered, [])

    def test_filters_plugin_device_structure_additions(self) -> None:
        song = DummySong()
        song.tracks = [FakeTrackWithDevices()]
        adapter = LiveSongAdapter(song)
        previous_state = {"tracks": [{"devices": []}]}
        current_state = {
            "tracks": [
                {
                    "devices": [
                        {
                            "name": "Serum",
                            "class_name": "PluginDevice",
                            "class_display_name": "Serum",
                            "type": 4,
                            "is_active": True,
                            "parameters": [],
                        }
                    ]
                }
            ]
        }
        operations = [
            Operation(
                op_id="plugin-add",
                client_id="client-a",
                lamport=1,
                kind="set",
                path="/tracks/0/devices",
                value=current_state["tracks"][0]["devices"],
            )
        ]

        filtered = adapter.filter_outbound_operations(
            operations,
            previous_state=previous_state,
            current_state=current_state,
        )

        self.assertEqual(filtered, [])

    def test_matching_existing_plugin_devices_still_sync_parameters(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        track = FakeTrackWithDevices()
        plugin = FakeDevice("Serum", "PluginDevice", "Serum")
        plugin.parameters = [FakeParameter("Cutoff", 0.25)]
        track.devices.append(plugin)

        adapter._reconcile_device_chain(
            track,
            [
                {
                    "name": "Serum",
                    "class_name": "PluginDevice",
                    "class_display_name": "Serum",
                    "type": 4,
                    "is_active": True,
                    "parameters": [
                        {
                            "name": "Cutoff",
                            "original_name": "Cutoff",
                            "value": 0.8,
                            "is_enabled": True,
                            "state": 0,
                            "min": 0.0,
                            "max": 1.0,
                            "is_quantized": False,
                        }
                    ],
                }
            ],
            "track 0",
            exclude_mixer=True,
        )

        self.assertEqual([device.class_name for device in track.devices[1:]], ["PluginDevice"])
        self.assertAlmostEqual(track.devices[1].parameters[0].value, 0.8)

    def test_legacy_tracks_only_apply_device_properties(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        track = FakeLegacyTrackWithDevices()

        adapter._reconcile_device_chain(
            track,
            [
                {
                    "name": "Auto Filter",
                    "class_name": "AutoFilter",
                    "class_display_name": "Auto Filter",
                    "type": 1,
                    "is_active": True,
                    "parameters": [
                        {
                            "name": "Value",
                            "original_name": "Value",
                            "value": 0.8,
                            "is_enabled": True,
                            "state": 0,
                            "min": 0.0,
                            "max": 1.0,
                            "is_quantized": False,
                        }
                    ],
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
            "track 0",
            exclude_mixer=True,
        )

        self.assertEqual([device.name for device in track.devices[1:]], ["Auto Filter"])
        self.assertAlmostEqual(track.devices[1].parameters[0].value, 0.8)

    def test_reconciles_nested_rack_chains_and_device_parameters(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        rack = FakeRackDevice()
        rack.chains[0].devices = [FakeDevice("Auto Filter", "AutoFilter", "Auto Filter")]

        desired_rack = {
            "name": "Instrument Rack",
            "class_name": "InstrumentGroupDevice",
            "class_display_name": "Instrument Rack",
            "type": 1,
            "is_active": True,
            "parameters": [
                {
                    "name": "Value",
                    "original_name": "Value",
                    "value": 0.5,
                    "is_enabled": True,
                    "state": 0,
                    "min": 0.0,
                    "max": 1.0,
                    "is_quantized": False,
                }
            ],
            "is_showing_chains": True,
            "selected_variation_index": 2,
            "chains": [
                {
                    "name": "Chain 1",
                    "mute": False,
                    "solo": False,
                    "color": 0,
                    "is_auto_colored": False,
                    "devices": [
                        {
                            "name": "Auto Filter",
                            "class_name": "AutoFilter",
                            "class_display_name": "Auto Filter",
                            "type": 1,
                            "is_active": True,
                            "parameters": [
                                {
                                    "name": "Value",
                                    "original_name": "Value",
                                    "value": 0.9,
                                    "is_enabled": True,
                                    "state": 0,
                                    "min": 0.0,
                                    "max": 1.0,
                                    "is_quantized": False,
                                }
                            ],
                        }
                    ],
                    "mixer": {
                        "chain_activator": 1.0,
                        "volume": 0.8,
                        "panning": 0.1,
                        "sends": [],
                    },
                },
                {
                    "name": "Chain 2",
                    "mute": False,
                    "solo": False,
                    "color": 0,
                    "is_auto_colored": False,
                    "devices": [
                        {
                            "name": "EQ Eight",
                            "class_name": "Eq8",
                            "class_display_name": "EQ Eight",
                            "type": 1,
                            "is_active": True,
                            "parameters": [],
                        }
                    ],
                    "mixer": {
                        "chain_activator": 1.0,
                        "volume": 0.5,
                        "panning": 0.0,
                        "sends": [],
                    },
                },
            ],
        }

        adapter._apply_device_properties(rack, desired_rack)
        adapter._reconcile_device_contents(rack, desired_rack, "rack")

        self.assertTrue(rack.is_showing_chains)
        self.assertEqual(rack.selected_variation_index, 2)
        self.assertEqual(len(rack.chains), 2)
        self.assertAlmostEqual(rack.chains[0].mixer_device.volume.value, 0.8)
        self.assertAlmostEqual(rack.chains[0].devices[0].parameters[0].value, 0.9)
        self.assertEqual(rack.chains[1].devices[0].name, "EQ Eight")

    def test_reconciles_drum_pad_chains(self) -> None:
        adapter = LiveSongAdapter(DummySong())
        drum_rack = FakeDrumRackDevice()

        desired_rack = {
            "name": "Drum Rack",
            "class_name": "DrumGroupDevice",
            "class_display_name": "Drum Rack",
            "type": 1,
            "is_active": True,
            "parameters": [],
            "drum_pads": [
                {
                    "note": 36,
                    "mute": False,
                    "solo": False,
                    "chains": [
                        {
                            "name": "Kick",
                            "mute": False,
                            "solo": False,
                            "color": 0,
                            "is_auto_colored": False,
                            "in_note": 36,
                            "out_note": 36,
                            "choke_group": 0,
                            "devices": [
                                {
                                    "name": "Amp",
                                    "class_name": "Amp",
                                    "class_display_name": "Amp",
                                    "type": 1,
                                    "is_active": True,
                                    "parameters": [],
                                }
                            ],
                            "mixer": {
                                "chain_activator": 1.0,
                                "volume": 0.6,
                                "panning": 0.0,
                                "sends": [],
                            },
                        }
                    ],
                }
            ],
        }

        adapter._reconcile_device_contents(drum_rack, desired_rack, "drum-rack")

        kick_pad = drum_rack.drum_pads[0]
        self.assertEqual(len(kick_pad.chains), 1)
        self.assertEqual(kick_pad.chains[0].name, "Kick")
        self.assertEqual(kick_pad.chains[0].devices[0].name, "Amp")
        self.assertEqual(kick_pad.chains[0].in_note, 36)

    def test_browser_loader_can_find_plugin_item(self) -> None:
        song = DummySong()
        track = FakeTrackWithDevices()
        song.tracks = [track]
        browser_item = FakeBrowserItem("Serum")
        browser = FakeBrowser(plugins=[FakeBrowserItem("Plugins", children=[browser_item], is_loadable=False)])
        adapter = LiveSongAdapter(song, application_provider=lambda: FakeApplication(browser))

        loaded = adapter._try_load_non_native_device(
            track,
            {
                "name": "Serum",
                "class_name": "PluginDevice",
                "class_display_name": "Serum",
                "type": 4,
                "is_active": True,
                "parameters": [],
            },
            0,
            "track 0",
        )

        self.assertTrue(loaded)
        self.assertIs(browser.loaded_item, browser_item)


if __name__ == "__main__":
    unittest.main()
