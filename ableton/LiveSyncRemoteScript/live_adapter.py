from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .sync_core import decode_pointer, diff_states, get_json_value


ListenerRegistration = Tuple[Callable[[Callable[[], None]], None], Callable[[], None]]


class LiveSongAdapter:
    def __init__(
        self,
        song_provider: Any,
        application_provider: Optional[Any] = None,
        logger: Optional[Callable[[str], None]] = None,
        project_root: Optional[str] = None,
    ) -> None:
        if callable(song_provider):
            self._song_provider = song_provider
        else:
            self._song_provider = lambda: song_provider
        self._application_provider = application_provider
        self._logger = logger or (lambda message: None)
        self._on_change: Optional[Callable[[], None]] = None
        self._listener_registrations: List[Tuple[Callable[[], None], Callable[[], None]]] = []
        self._configured_project_root = Path(project_root).expanduser().resolve() if project_root else None
        self._suppress_change_depth = 0
        self._logged_device_structure_warnings = set()
        self._pending_browser_device_loads = set()

    def capture_state(self) -> Dict[str, Any]:
        song = self._song_provider()
        return {
            "song": {
                "tempo": self._safe_number(getattr(song, "tempo", 120.0)),
                "groove_amount": self._safe_number(getattr(song, "groove_amount", 1.0)),
            },
            "groove_pool": self._snapshot_groove_pool(song),
            "tracks": [
                self._snapshot_track(track, include_arm=True, track_index=index)
                for index, track in enumerate(getattr(song, "tracks", []))
            ],
            "return_tracks": [
                self._snapshot_track(track, include_arm=False, track_index=index)
                for index, track in enumerate(getattr(song, "return_tracks", []))
            ],
            "master_track": self._snapshot_master_track(getattr(song, "master_track", None)),
        }

    def get_project_root(self) -> Optional[str]:
        project_root = self._project_root_path()
        return str(project_root) if project_root is not None else None

    def capture_media_references(self) -> List[Dict[str, Any]]:
        references: List[Dict[str, Any]] = []
        song = self._song_provider()
        project_root = self._project_root_path()

        for track_index, track in enumerate(getattr(song, "tracks", [])):
            for slot_index, clip_slot in enumerate(getattr(track, "clip_slots", [])):
                clip = getattr(clip_slot, "clip", None) if getattr(clip_slot, "has_clip", False) else None
                if clip is None or not bool(getattr(clip, "is_audio_clip", False)):
                    continue

                file_path = self._clip_file_path(clip)
                if not file_path:
                    continue

                relative_path = self._relative_media_path(project_root, file_path, track_index, slot_index)
                self._append_media_reference(
                    references,
                    reference_id="tracks/%s/clip_slots/%s" % (track_index, slot_index),
                    lom_path="/tracks/%s/clip_slots/%s/clip" % (track_index, slot_index),
                    relative_path=relative_path,
                    absolute_path=file_path,
                    role="audio_clip_source",
                )

            for arrangement_index, clip in enumerate(self._arrangement_clips(track)):
                if clip is None or not bool(getattr(clip, "is_audio_clip", False)):
                    continue
                file_path = self._clip_file_path(clip)
                if not file_path:
                    continue

                relative_path = self._relative_media_path(project_root, file_path, track_index, arrangement_index)
                self._append_media_reference(
                    references,
                    reference_id="tracks/%s/arrangement_clips/%s" % (track_index, arrangement_index),
                    lom_path="/tracks/%s/arrangement_clips/%s" % (track_index, arrangement_index),
                    relative_path=relative_path,
                    absolute_path=file_path,
                    role="arrangement_audio_clip_source",
                )

        return references

    def start_listening(self, on_change: Callable[[], None]) -> None:
        self.stop_listening()
        self._on_change = on_change
        self._bind_all_listeners()

    def stop_listening(self) -> None:
        while self._listener_registrations:
            _, remove_listener = self._listener_registrations.pop()
            try:
                remove_listener()
            except Exception:
                pass
        self._on_change = None

    def apply_snapshot(self, snapshot_state: Any) -> None:
        self._begin_suppress_changes()
        try:
            current_state = self.capture_state()
            operations, _ = diff_states(current_state, snapshot_state, "__snapshot__", 0)
            for operation in operations:
                if self._is_snapshot_reconciled_clip_path(operation.path):
                    continue
                self.apply_operation(operation)
            self._reconcile_session_snapshot(snapshot_state)
            self._reconcile_arrangement_snapshot(snapshot_state)
            self._reconcile_device_snapshot(snapshot_state)
        finally:
            self._end_suppress_changes()

    def _is_snapshot_reconciled_clip_path(self, path: str) -> bool:
        if "/arrangement_clips" in path:
            return True
        if "/devices" in path or "/parameters" in path:
            return True
        if "/clip_slots/" not in path:
            return False
        return path.endswith("/has_clip") or "/clip" in path

    def apply_operation(self, operation) -> None:
        self._begin_suppress_changes()
        try:
            if operation.kind != "set":
                return

            segments = decode_pointer(operation.path)
            if not segments:
                self._log("Ignoring root-level set operation; structural replacement is not implemented.")
                return

            song = self._song_provider()

            if segments == ["song", "tempo"]:
                setattr(song, "tempo", float(operation.value))
                return
            if segments == ["song", "groove_amount"]:
                if hasattr(song, "groove_amount"):
                    setattr(song, "groove_amount", float(operation.value))
                return

            if segments[0] == "groove_pool":
                self._apply_groove_pool_segments(song, segments[1:], operation.value)
                return

            if segments[0] in ("tracks", "return_tracks"):
                if len(segments) < 2:
                    self._log("Ignoring structural track change for unsupported path: %s" % operation.path)
                    return
                track = self._resolve_list_item(getattr(song, segments[0], []), segments[1])
                if track is None:
                    self._log("Track path out of range: %s" % operation.path)
                    return
                self._apply_track_segments(track, segments[2:], operation.value)
                return

            if segments[0] == "master_track":
                self._apply_master_segments(getattr(song, "master_track", None), segments[1:], operation.value)
                return

            self._log("Unsupported Live path: %s" % operation.path)
        finally:
            self._end_suppress_changes()

    def filter_outbound_operations(
        self,
        operations: List[Any],
        previous_state: Any = None,
        current_state: Any = None,
    ) -> List[Any]:
        filtered: List[Any] = []
        for operation in operations:
            label = self._unsupported_device_structure_label(operation.path)
            if label is not None:
                self._log_device_structure_unsupported(label)
                continue
            if self._should_skip_unsupported_plugin_structure(operation, previous_state, current_state):
                self._log("Skipping unsupported plug-in or Max device structure sync for %s." % operation.path)
                continue
            filtered.append(operation)
        return filtered

    def _bind_all_listeners(self) -> None:
        song = self._song_provider()
        self._register_listener(song, "add_tempo_listener", "remove_tempo_listener", self._emit_change)
        self._register_listener(song, "add_groove_amount_listener", "remove_groove_amount_listener", self._emit_change)
        self._register_listener(song, "add_tracks_listener", "remove_tracks_listener", self._rebind_after_structure_change)
        self._register_listener(
            song,
            "add_return_tracks_listener",
            "remove_return_tracks_listener",
            self._rebind_after_structure_change,
        )
        self._bind_groove_pool_listeners(song)

        for track in getattr(song, "tracks", []):
            self._bind_track_listeners(track, include_arm=True)
        for track in getattr(song, "return_tracks", []):
            self._bind_track_listeners(track, include_arm=False)
        self._bind_master_track_listeners(getattr(song, "master_track", None))

    def _bind_track_listeners(self, track: Any, include_arm: bool) -> None:
        self._register_listener(track, "add_color_listener", "remove_color_listener", self._emit_change)
        self._register_listener(track, "add_name_listener", "remove_name_listener", self._emit_change)
        self._register_listener(track, "add_mute_listener", "remove_mute_listener", self._emit_change)
        self._register_listener(track, "add_solo_listener", "remove_solo_listener", self._emit_change)
        self._register_listener(track, "add_back_to_arranger_listener", "remove_back_to_arranger_listener", self._emit_change)
        self._register_listener(track, "add_devices_listener", "remove_devices_listener", self._rebind_after_structure_change)
        self._register_listener(
            track,
            "add_arrangement_clips_listener",
            "remove_arrangement_clips_listener",
            self._rebind_after_structure_change,
        )

        if include_arm and getattr(track, "can_be_armed", False):
            self._register_listener(track, "add_arm_listener", "remove_arm_listener", self._emit_change)

        mixer_device = getattr(track, "mixer_device", None)
        self._register_listener(
            mixer_device,
            "add_crossfade_assign_listener",
            "remove_crossfade_assign_listener",
            self._emit_change,
        )
        self._register_listener(
            mixer_device,
            "add_panning_mode_listener",
            "remove_panning_mode_listener",
            self._emit_change,
        )
        self._bind_parameter_listener(getattr(mixer_device, "track_activator", None))
        self._bind_parameter_listener(getattr(mixer_device, "volume", None))
        self._bind_parameter_listener(getattr(mixer_device, "panning", None))
        for send in getattr(mixer_device, "sends", []):
            self._bind_parameter_listener(send)

        for clip_slot in getattr(track, "clip_slots", []):
            self._bind_clip_slot_listeners(clip_slot)
        for arrangement_clip in self._arrangement_clips(track):
            self._bind_clip_listeners(arrangement_clip, include_arrangement_geometry=True)
        for device in self._device_chain_devices(track, exclude_mixer=True):
            self._bind_device_listeners(device)

    def _bind_master_track_listeners(self, track: Any) -> None:
        if track is None:
            return
        self._register_listener(track, "add_devices_listener", "remove_devices_listener", self._rebind_after_structure_change)
        mixer_device = getattr(track, "mixer_device", None)
        self._bind_parameter_listener(getattr(mixer_device, "cue_volume", None))
        self._bind_parameter_listener(getattr(mixer_device, "crossfader", None))
        self._bind_parameter_listener(getattr(mixer_device, "volume", None))
        self._bind_parameter_listener(getattr(mixer_device, "panning", None))
        for send in getattr(mixer_device, "sends", []):
            self._bind_parameter_listener(send)
        for device in self._device_chain_devices(track, exclude_mixer=True):
            self._bind_device_listeners(device)

    def _bind_parameter_listener(self, parameter: Any) -> None:
        self._register_listener(parameter, "add_value_listener", "remove_value_listener", self._emit_change)

    def _bind_clip_slot_listeners(self, clip_slot: Any) -> None:
        self._register_listener(
            clip_slot,
            "add_has_clip_listener",
            "remove_has_clip_listener",
            self._rebind_after_structure_change,
        )
        self._register_listener(
            clip_slot,
            "add_has_stop_button_listener",
            "remove_has_stop_button_listener",
            self._emit_change,
        )
        clip = getattr(clip_slot, "clip", None) if getattr(clip_slot, "has_clip", False) else None
        if clip is None:
            return
        self._bind_clip_listeners(clip, include_arrangement_geometry=False)

    def _bind_clip_listeners(self, clip: Any, include_arrangement_geometry: bool) -> None:
        self._register_listener(clip, "add_name_listener", "remove_name_listener", self._emit_change)
        self._register_listener(clip, "add_color_listener", "remove_color_listener", self._emit_change)
        self._register_listener(clip, "add_color_index_listener", "remove_color_index_listener", self._emit_change)
        self._register_listener(clip, "add_muted_listener", "remove_muted_listener", self._emit_change)
        self._register_listener(clip, "add_file_path_listener", "remove_file_path_listener", self._emit_change)
        self._register_listener(clip, "add_groove_listener", "remove_groove_listener", self._emit_change)
        self._register_listener(clip, "add_has_envelopes_listener", "remove_has_envelopes_listener", self._emit_change)
        self._register_listener(clip, "add_notes_listener", "remove_notes_listener", self._emit_change)
        self._register_listener(clip, "add_looping_listener", "remove_looping_listener", self._emit_change)
        self._register_listener(clip, "add_loop_start_listener", "remove_loop_start_listener", self._emit_change)
        self._register_listener(clip, "add_loop_end_listener", "remove_loop_end_listener", self._emit_change)
        self._register_listener(clip, "add_start_marker_listener", "remove_start_marker_listener", self._emit_change)
        self._register_listener(clip, "add_end_marker_listener", "remove_end_marker_listener", self._emit_change)
        self._register_listener(
            clip,
            "add_signature_numerator_listener",
            "remove_signature_numerator_listener",
            self._emit_change,
        )
        self._register_listener(
            clip,
            "add_signature_denominator_listener",
            "remove_signature_denominator_listener",
            self._emit_change,
        )
        self._register_listener(clip, "add_legato_listener", "remove_legato_listener", self._emit_change)
        self._register_listener(clip, "add_launch_mode_listener", "remove_launch_mode_listener", self._emit_change)
        self._register_listener(
            clip,
            "add_launch_quantization_listener",
            "remove_launch_quantization_listener",
            self._emit_change,
        )
        self._register_listener(
            clip,
            "add_velocity_amount_listener",
            "remove_velocity_amount_listener",
            self._emit_change,
        )
        self._register_listener(clip, "add_gain_listener", "remove_gain_listener", self._emit_change)
        self._register_listener(
            clip,
            "add_pitch_coarse_listener",
            "remove_pitch_coarse_listener",
            self._emit_change,
        )
        self._register_listener(clip, "add_pitch_fine_listener", "remove_pitch_fine_listener", self._emit_change)
        self._register_listener(clip, "add_warping_listener", "remove_warping_listener", self._emit_change)
        self._register_listener(clip, "add_warp_mode_listener", "remove_warp_mode_listener", self._emit_change)
        self._register_listener(clip, "add_ram_mode_listener", "remove_ram_mode_listener", self._emit_change)
        self._bind_clip_view_listeners(getattr(clip, "view", None))
        if not include_arrangement_geometry:
            return
        self._register_listener(clip, "add_start_time_listener", "remove_start_time_listener", self._emit_change)
        self._register_listener(clip, "add_end_time_listener", "remove_end_time_listener", self._emit_change)

    def _bind_clip_view_listeners(self, clip_view: Any) -> None:
        self._register_listener(
            clip_view,
            "add_grid_quantization_listener",
            "remove_grid_quantization_listener",
            self._emit_change,
        )
        self._register_listener(
            clip_view,
            "add_grid_is_triplet_listener",
            "remove_grid_is_triplet_listener",
            self._emit_change,
        )

    def _bind_groove_pool_listeners(self, song: Any) -> None:
        groove_pool = getattr(song, "groove_pool", None)
        self._register_listener(groove_pool, "add_grooves_listener", "remove_grooves_listener", self._rebind_after_structure_change)
        for groove in self._groove_pool_grooves(song):
            self._bind_groove_listeners(groove)

    def _bind_groove_listeners(self, groove: Any) -> None:
        self._register_listener(groove, "add_name_listener", "remove_name_listener", self._emit_change)
        self._register_listener(groove, "add_base_listener", "remove_base_listener", self._emit_change)
        self._register_listener(
            groove,
            "add_quantization_amount_listener",
            "remove_quantization_amount_listener",
            self._emit_change,
        )
        self._register_listener(groove, "add_random_amount_listener", "remove_random_amount_listener", self._emit_change)
        self._register_listener(groove, "add_timing_amount_listener", "remove_timing_amount_listener", self._emit_change)
        self._register_listener(groove, "add_velocity_amount_listener", "remove_velocity_amount_listener", self._emit_change)

    def _bind_device_listeners(self, device: Any) -> None:
        self._register_listener(device, "add_name_listener", "remove_name_listener", self._emit_change)
        self._register_listener(device, "add_is_active_listener", "remove_is_active_listener", self._emit_change)
        self._register_listener(
            device,
            "add_selected_preset_index_listener",
            "remove_selected_preset_index_listener",
            self._emit_change,
        )
        self._register_listener(
            device,
            "add_is_using_compare_preset_b_listener",
            "remove_is_using_compare_preset_b_listener",
            self._emit_change,
        )
        self._register_listener(device, "add_chains_listener", "remove_chains_listener", self._rebind_after_structure_change)
        self._register_listener(
            device,
            "add_return_chains_listener",
            "remove_return_chains_listener",
            self._rebind_after_structure_change,
        )
        self._register_listener(
            device,
            "add_drum_pads_listener",
            "remove_drum_pads_listener",
            self._rebind_after_structure_change,
        )
        self._register_listener(
            device,
            "add_is_showing_chains_listener",
            "remove_is_showing_chains_listener",
            self._emit_change,
        )
        self._register_listener(
            device,
            "add_selected_variation_index_listener",
            "remove_selected_variation_index_listener",
            self._emit_change,
        )
        for parameter in self._device_parameters(device):
            self._bind_parameter_listener(parameter)
        for chain in self._rack_chains(device, "chains"):
            self._bind_chain_listeners(chain)
        for chain in self._rack_chains(device, "return_chains"):
            self._bind_chain_listeners(chain)
        for drum_pad in self._drum_pads(device):
            self._bind_drum_pad_listeners(drum_pad)

    def _bind_chain_listeners(self, chain: Any) -> None:
        self._register_listener(chain, "add_name_listener", "remove_name_listener", self._emit_change)
        self._register_listener(chain, "add_mute_listener", "remove_mute_listener", self._emit_change)
        self._register_listener(chain, "add_solo_listener", "remove_solo_listener", self._emit_change)
        self._register_listener(chain, "add_color_listener", "remove_color_listener", self._emit_change)
        self._register_listener(
            chain,
            "add_is_auto_colored_listener",
            "remove_is_auto_colored_listener",
            self._emit_change,
        )
        self._register_listener(chain, "add_in_note_listener", "remove_in_note_listener", self._emit_change)
        self._register_listener(chain, "add_out_note_listener", "remove_out_note_listener", self._emit_change)
        self._register_listener(chain, "add_choke_group_listener", "remove_choke_group_listener", self._emit_change)
        self._register_listener(chain, "add_devices_listener", "remove_devices_listener", self._rebind_after_structure_change)
        mixer_device = getattr(chain, "mixer_device", None)
        self._bind_parameter_listener(getattr(mixer_device, "chain_activator", None))
        self._bind_parameter_listener(getattr(mixer_device, "volume", None))
        self._bind_parameter_listener(getattr(mixer_device, "panning", None))
        for send in getattr(mixer_device, "sends", []):
            self._bind_parameter_listener(send)
        for device in self._device_chain_devices(chain, exclude_mixer=False):
            self._bind_device_listeners(device)

    def _bind_drum_pad_listeners(self, drum_pad: Any) -> None:
        self._register_listener(drum_pad, "add_mute_listener", "remove_mute_listener", self._emit_change)
        self._register_listener(drum_pad, "add_solo_listener", "remove_solo_listener", self._emit_change)
        self._register_listener(drum_pad, "add_chains_listener", "remove_chains_listener", self._rebind_after_structure_change)
        for chain in getattr(drum_pad, "chains", []):
            self._bind_chain_listeners(chain)

    def _register_listener(
        self,
        subject: Any,
        add_method_name: str,
        remove_method_name: str,
        callback: Callable[[], None],
    ) -> None:
        if subject is None or not hasattr(subject, add_method_name) or not hasattr(subject, remove_method_name):
            return

        add_listener = getattr(subject, add_method_name)
        remove_listener = getattr(subject, remove_method_name)

        def listener() -> None:
            callback()

        try:
            add_listener(listener)
        except Exception as error:
            self._log("Unable to add listener %s on %s: %s" % (add_method_name, type(subject).__name__, error))
            return
        self._listener_registrations.append((listener, lambda: self._safe_remove_listener(remove_listener, listener)))

    def _rebind_after_structure_change(self) -> None:
        if self._on_change is None:
            return
        self._pending_browser_device_loads.clear()
        on_change = self._on_change
        self.start_listening(on_change)
        self._emit_change()

    def _emit_change(self) -> None:
        if self._suppress_change_depth > 0:
            return
        if self._on_change is not None:
            self._on_change()

    def _begin_suppress_changes(self) -> None:
        self._suppress_change_depth += 1

    def _end_suppress_changes(self) -> None:
        self._suppress_change_depth = max(0, self._suppress_change_depth - 1)

    def _snapshot_track(self, track: Any, include_arm: bool, track_index: int) -> Dict[str, Any]:
        mixer_device = getattr(track, "mixer_device", None)
        snapshot = {
            "color": int(getattr(track, "color", 0)),
            "name": getattr(track, "name", ""),
            "mute": bool(getattr(track, "mute", False)),
            "solo": bool(getattr(track, "solo", False)),
            "back_to_arranger": bool(getattr(track, "back_to_arranger", False)),
            "crossfade_assign": int(getattr(mixer_device, "crossfade_assign", 1)),
            "panning_mode": int(getattr(mixer_device, "panning_mode", 0)),
            "track_activator": self._parameter_value(getattr(mixer_device, "track_activator", None)),
            "volume": self._parameter_value(getattr(mixer_device, "volume", None)),
            "panning": self._parameter_value(getattr(mixer_device, "panning", None)),
            "sends": [
                self._parameter_value(parameter)
                for parameter in getattr(mixer_device, "sends", [])
            ],
            "clip_slots": [self._snapshot_clip_slot(slot) for slot in getattr(track, "clip_slots", [])],
            "devices": self._snapshot_device_chain(track, exclude_mixer=True),
            "arrangement_clips": [
                self._snapshot_arrangement_clip(clip, track_index, arrangement_index)
                for arrangement_index, clip in enumerate(self._arrangement_clips(track))
            ],
        }
        if include_arm and getattr(track, "can_be_armed", False):
            snapshot["arm"] = bool(getattr(track, "arm", False))
        return snapshot

    def _snapshot_master_track(self, track: Any) -> Dict[str, Any]:
        if track is None:
            return {}
        mixer_device = getattr(track, "mixer_device", None)
        return {
            "cue_volume": self._parameter_value(getattr(mixer_device, "cue_volume", None)),
            "crossfader": self._parameter_value(getattr(mixer_device, "crossfader", None)),
            "volume": self._parameter_value(getattr(mixer_device, "volume", None)),
            "panning": self._parameter_value(getattr(mixer_device, "panning", None)),
            "sends": [
                self._parameter_value(parameter)
                for parameter in getattr(mixer_device, "sends", [])
            ],
            "devices": self._snapshot_device_chain(track, exclude_mixer=True),
        }

    def _snapshot_clip_slot(self, clip_slot: Any) -> Dict[str, Any]:
        has_clip = bool(getattr(clip_slot, "has_clip", False))
        clip = getattr(clip_slot, "clip", None) if has_clip else None
        return {
            "has_clip": has_clip,
            "has_stop_button": bool(getattr(clip_slot, "has_stop_button", True)),
            "clip": self._snapshot_clip(clip, clip_slot) if clip is not None else None,
        }

    def _snapshot_clip(self, clip: Any, clip_slot: Any) -> Dict[str, Any]:
        relative_path = None
        if bool(getattr(clip, "is_audio_clip", False)):
            clip_path = self._clip_file_path(clip)
            if clip_path:
                relative_path = self._relative_media_path_for_clip_slot(clip_slot, clip_path)
        return self._snapshot_clip_common(clip, relative_path, include_timeline_geometry=False)

    def _snapshot_arrangement_clip(self, clip: Any, track_index: int, arrangement_index: int) -> Dict[str, Any]:
        relative_path = None
        if bool(getattr(clip, "is_audio_clip", False)):
            clip_path = self._clip_file_path(clip)
            if clip_path:
                relative_path = self._relative_media_path(self._project_root_path(), clip_path, track_index, arrangement_index)

        start_time = self._safe_number(getattr(clip, "start_time", 0.0))
        end_time = self._safe_number(getattr(clip, "end_time", start_time))
        length = max(0.0, end_time - start_time)
        snapshot = self._snapshot_clip_common(clip, relative_path, include_timeline_geometry=True)
        snapshot["start_time"] = start_time
        snapshot["end_time"] = end_time
        snapshot["length"] = length
        return snapshot

    def _snapshot_clip_common(
        self,
        clip: Any,
        relative_path: Optional[str],
        include_timeline_geometry: bool,
    ) -> Dict[str, Any]:
        groove_index = self._clip_groove_index(clip)
        snapshot = {
            "name": getattr(clip, "name", ""),
            "color": int(getattr(clip, "color", 0)),
            "color_index": int(getattr(clip, "color_index", -1)),
            "muted": bool(getattr(clip, "muted", False)),
            "looping": bool(getattr(clip, "looping", False)),
            "loop_start": self._safe_number(getattr(clip, "loop_start", 0.0)),
            "loop_end": self._safe_number(getattr(clip, "loop_end", getattr(clip, "length", 0.0))),
            "start_marker": self._safe_number(getattr(clip, "start_marker", 0.0)),
            "end_marker": self._safe_number(getattr(clip, "end_marker", getattr(clip, "length", 0.0))),
            "signature_numerator": int(getattr(clip, "signature_numerator", 4)),
            "signature_denominator": int(getattr(clip, "signature_denominator", 4)),
            "legato": bool(getattr(clip, "legato", False)),
            "launch_mode": int(getattr(clip, "launch_mode", 0)),
            "launch_quantization": int(getattr(clip, "launch_quantization", 0)),
            "velocity_amount": self._safe_number(getattr(clip, "velocity_amount", 0.0)),
            "length": self._safe_number(getattr(clip, "length", 0.0)),
            "is_audio_clip": bool(getattr(clip, "is_audio_clip", False)),
            "is_midi_clip": bool(getattr(clip, "is_midi_clip", False)),
            "media_relative_path": relative_path,
            "groove_index": groove_index,
            "has_envelopes": bool(getattr(clip, "has_envelopes", False)),
        }

        if include_timeline_geometry:
            snapshot["start_time"] = self._safe_number(getattr(clip, "start_time", 0.0))
            snapshot["end_time"] = self._safe_number(getattr(clip, "end_time", snapshot["start_time"]))

        view_snapshot = self._snapshot_clip_view(clip)
        if view_snapshot is not None:
            snapshot["view"] = view_snapshot

        if snapshot["is_midi_clip"]:
            snapshot["notes"] = self._snapshot_clip_notes(clip)

        if snapshot["is_audio_clip"]:
            snapshot["gain"] = self._safe_number(getattr(clip, "gain", 0.85))
            snapshot["pitch_coarse"] = int(getattr(clip, "pitch_coarse", 0))
            snapshot["pitch_fine"] = int(getattr(clip, "pitch_fine", 0))
            snapshot["warping"] = bool(getattr(clip, "warping", False))
            snapshot["warp_mode"] = int(getattr(clip, "warp_mode", 0))
            snapshot["ram_mode"] = bool(getattr(clip, "ram_mode", False))

        return snapshot

    def _snapshot_clip_view(self, clip: Any) -> Optional[Dict[str, Any]]:
        clip_view = getattr(clip, "view", None)
        if clip_view is None:
            return None
        snapshot: Dict[str, Any] = {}
        if hasattr(clip_view, "grid_quantization"):
            snapshot["grid_quantization"] = int(getattr(clip_view, "grid_quantization", 0))
        if hasattr(clip_view, "grid_is_triplet"):
            snapshot["grid_is_triplet"] = bool(getattr(clip_view, "grid_is_triplet", False))
        return snapshot or None

    def _snapshot_groove_pool(self, song: Any) -> List[Dict[str, Any]]:
        return [self._snapshot_groove(groove) for groove in self._groove_pool_grooves(song)]

    def _snapshot_groove(self, groove: Any) -> Dict[str, Any]:
        return {
            "name": getattr(groove, "name", ""),
            "base": int(getattr(groove, "base", 0)),
            "quantization_amount": self._safe_number(getattr(groove, "quantization_amount", 0.0)),
            "random_amount": self._safe_number(getattr(groove, "random_amount", 0.0)),
            "timing_amount": self._safe_number(getattr(groove, "timing_amount", 0.0)),
            "velocity_amount": self._safe_number(getattr(groove, "velocity_amount", 0.0)),
        }

    def _groove_pool_grooves(self, song: Any) -> List[Any]:
        groove_pool = getattr(song, "groove_pool", None)
        try:
            grooves = getattr(groove_pool, "grooves", [])
        except Exception as error:
            self._log("Unable to enumerate groove pool: %s" % error)
            return []
        try:
            return list(grooves)
        except Exception as error:
            self._log("Unable to list groove pool contents: %s" % error)
            return []

    def _clip_groove_index(self, clip: Any) -> Optional[int]:
        if clip is None or not bool(getattr(clip, "has_groove", False)):
            return None
        song = self._song_provider()
        clip_groove = getattr(clip, "groove", None)
        for groove_index, groove in enumerate(self._groove_pool_grooves(song)):
            if groove is clip_groove or groove == clip_groove:
                return groove_index
        clip_snapshot = self._snapshot_groove(clip_groove) if clip_groove is not None else None
        for groove_index, groove in enumerate(self._groove_pool_grooves(song)):
            if clip_snapshot == self._snapshot_groove(groove):
                return groove_index
        return None

    def _snapshot_clip_notes(self, clip: Any) -> List[Dict[str, Any]]:
        return self._clip_notes_payload(clip, include_note_ids=False)

    def _snapshot_device_chain(self, container: Any, exclude_mixer: bool) -> List[Dict[str, Any]]:
        return [
            self._snapshot_device(device)
            for device in self._device_chain_devices(container, exclude_mixer=exclude_mixer)
        ]

    def _snapshot_device(self, device: Any) -> Dict[str, Any]:
        snapshot = {
            "name": getattr(device, "name", ""),
            "class_name": str(getattr(device, "class_name", "")),
            "class_display_name": str(getattr(device, "class_display_name", "")),
            "type": int(getattr(device, "type", 0)),
            "is_active": bool(getattr(device, "is_active", True)),
            "parameters": [
                self._snapshot_device_parameter(parameter)
                for parameter in self._device_parameters(device)
            ],
        }
        if hasattr(device, "selected_preset_index"):
            snapshot["selected_preset_index"] = int(getattr(device, "selected_preset_index", -1))
        if hasattr(device, "is_using_compare_preset_b"):
            snapshot["is_using_compare_preset_b"] = bool(getattr(device, "is_using_compare_preset_b", False))
        if hasattr(device, "is_showing_chains"):
            snapshot["is_showing_chains"] = bool(getattr(device, "is_showing_chains", False))
        if hasattr(device, "selected_variation_index"):
            snapshot["selected_variation_index"] = int(getattr(device, "selected_variation_index", -1))

        chains = self._rack_chains(device, "chains")
        if chains:
            snapshot["chains"] = [self._snapshot_chain(chain) for chain in chains]

        return_chains = self._rack_chains(device, "return_chains")
        if return_chains:
            snapshot["return_chains"] = [self._snapshot_chain(chain) for chain in return_chains]

        drum_pad_snapshots = [
            self._snapshot_drum_pad(drum_pad)
            for drum_pad in self._drum_pads(device)
            if self._should_include_drum_pad(drum_pad)
        ]
        if drum_pad_snapshots:
            snapshot["drum_pads"] = drum_pad_snapshots
        return snapshot

    def _snapshot_chain(self, chain: Any) -> Dict[str, Any]:
        mixer_device = getattr(chain, "mixer_device", None)
        snapshot = {
            "name": getattr(chain, "name", ""),
            "mute": bool(getattr(chain, "mute", False)),
            "solo": bool(getattr(chain, "solo", False)),
            "color": int(getattr(chain, "color", 0)),
            "is_auto_colored": bool(getattr(chain, "is_auto_colored", False)),
            "devices": self._snapshot_device_chain(chain, exclude_mixer=False),
            "mixer": {
                "chain_activator": self._parameter_value(getattr(mixer_device, "chain_activator", None)),
                "volume": self._parameter_value(getattr(mixer_device, "volume", None)),
                "panning": self._parameter_value(getattr(mixer_device, "panning", None)),
                "sends": [
                    self._parameter_value(parameter)
                    for parameter in getattr(mixer_device, "sends", [])
                ],
            },
        }
        if hasattr(chain, "in_note"):
            snapshot["in_note"] = int(getattr(chain, "in_note", 0))
        if hasattr(chain, "out_note"):
            snapshot["out_note"] = int(getattr(chain, "out_note", 0))
        if hasattr(chain, "choke_group"):
            snapshot["choke_group"] = int(getattr(chain, "choke_group", 0))
        return snapshot

    def _snapshot_drum_pad(self, drum_pad: Any) -> Dict[str, Any]:
        return {
            "note": int(getattr(drum_pad, "note", 0)),
            "mute": bool(getattr(drum_pad, "mute", False)),
            "solo": bool(getattr(drum_pad, "solo", False)),
            "chains": [self._snapshot_chain(chain) for chain in getattr(drum_pad, "chains", [])],
        }

    def _should_include_drum_pad(self, drum_pad: Any) -> bool:
        chains = list(getattr(drum_pad, "chains", []))
        if chains:
            return True
        return bool(getattr(drum_pad, "mute", False) or getattr(drum_pad, "solo", False))

    def _snapshot_device_parameter(self, parameter: Any) -> Dict[str, Any]:
        return {
            "name": getattr(parameter, "name", ""),
            "original_name": getattr(parameter, "original_name", getattr(parameter, "name", "")),
            "value": self._safe_number(getattr(parameter, "value", 0.0)),
            "is_enabled": bool(getattr(parameter, "is_enabled", True)),
            "state": int(getattr(parameter, "state", 0)),
            "min": self._safe_number(getattr(parameter, "min", 0.0)),
            "max": self._safe_number(getattr(parameter, "max", 1.0)),
            "is_quantized": bool(getattr(parameter, "is_quantized", False)),
        }

    def _clip_notes_payload(self, clip: Any, include_note_ids: bool) -> List[Dict[str, Any]]:
        if clip is None or not bool(getattr(clip, "is_midi_clip", False)):
            return []

        get_all_notes_extended = getattr(clip, "get_all_notes_extended", None)
        if not callable(get_all_notes_extended):
            return []

        try:
            payload = get_all_notes_extended()
        except Exception as error:
            self._log("Unable to read clip notes: %s" % error)
            return []

        notes = payload.get("notes", []) if isinstance(payload, dict) else payload
        if not isinstance(notes, (list, tuple)):
            return []

        normalized = [
            self._normalize_note_payload(note, include_note_id=include_note_ids)
            for note in notes
            if isinstance(note, dict)
        ]
        normalized.sort(
            key=lambda note: (
                self._safe_number(note.get("start_time", 0.0)),
                int(note.get("pitch", 0)),
                self._safe_number(note.get("duration", 0.0)),
                int(note.get("velocity", 0)),
                int(note.get("release_velocity", 0)),
                self._safe_number(note.get("probability", 1.0)),
                self._safe_number(note.get("velocity_deviation", 0.0)),
                bool(note.get("mute", False)),
            )
        )
        return normalized

    def _normalize_note_payload(self, note: Dict[str, Any], include_note_id: bool) -> Dict[str, Any]:
        normalized = {
            "pitch": int(note.get("pitch", 60)),
            "start_time": self._safe_number(note.get("start_time", 0.0)),
            "duration": max(0.0, self._safe_number(note.get("duration", 0.0))),
            "velocity": int(note.get("velocity", 100)),
            "mute": bool(note.get("mute", False)),
            "probability": self._safe_number(note.get("probability", 1.0)),
            "velocity_deviation": self._safe_number(note.get("velocity_deviation", 0.0)),
            "release_velocity": int(note.get("release_velocity", 64)),
        }
        if include_note_id and "note_id" in note:
            normalized["note_id"] = int(note["note_id"])
        return normalized

    def _apply_track_segments(self, track: Any, segments: List[str], value: Any) -> None:
        if not segments:
            return

        head = segments[0]
        if head == "color":
            if hasattr(track, "color"):
                setattr(track, "color", int(value))
            return
        if head == "name":
            setattr(track, "name", str(value))
            return
        if head == "mute":
            setattr(track, "mute", bool(value))
            return
        if head == "solo":
            setattr(track, "solo", bool(value))
            return
        if head == "back_to_arranger":
            if hasattr(track, "back_to_arranger"):
                setattr(track, "back_to_arranger", bool(value))
            return
        if head == "arm" and getattr(track, "can_be_armed", False):
            setattr(track, "arm", bool(value))
            return
        if head == "crossfade_assign":
            mixer_device = getattr(track, "mixer_device", None)
            if hasattr(mixer_device, "crossfade_assign"):
                setattr(mixer_device, "crossfade_assign", int(value))
            return
        if head == "panning_mode":
            mixer_device = getattr(track, "mixer_device", None)
            if hasattr(mixer_device, "panning_mode"):
                setattr(mixer_device, "panning_mode", int(value))
            return
        if head == "track_activator":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "track_activator", None), value)
            return
        if head == "volume":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "volume", None), value)
            return
        if head == "panning":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "panning", None), value)
            return
        if head == "sends" and len(segments) == 2:
            send = self._resolve_list_item(getattr(getattr(track, "mixer_device", None), "sends", []), segments[1])
            self._set_parameter(send, value)
            return
        if head == "clip_slots" and len(segments) >= 2:
            clip_slot = self._resolve_list_item(getattr(track, "clip_slots", []), segments[1])
            if clip_slot is None:
                return
            if len(segments) == 3 and segments[2] == "has_clip":
                if not bool(value):
                    self._delete_clip(clip_slot)
                return
            if len(segments) == 3 and segments[2] == "has_stop_button":
                if hasattr(clip_slot, "has_stop_button"):
                    setattr(clip_slot, "has_stop_button", bool(value))
                return
            if len(segments) == 3 and segments[2] == "clip":
                self._apply_clip_value(track, clip_slot, value, segments[1])
                return
            if len(segments) < 4 or segments[2] != "clip":
                return

            clip = getattr(clip_slot, "clip", None)
            if segments[3] == "name":
                if clip is None:
                    return
                setattr(clip, "name", str(value))
                return
            if segments[3] == "color":
                if clip is None:
                    return
                setattr(clip, "color", int(value))
                return
            if segments[3] == "color_index":
                if clip is None or not hasattr(clip, "color_index"):
                    return
                setattr(clip, "color_index", int(value))
                return
            if segments[3] == "muted":
                if clip is None:
                    return
                setattr(clip, "muted", bool(value))
                return
            if segments[3] == "notes":
                if clip is None:
                    return
                self._replace_clip_notes(clip, value)
                return
            if segments[3] == "groove_index":
                if clip is None:
                    return
                self._apply_clip_groove_index(clip, value)
                return
            if segments[3] == "has_envelopes":
                if clip is None:
                    return
                self._apply_shared_clip_property(clip, "has_envelopes", value)
                return
            if segments[3] == "media_relative_path":
                self._apply_clip_media_path(track, clip_slot, str(value), segments[1])
                return
            if segments[3] == "is_audio_clip":
                return
            if segments[3] == "is_midi_clip":
                return
            if segments[3] == "view":
                if clip is None:
                    return
                if len(segments) == 4 and isinstance(value, dict):
                    self._apply_clip_view_properties(clip, value)
                    return
                if len(segments) >= 5:
                    self._apply_clip_view_property(clip, segments[4], value)
                return
            if clip is None:
                return
            if self._apply_shared_clip_property(clip, segments[3], value):
                return

        self._log("Unsupported track path: %s" % "/".join(segments))

    def _apply_master_segments(self, track: Any, segments: List[str], value: Any) -> None:
        if track is None or not segments:
            return
        if segments[0] == "cue_volume":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "cue_volume", None), value)
            return
        if segments[0] == "crossfader":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "crossfader", None), value)
            return
        if segments[0] == "volume":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "volume", None), value)
            return
        if segments[0] == "panning":
            self._set_parameter(getattr(getattr(track, "mixer_device", None), "panning", None), value)
            return
        if segments[0] == "sends" and len(segments) == 2:
            send = self._resolve_list_item(getattr(getattr(track, "mixer_device", None), "sends", []), segments[1])
            self._set_parameter(send, value)
            return
        self._log("Unsupported master path: %s" % "/".join(segments))

    def _apply_groove_pool_segments(self, song: Any, segments: List[str], value: Any) -> None:
        if song is None:
            return
        if not segments:
            self._apply_groove_pool_snapshot(song, value)
            return
        groove = self._resolve_list_item(self._groove_pool_grooves(song), segments[0])
        if groove is None:
            return
        if len(segments) == 1 and isinstance(value, dict):
            for field_name, field_value in value.items():
                self._apply_groove_property(groove, field_name, field_value)
            return
        if len(segments) >= 2:
            self._apply_groove_property(groove, segments[1], value)

    def _set_parameter(self, parameter: Any, value: Any) -> None:
        if parameter is None or value is None:
            return
        if hasattr(parameter, "value"):
            parameter.value = float(value)

    def _parameter_value(self, parameter: Any) -> float:
        if parameter is None:
            return 0.0
        return self._safe_number(getattr(parameter, "value", 0.0))

    def _resolve_list_item(self, items: Any, index_text: str) -> Any:
        try:
            index = int(index_text)
        except (TypeError, ValueError):
            return None
        if index < 0 or index >= len(items):
            return None
        return items[index]

    def _safe_number(self, value: Any) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _project_root_path(self) -> Optional[Path]:
        if self._configured_project_root is not None:
            return self._configured_project_root

        song = self._song_provider()
        song_path = getattr(song, "file_path", "")
        if not song_path:
            return None
        try:
            return Path(song_path).expanduser().resolve().parent
        except Exception:
            return None

    def _clip_file_path(self, clip: Any) -> Optional[str]:
        raw_path = getattr(clip, "file_path", None)
        if raw_path:
            return str(raw_path)
        return None

    def _relative_media_path_for_clip_slot(self, clip_slot: Any, absolute_path: str) -> str:
        song = self._song_provider()
        for track_index, track in enumerate(getattr(song, "tracks", [])):
            for slot_index, candidate_slot in enumerate(getattr(track, "clip_slots", [])):
                if candidate_slot is clip_slot:
                    return self._relative_media_path(self._project_root_path(), absolute_path, track_index, slot_index)
        return self._relative_media_path(self._project_root_path(), absolute_path, 0, 0)

    def _relative_media_path(
        self,
        project_root: Optional[Path],
        absolute_path: str,
        track_index: int,
        slot_index: int,
    ) -> str:
        source_path = Path(absolute_path).expanduser()
        if project_root is not None:
            try:
                return source_path.resolve().relative_to(project_root).as_posix()
            except Exception:
                pass
        return "Samples/Imported/track_%s_slot_%s_%s" % (
            track_index,
            slot_index,
            source_path.name,
        )

    def _append_media_reference(
        self,
        references: List[Dict[str, Any]],
        reference_id: str,
        lom_path: str,
        relative_path: str,
        absolute_path: str,
        role: str,
    ) -> None:
        references.append(
            {
                "reference_id": reference_id,
                "lom_path": lom_path,
                "relative_path": relative_path,
                "absolute_path": absolute_path,
                "role": role,
            }
        )

        analysis_path = self._analysis_sidecar_path(absolute_path)
        if analysis_path is None:
            return
        references.append(
            {
                "reference_id": reference_id + "/analysis",
                "lom_path": lom_path + "/analysis",
                "relative_path": relative_path + ".asd",
                "absolute_path": analysis_path,
                "role": role + "_analysis",
            }
        )

    def _apply_clip_value(self, track: Any, clip_slot: Any, value: Any, slot_index_text: str) -> None:
        if value is None:
            self._delete_clip(clip_slot)
            return
        if not isinstance(value, dict):
            return

        clip = getattr(clip_slot, "clip", None)
        current_snapshot = self._snapshot_clip(clip, clip_slot) if clip is not None else None
        if clip is None or not self._can_update_session_clip_in_place(current_snapshot, value):
            if clip is not None:
                self._delete_clip(clip_slot)
            clip = self._create_session_clip(clip_slot, value, slot_index_text)
        if clip is None:
            return
        self._apply_clip_properties(clip, value)

    def _apply_clip_media_path(self, track: Any, clip_slot: Any, relative_path: str, slot_index_text: str) -> None:
        clip = getattr(clip_slot, "clip", None)
        if clip is not None and bool(getattr(clip, "is_audio_clip", False)):
            return
        self._create_audio_clip(clip_slot, relative_path, slot_index_text)

    def _create_session_clip(self, clip_slot: Any, clip_spec: Any, slot_index_text: str) -> Any:
        if not isinstance(clip_spec, dict):
            return None

        if bool(clip_spec.get("is_audio_clip")) and clip_spec.get("media_relative_path"):
            return self._create_audio_clip(
                clip_slot,
                str(clip_spec.get("media_relative_path")),
                slot_index_text,
            )

        if bool(clip_spec.get("is_midi_clip")):
            create_clip = getattr(clip_slot, "create_clip", None)
            if not callable(create_clip):
                self._log("Clip slot does not support create_clip.")
                return None
            try:
                create_clip(self._clip_creation_length(clip_spec))
            except Exception as error:
                self._log("Unable to create MIDI clip in slot %s: %s" % (slot_index_text, error))
                return None
            return getattr(clip_slot, "clip", None)

        return None

    def _create_audio_clip(self, clip_slot: Any, relative_path: str, slot_index_text: str) -> Any:
        project_root = self._project_root_path()
        if project_root is None:
            self._log("Cannot create remote audio clip because project root is unknown.")
            return None

        source_path = project_root / relative_path
        if not source_path.exists():
            self._log("Audio file not downloaded yet for clip slot %s: %s" % (slot_index_text, source_path))
            return None

        create_audio_clip = getattr(clip_slot, "create_audio_clip", None)
        if not callable(create_audio_clip):
            self._log("Clip slot does not support create_audio_clip.")
            return None

        try:
            create_audio_clip(str(source_path))
        except Exception as error:
            self._log("Unable to create audio clip from %s: %s" % (source_path, error))
            return None
        return getattr(clip_slot, "clip", None)

    def _delete_clip(self, clip_slot: Any) -> None:
        if clip_slot is None or not bool(getattr(clip_slot, "has_clip", False)):
            return
        delete_clip = getattr(clip_slot, "delete_clip", None)
        if callable(delete_clip):
            try:
                delete_clip()
            except Exception as error:
                self._log("Unable to delete clip: %s" % error)

    def _analysis_sidecar_path(self, absolute_path: str) -> Optional[str]:
        sidecar_path = Path(absolute_path + ".asd")
        if sidecar_path.exists():
            return str(sidecar_path)
        return None

    def _apply_clip_properties(self, clip: Any, clip_spec: Any) -> None:
        if clip is None or not isinstance(clip_spec, dict):
            return
        for field_name in ("name", "color", "color_index", "muted"):
            if field_name in clip_spec:
                self._apply_shared_clip_property(clip, field_name, clip_spec[field_name])
        for field_name in (
            "looping",
            "loop_start",
            "loop_end",
            "start_marker",
            "end_marker",
            "signature_numerator",
            "signature_denominator",
            "legato",
            "launch_mode",
            "launch_quantization",
            "velocity_amount",
            "gain",
            "pitch_coarse",
            "pitch_fine",
            "warping",
            "warp_mode",
            "ram_mode",
            "has_envelopes",
        ):
            if field_name in clip_spec:
                self._apply_shared_clip_property(clip, field_name, clip_spec[field_name])

        if "groove_index" in clip_spec:
            self._apply_clip_groove_index(clip, clip_spec["groove_index"])

        if "view" in clip_spec and isinstance(clip_spec["view"], dict):
            self._apply_clip_view_properties(clip, clip_spec["view"])

        if "notes" in clip_spec:
            self._replace_clip_notes(clip, clip_spec["notes"])

    def _apply_shared_clip_property(self, clip: Any, field_name: str, value: Any) -> bool:
        if clip is None:
            return False
        if field_name == "name":
            setattr(clip, "name", str(value))
            return True
        if field_name == "color":
            setattr(clip, "color", int(value))
            return True
        if field_name == "color_index" and hasattr(clip, "color_index"):
            setattr(clip, "color_index", int(value))
            return True
        if field_name == "muted":
            setattr(clip, "muted", bool(value))
            return True
        if field_name == "looping" and hasattr(clip, "looping"):
            setattr(clip, "looping", bool(value))
            return True
        if field_name == "loop_start" and hasattr(clip, "loop_start"):
            setattr(clip, "loop_start", self._safe_number(value))
            return True
        if field_name == "loop_end" and hasattr(clip, "loop_end"):
            setattr(clip, "loop_end", self._safe_number(value))
            return True
        if field_name == "start_marker" and hasattr(clip, "start_marker"):
            setattr(clip, "start_marker", self._safe_number(value))
            return True
        if field_name == "end_marker" and hasattr(clip, "end_marker"):
            setattr(clip, "end_marker", self._safe_number(value))
            return True
        if field_name == "signature_numerator" and hasattr(clip, "signature_numerator"):
            setattr(clip, "signature_numerator", int(value))
            return True
        if field_name == "signature_denominator" and hasattr(clip, "signature_denominator"):
            setattr(clip, "signature_denominator", int(value))
            return True
        if field_name == "legato" and hasattr(clip, "legato"):
            setattr(clip, "legato", bool(value))
            return True
        if field_name == "launch_mode" and hasattr(clip, "launch_mode"):
            setattr(clip, "launch_mode", int(value))
            return True
        if field_name == "launch_quantization" and hasattr(clip, "launch_quantization"):
            setattr(clip, "launch_quantization", int(value))
            return True
        if field_name == "velocity_amount" and hasattr(clip, "velocity_amount"):
            setattr(clip, "velocity_amount", self._safe_number(value))
            return True
        if field_name == "gain" and hasattr(clip, "gain"):
            setattr(clip, "gain", self._safe_number(value))
            return True
        if field_name == "pitch_coarse" and hasattr(clip, "pitch_coarse"):
            setattr(clip, "pitch_coarse", int(value))
            return True
        if field_name == "pitch_fine" and hasattr(clip, "pitch_fine"):
            setattr(clip, "pitch_fine", int(value))
            return True
        if field_name == "warping" and hasattr(clip, "warping"):
            setattr(clip, "warping", bool(value))
            return True
        if field_name == "warp_mode" and hasattr(clip, "warp_mode"):
            setattr(clip, "warp_mode", int(value))
            return True
        if field_name == "ram_mode" and hasattr(clip, "ram_mode"):
            setattr(clip, "ram_mode", bool(value))
            return True
        if field_name == "has_envelopes":
            target_has_envelopes = bool(value)
            current_has_envelopes = bool(getattr(clip, "has_envelopes", False))
            if target_has_envelopes == current_has_envelopes:
                return True
            if not target_has_envelopes:
                clear_all_envelopes = getattr(clip, "clear_all_envelopes", None)
                if callable(clear_all_envelopes):
                    clear_all_envelopes()
                return True
            self._log("Cannot reconstruct automation envelope payloads for clip %s via the public API." % getattr(clip, "name", ""))
            return True
        return False

    def _apply_clip_view_properties(self, clip: Any, view_spec: Any) -> None:
        if clip is None or not isinstance(view_spec, dict):
            return
        for field_name, field_value in view_spec.items():
            self._apply_clip_view_property(clip, field_name, field_value)

    def _apply_clip_view_property(self, clip: Any, field_name: str, value: Any) -> bool:
        clip_view = getattr(clip, "view", None)
        if clip_view is None:
            return False
        if field_name == "grid_quantization" and hasattr(clip_view, "grid_quantization"):
            setattr(clip_view, "grid_quantization", int(value))
            return True
        if field_name == "grid_is_triplet" and hasattr(clip_view, "grid_is_triplet"):
            setattr(clip_view, "grid_is_triplet", bool(value))
            return True
        return False

    def _apply_clip_groove_index(self, clip: Any, groove_index: Any) -> None:
        song = self._song_provider()
        grooves = self._groove_pool_grooves(song)
        target_groove = None
        if groove_index is not None:
            try:
                groove_slot = int(groove_index)
            except (TypeError, ValueError):
                groove_slot = -1
            if 0 <= groove_slot < len(grooves):
                target_groove = grooves[groove_slot]
            else:
                self._log("Groove index %s is not available in the local groove pool." % groove_index)
                return
        try:
            setattr(clip, "groove", target_groove)
        except Exception as error:
            self._log("Unable to set groove on clip %s: %s" % (getattr(clip, "name", ""), error))

    def _apply_groove_pool_snapshot(self, song: Any, groove_pool_state: Any) -> None:
        if not isinstance(groove_pool_state, list):
            return
        grooves = self._groove_pool_grooves(song)
        overlap = min(len(grooves), len(groove_pool_state))
        for groove_index in range(overlap):
            groove_state = groove_pool_state[groove_index]
            if not isinstance(groove_state, dict):
                continue
            for field_name, field_value in groove_state.items():
                self._apply_groove_property(grooves[groove_index], field_name, field_value)
        if len(grooves) != len(groove_pool_state):
            self._log(
                "Groove pool size differs locally (%s) vs remote (%s); structure sync is best-effort only."
                % (len(grooves), len(groove_pool_state))
            )

    def _apply_groove_property(self, groove: Any, field_name: str, value: Any) -> None:
        if groove is None:
            return
        try:
            if field_name == "name" and hasattr(groove, "name"):
                setattr(groove, "name", str(value))
            elif field_name == "base" and hasattr(groove, "base"):
                setattr(groove, "base", int(value))
            elif field_name in ("quantization_amount", "random_amount", "timing_amount", "velocity_amount") and hasattr(groove, field_name):
                setattr(groove, field_name, self._safe_number(value))
        except Exception as error:
            self._log("Unable to set groove %s on %s: %s" % (field_name, getattr(groove, "name", ""), error))

    def _replace_clip_notes(self, clip: Any, notes_value: Any) -> None:
        if clip is None or not bool(getattr(clip, "is_midi_clip", False)) or not isinstance(notes_value, list):
            return

        desired_notes = [
            self._normalize_note_payload(note, include_note_id=False)
            for note in notes_value
            if isinstance(note, dict)
        ]
        current_notes = self._snapshot_clip_notes(clip)
        if current_notes == desired_notes:
            return

        remove_notes_by_id = getattr(clip, "remove_notes_by_id", None)
        existing_notes = self._clip_notes_payload(clip, include_note_ids=True)
        removed_existing = False
        if callable(remove_notes_by_id):
            note_ids = [int(note["note_id"]) for note in existing_notes if "note_id" in note]
            if note_ids:
                try:
                    remove_notes_by_id(note_ids)
                    removed_existing = True
                except Exception as error:
                    self._log("Unable to remove clip notes: %s" % error)
                    return

        if not removed_existing and existing_notes:
            remove_notes_extended = getattr(clip, "remove_notes_extended", None)
            if callable(remove_notes_extended):
                min_pitch = min(int(note.get("pitch", 0)) for note in existing_notes)
                max_pitch = max(int(note.get("pitch", 127)) for note in existing_notes)
                start_time = min(self._safe_number(note.get("start_time", 0.0)) for note in existing_notes)
                end_time = max(
                    self._safe_number(note.get("start_time", 0.0)) + self._safe_number(note.get("duration", 0.0))
                    for note in existing_notes
                )
                try:
                    remove_notes_extended(
                        min_pitch,
                        max((max_pitch - min_pitch) + 1, 1),
                        start_time,
                        max(end_time - start_time, 0.25),
                    )
                except Exception as error:
                    self._log("Unable to clear clip notes by range: %s" % error)
                    return

        add_new_notes = getattr(clip, "add_new_notes", None)
        if callable(add_new_notes) and desired_notes:
            try:
                add_new_notes({"notes": self._strip_note_ids(desired_notes)})
            except Exception as error:
                self._log("Unable to add clip notes: %s" % error)

    def _strip_note_ids(self, notes: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [
            {
                key: value
                for key, value in note.items()
                if key != "note_id"
            }
            for note in notes
        ]

    def _clip_creation_length(self, clip_spec: Dict[str, Any]) -> float:
        explicit_length = self._safe_number(clip_spec.get("length", 0.0))
        loop_length = self._safe_number(clip_spec.get("loop_end", 0.0)) - self._safe_number(clip_spec.get("loop_start", 0.0))
        marker_length = self._safe_number(clip_spec.get("end_marker", 0.0)) - self._safe_number(clip_spec.get("start_marker", 0.0))
        return max(explicit_length, loop_length, marker_length, 0.25)

    def _reconcile_session_snapshot(self, snapshot_state: Any) -> None:
        song = self._song_provider()
        target_tracks = snapshot_state.get("tracks", []) if isinstance(snapshot_state, dict) else []
        live_tracks = list(getattr(song, "tracks", []))
        for track_index, track in enumerate(live_tracks):
            if track_index >= len(target_tracks):
                continue
            target_track = target_tracks[track_index]
            target_slots = target_track.get("clip_slots", []) if isinstance(target_track, dict) else []
            self._reconcile_track_clip_slots(track, target_slots)

    def _reconcile_track_clip_slots(self, track: Any, target_slots: Any) -> None:
        desired_slots = target_slots if isinstance(target_slots, list) else []
        for slot_index, clip_slot in enumerate(getattr(track, "clip_slots", [])):
            if slot_index >= len(desired_slots):
                continue
            self._reconcile_clip_slot(clip_slot, desired_slots[slot_index], slot_index)

    def _reconcile_clip_slot(self, clip_slot: Any, target_slot: Any, slot_index: int) -> None:
        if not isinstance(target_slot, dict):
            return

        desired_has_clip = bool(target_slot.get("has_clip", False))
        desired_clip = target_slot.get("clip") if desired_has_clip else None
        current_clip = getattr(clip_slot, "clip", None) if getattr(clip_slot, "has_clip", False) else None

        if not desired_has_clip or not isinstance(desired_clip, dict):
            self._delete_clip(clip_slot)
            return

        current_snapshot = self._snapshot_clip(current_clip, clip_slot) if current_clip is not None else None
        if current_snapshot == desired_clip:
            return

        if current_clip is None or not self._can_update_session_clip_in_place(current_snapshot, desired_clip):
            if current_clip is not None:
                self._delete_clip(clip_slot)
            current_clip = self._create_session_clip(clip_slot, desired_clip, str(slot_index))
        if current_clip is None:
            return
        self._apply_clip_properties(current_clip, desired_clip)

    def _can_update_session_clip_in_place(self, current_snapshot: Any, desired_clip: Any) -> bool:
        if not isinstance(current_snapshot, dict) or not isinstance(desired_clip, dict):
            return False
        if bool(current_snapshot.get("is_audio_clip")) != bool(desired_clip.get("is_audio_clip")):
            return False
        if bool(current_snapshot.get("is_midi_clip")) != bool(desired_clip.get("is_midi_clip")):
            return False
        if bool(desired_clip.get("is_audio_clip")):
            return current_snapshot.get("media_relative_path") == desired_clip.get("media_relative_path")
        return True

    def _reconcile_arrangement_snapshot(self, snapshot_state: Any) -> None:
        song = self._song_provider()
        target_tracks = snapshot_state.get("tracks", []) if isinstance(snapshot_state, dict) else []
        live_tracks = list(getattr(song, "tracks", []))
        for track_index, track in enumerate(live_tracks):
            if track_index >= len(target_tracks):
                continue
            target_track = target_tracks[track_index]
            target_clips = target_track.get("arrangement_clips", []) if isinstance(target_track, dict) else []
            self._reconcile_track_arrangement(track, target_clips, track_index)

    def _reconcile_device_snapshot(self, snapshot_state: Any) -> None:
        song = self._song_provider()
        target_tracks = snapshot_state.get("tracks", []) if isinstance(snapshot_state, dict) else []
        for track_index, track in enumerate(getattr(song, "tracks", [])):
            if track_index >= len(target_tracks):
                continue
            target_track = target_tracks[track_index]
            desired_devices = target_track.get("devices", []) if isinstance(target_track, dict) else []
            self._reconcile_device_chain(track, desired_devices, "track %s" % track_index, exclude_mixer=True)

        target_return_tracks = snapshot_state.get("return_tracks", []) if isinstance(snapshot_state, dict) else []
        for track_index, track in enumerate(getattr(song, "return_tracks", [])):
            if track_index >= len(target_return_tracks):
                continue
            target_track = target_return_tracks[track_index]
            desired_devices = target_track.get("devices", []) if isinstance(target_track, dict) else []
            self._reconcile_device_chain(track, desired_devices, "return track %s" % track_index, exclude_mixer=True)

        target_master = snapshot_state.get("master_track", {}) if isinstance(snapshot_state, dict) else {}
        desired_master_devices = target_master.get("devices", []) if isinstance(target_master, dict) else []
        self._reconcile_device_chain(getattr(song, "master_track", None), desired_master_devices, "master track", exclude_mixer=True)

    def _reconcile_device_chain(
        self,
        container: Any,
        target_devices: Any,
        label: str,
        exclude_mixer: bool,
    ) -> None:
        if container is None:
            return
        desired_devices = target_devices if isinstance(target_devices, list) else []
        current_devices = self._device_chain_devices(container, exclude_mixer=exclude_mixer)
        current_state = [self._snapshot_device(device) for device in current_devices]
        if current_state == desired_devices:
            return
        if not self._supports_device_chain_mutation(container):
            self._reconcile_device_properties_only(current_devices, desired_devices, label)
            return

        for device_index, desired_device in enumerate(desired_devices):
            current_devices = self._device_chain_devices(container, exclude_mixer=exclude_mixer)
            current_device = current_devices[device_index] if device_index < len(current_devices) else None
            if current_device is None or not self._can_reuse_device(current_device, desired_device):
                if current_device is not None:
                    self._delete_device_at_index(container, device_index, exclude_mixer, label)
                self._insert_device_at_index(container, desired_device, device_index, exclude_mixer, label)
                current_devices = self._device_chain_devices(container, exclude_mixer=exclude_mixer)
                current_device = current_devices[device_index] if device_index < len(current_devices) else None
            if current_device is None:
                continue
            self._apply_device_properties(current_device, desired_device)
            self._reconcile_device_contents(current_device, desired_device, "%s/device %s" % (label, device_index))

        current_devices = self._device_chain_devices(container, exclude_mixer=exclude_mixer)
        for device_index in range(len(current_devices) - 1, len(desired_devices) - 1, -1):
            self._delete_device_at_index(container, device_index, exclude_mixer, label)

    def _reconcile_device_properties_only(
        self,
        current_devices: List[Any],
        desired_devices: List[Any],
        label: str,
    ) -> None:
        overlapping_devices = min(len(current_devices), len(desired_devices))
        had_structure_mismatch = len(current_devices) != len(desired_devices)

        for device_index in range(overlapping_devices):
            current_device = current_devices[device_index]
            desired_device = desired_devices[device_index]
            if not self._can_reuse_device(current_device, desired_device):
                had_structure_mismatch = True
                continue
            self._apply_device_properties(current_device, desired_device)
            self._reconcile_device_contents(current_device, desired_device, "%s/device %s" % (label, device_index))

        if had_structure_mismatch:
            self._log_device_structure_unsupported(label)

    def _reconcile_device_contents(self, device: Any, desired_device: Any, label: str) -> None:
        if device is None or not isinstance(desired_device, dict):
            return
        self._reconcile_rack_chain_collection(
            device,
            desired_device.get("chains", []),
            label,
            collection_name="chains",
            allow_insert=callable(getattr(device, "insert_chain", None)),
            allow_delete=False,
        )
        self._reconcile_rack_chain_collection(
            device,
            desired_device.get("return_chains", []),
            label,
            collection_name="return_chains",
            allow_insert=False,
            allow_delete=False,
        )
        self._reconcile_drum_pad_collection(device, desired_device.get("drum_pads", []), label)

    def _reconcile_rack_chain_collection(
        self,
        device: Any,
        desired_chains: Any,
        label: str,
        collection_name: str,
        allow_insert: bool,
        allow_delete: bool,
    ) -> None:
        target_chains = desired_chains if isinstance(desired_chains, list) else []
        current_chains = self._rack_chains(device, collection_name)
        current_state = [self._snapshot_chain(chain) for chain in current_chains]
        if current_state == target_chains:
            return

        overlap = min(len(current_chains), len(target_chains))
        for chain_index in range(overlap):
            self._reconcile_chain(
                current_chains[chain_index],
                target_chains[chain_index],
                "%s/%s %s" % (label, collection_name, chain_index),
            )

        if len(target_chains) > len(current_chains):
            if not allow_insert:
                self._log("Cannot insert %s on %s via the public Live API." % (collection_name, label))
                return
            for chain_index in range(len(current_chains), len(target_chains)):
                chain = self._insert_chain_at_index(device, chain_index, label)
                if chain is None:
                    break
                self._reconcile_chain(
                    chain,
                    target_chains[chain_index],
                    "%s/%s %s" % (label, collection_name, chain_index),
                )

        if len(current_chains) > len(target_chains) and not allow_delete:
            self._log("Cannot delete %s on %s via the public Live API." % (collection_name, label))

    def _reconcile_chain(self, chain: Any, desired_chain: Any, label: str) -> None:
        if chain is None or not isinstance(desired_chain, dict):
            return

        desired_name = str(desired_chain.get("name", getattr(chain, "name", "")))
        if getattr(chain, "name", "") != desired_name and hasattr(chain, "name"):
            try:
                setattr(chain, "name", desired_name)
            except Exception as error:
                self._log("Unable to rename chain %s: %s" % (desired_name, error))

        for field_name in ("mute", "solo", "color", "is_auto_colored", "in_note", "out_note", "choke_group"):
            if field_name not in desired_chain or not hasattr(chain, field_name):
                continue
            desired_value = desired_chain[field_name]
            current_value = getattr(chain, field_name)
            if current_value == desired_value:
                continue
            try:
                setattr(chain, field_name, desired_value)
            except Exception as error:
                self._log("Unable to set %s on %s: %s" % (field_name, label, error))

        mixer_device = getattr(chain, "mixer_device", None)
        mixer_snapshot = desired_chain.get("mixer", {})
        if isinstance(mixer_snapshot, dict):
            self._set_parameter(getattr(mixer_device, "chain_activator", None), mixer_snapshot.get("chain_activator"))
            self._set_parameter(getattr(mixer_device, "volume", None), mixer_snapshot.get("volume"))
            self._set_parameter(getattr(mixer_device, "panning", None), mixer_snapshot.get("panning"))
            sends = mixer_snapshot.get("sends", [])
            if isinstance(sends, list):
                for send_index, send_value in enumerate(sends):
                    send = self._resolve_list_item(getattr(mixer_device, "sends", []), str(send_index))
                    self._set_parameter(send, send_value)

        self._reconcile_device_chain(chain, desired_chain.get("devices", []), label, exclude_mixer=False)

    def _reconcile_drum_pad_collection(self, device: Any, desired_pads: Any, label: str) -> None:
        target_pads = desired_pads if isinstance(desired_pads, list) else []
        if not target_pads:
            for drum_pad in self._drum_pads(device):
                if not list(getattr(drum_pad, "chains", [])):
                    continue
                delete_all_chains = getattr(drum_pad, "delete_all_chains", None)
                if callable(delete_all_chains):
                    try:
                        delete_all_chains()
                    except Exception as error:
                        self._log("Unable to delete drum-pad chains on %s: %s" % (label, error))
            return

        pad_by_note = {int(getattr(pad, "note", -1)): pad for pad in self._drum_pads(device)}
        for pad_spec in target_pads:
            if not isinstance(pad_spec, dict):
                continue
            note = int(pad_spec.get("note", -1))
            drum_pad = pad_by_note.get(note)
            if drum_pad is None:
                continue
            for field_name in ("mute", "solo"):
                if field_name not in pad_spec or not hasattr(drum_pad, field_name):
                    continue
                desired_value = bool(pad_spec[field_name])
                if getattr(drum_pad, field_name) == desired_value:
                    continue
                try:
                    setattr(drum_pad, field_name, desired_value)
                except Exception as error:
                    self._log("Unable to set %s on drum pad %s in %s: %s" % (field_name, note, label, error))
            self._reconcile_drum_pad_chains(device, drum_pad, pad_spec.get("chains", []), "%s/drum_pad %s" % (label, note))

    def _reconcile_drum_pad_chains(self, device: Any, drum_pad: Any, desired_chains: Any, label: str) -> None:
        target_chains = desired_chains if isinstance(desired_chains, list) else []
        current_chains = list(getattr(drum_pad, "chains", []))
        current_state = [self._snapshot_chain(chain) for chain in current_chains]
        if current_state == target_chains:
            return

        if len(current_chains) > len(target_chains):
            delete_all_chains = getattr(drum_pad, "delete_all_chains", None)
            if callable(delete_all_chains):
                try:
                    delete_all_chains()
                except Exception as error:
                    self._log("Unable to reset drum-pad chains on %s: %s" % (label, error))
                current_chains = []
            else:
                self._log("Cannot delete drum-pad chains on %s via the public Live API." % label)
                return

        while len(current_chains) < len(target_chains):
            new_chain = self._insert_chain_for_drum_pad(device, drum_pad, label)
            if new_chain is None:
                return
            current_chains = list(getattr(drum_pad, "chains", []))

        for chain_index, desired_chain in enumerate(target_chains):
            current_chain = current_chains[chain_index] if chain_index < len(current_chains) else None
            if current_chain is None:
                continue
            self._reconcile_chain(current_chain, desired_chain, "%s/chain %s" % (label, chain_index))

    def _reconcile_track_arrangement(self, track: Any, target_clips: Any, track_index: int) -> None:
        desired_clips = target_clips if isinstance(target_clips, list) else []
        current_clips = list(self._arrangement_clips(track))
        current_state = [
            self._snapshot_arrangement_clip(clip, track_index, arrangement_index)
            for arrangement_index, clip in enumerate(current_clips)
        ]
        if current_state == desired_clips:
            return

        if self._can_update_arrangement_in_place(current_state, desired_clips):
            for clip, clip_spec in zip(current_clips, desired_clips):
                self._apply_arrangement_clip_properties(clip, clip_spec)
            return

        delete_clip = getattr(track, "delete_clip", None)
        if callable(delete_clip):
            for clip in reversed(current_clips):
                try:
                    delete_clip(clip)
                except Exception as error:
                    self._log("Unable to delete arrangement clip: %s" % error)

        for clip_spec in desired_clips:
            created_clip = self._create_arrangement_clip(track, clip_spec)
            if created_clip is None:
                continue
            self._apply_arrangement_clip_properties(created_clip, clip_spec)

    def _can_update_arrangement_in_place(self, current_state: Any, desired_clips: Any) -> bool:
        if not isinstance(current_state, list) or not isinstance(desired_clips, list):
            return False
        if len(current_state) != len(desired_clips):
            return False
        for current_clip, desired_clip in zip(current_state, desired_clips):
            if not isinstance(current_clip, dict) or not isinstance(desired_clip, dict):
                return False
            if bool(current_clip.get("is_audio_clip")) != bool(desired_clip.get("is_audio_clip")):
                return False
            if bool(current_clip.get("is_midi_clip")) != bool(desired_clip.get("is_midi_clip")):
                return False
            if bool(desired_clip.get("is_audio_clip")) and current_clip.get("media_relative_path") != desired_clip.get("media_relative_path"):
                return False
            for field_name in ("start_time", "end_time", "length"):
                if not self._numbers_close(current_clip.get(field_name), desired_clip.get(field_name)):
                    return False
        return True

    def _create_arrangement_clip(self, track: Any, clip_spec: Any) -> Any:
        if not isinstance(clip_spec, dict):
            return None

        before = list(self._arrangement_clips(track))
        start_time = self._safe_number(clip_spec.get("start_time", 0.0))
        if bool(clip_spec.get("is_audio_clip")) and clip_spec.get("media_relative_path"):
            project_root = self._project_root_path()
            if project_root is None:
                self._log("Cannot create arrangement audio clip because project root is unknown.")
                return None
            source_path = project_root / str(clip_spec["media_relative_path"])
            if not source_path.exists():
                self._log("Arrangement audio file not downloaded yet: %s" % source_path)
                return None
            create_audio_clip = getattr(track, "create_audio_clip", None)
            if not callable(create_audio_clip):
                self._log("Track does not support create_audio_clip.")
                return None
            try:
                create_audio_clip(str(source_path), start_time)
            except Exception as error:
                self._log("Unable to create arrangement audio clip: %s" % error)
                return None
            return self._find_new_arrangement_clip(track, before)

        if bool(clip_spec.get("is_midi_clip")):
            create_midi_clip = getattr(track, "create_midi_clip", None)
            if not callable(create_midi_clip):
                self._log("Track does not support create_midi_clip.")
                return None
            length = max(self._safe_number(clip_spec.get("length", 0.0)), 0.25)
            try:
                create_midi_clip(start_time, length)
            except Exception as error:
                self._log("Unable to create arrangement MIDI clip: %s" % error)
                return None
            return self._find_new_arrangement_clip(track, before)

        return None

    def _find_new_arrangement_clip(self, track: Any, before: List[Any]) -> Any:
        before_ids = {id(clip) for clip in before}
        for clip in self._arrangement_clips(track):
            if id(clip) not in before_ids:
                return clip
        clips = list(self._arrangement_clips(track))
        return clips[-1] if clips else None

    def _apply_arrangement_clip_properties(self, clip: Any, clip_spec: Any) -> None:
        if clip is None or not isinstance(clip_spec, dict):
            return
        self._apply_clip_properties(clip, clip_spec)

    def _can_reuse_device(self, device: Any, desired_device: Any) -> bool:
        if not isinstance(desired_device, dict):
            return False
        return (
            str(getattr(device, "class_name", "")) == str(desired_device.get("class_name", ""))
            and str(getattr(device, "class_display_name", "")) == str(desired_device.get("class_display_name", ""))
        )

    def _supports_device_chain_mutation(self, container: Any) -> bool:
        return callable(getattr(container, "insert_device", None)) and callable(getattr(container, "delete_device", None))

    def _unsupported_device_structure_label(self, path: str) -> Optional[str]:
        song = self._song_provider()
        segments = decode_pointer(path)
        if len(segments) >= 3 and segments[0] == "tracks" and segments[2] == "devices":
            track = self._resolve_list_item(getattr(song, "tracks", []), segments[1])
            if track is None or self._supports_device_chain_mutation(track):
                return None
            if self._is_device_structure_remainder(segments[3:]):
                return "track %s" % segments[1]
            return None
        if len(segments) >= 3 and segments[0] == "return_tracks" and segments[2] == "devices":
            track = self._resolve_list_item(getattr(song, "return_tracks", []), segments[1])
            if track is None or self._supports_device_chain_mutation(track):
                return None
            if self._is_device_structure_remainder(segments[3:]):
                return "return track %s" % segments[1]
            return None
        if len(segments) >= 2 and segments[0] == "master_track" and segments[1] == "devices":
            track = getattr(song, "master_track", None)
            if track is None or self._supports_device_chain_mutation(track):
                return None
            if self._is_device_structure_remainder(segments[2:]):
                return "master track"
        return None

    def _is_device_structure_remainder(self, remainder: List[str]) -> bool:
        if not remainder:
            return True
        if not remainder[0].isdigit():
            return True
        if len(remainder) == 1:
            return True
        return remainder[1] in ("class_name", "class_display_name", "type")

    def _should_skip_unsupported_plugin_structure(
        self,
        operation: Any,
        previous_state: Any,
        current_state: Any,
    ) -> bool:
        if getattr(operation, "kind", None) != "set":
            return False
        if "/devices" not in getattr(operation, "path", ""):
            return False
        segments = decode_pointer(operation.path)
        if "devices" not in segments:
            return False
        device_index = segments.index("devices")
        if not self._is_device_structure_remainder(segments[device_index + 1 :]):
            return False
        previous_value = get_json_value(previous_state, operation.path) if previous_state is not None else None
        current_value = get_json_value(current_state, operation.path) if current_state is not None else None
        previous_has_non_instantiable = self._contains_non_instantiable_device(previous_value)
        current_has_non_instantiable = (
            self._contains_non_instantiable_device(current_value)
            or self._contains_non_instantiable_device(getattr(operation, "value", None))
        )
        return previous_has_non_instantiable and not current_has_non_instantiable

    def _contains_non_instantiable_device(self, value: Any) -> bool:
        if isinstance(value, list):
            return any(self._contains_non_instantiable_device(item) for item in value)
        if not isinstance(value, dict):
            return False
        class_name = str(value.get("class_name", ""))
        if class_name == "PluginDevice" or class_name.startswith("MxDevice"):
            return True
        for collection_name in ("devices", "chains", "return_chains", "drum_pads"):
            nested_value = value.get(collection_name)
            if self._contains_non_instantiable_device(nested_value):
                return True
        return False

    def _log_device_structure_unsupported(self, label: str) -> None:
        if label in self._logged_device_structure_warnings:
            return
        self._logged_device_structure_warnings.add(label)
        self._log(
            "Skipping device-chain structure sync on %s because this Live build does not expose insert_device/delete_device."
            % label
        )

    def _rack_chains(self, device: Any, collection_name: str) -> List[Any]:
        try:
            chains = getattr(device, collection_name, [])
        except Exception as error:
            self._log("Unable to enumerate %s on %s: %s" % (collection_name, type(device).__name__, error))
            return []
        try:
            return list(chains)
        except Exception as error:
            self._log("Unable to list %s on %s: %s" % (collection_name, type(device).__name__, error))
            return []

    def _drum_pads(self, device: Any) -> List[Any]:
        try:
            drum_pads = getattr(device, "drum_pads", [])
        except Exception:
            return []
        try:
            return list(drum_pads)
        except Exception as error:
            self._log("Unable to list drum pads on %s: %s" % (type(device).__name__, error))
            return []

    def _device_chain_devices(self, container: Any, exclude_mixer: bool) -> List[Any]:
        try:
            devices = list(getattr(container, "devices", []))
        except Exception as error:
            self._log("Unable to enumerate devices on %s: %s" % (type(container).__name__, error))
            return []
        if not exclude_mixer:
            return devices
        mixer_device = getattr(container, "mixer_device", None)
        return [device for device in devices if device is not mixer_device]

    def _device_parameters(self, device: Any) -> List[Any]:
        try:
            return list(getattr(device, "parameters", []))
        except Exception as error:
            self._log("Unable to enumerate parameters on %s: %s" % (type(device).__name__, error))
            return []

    def _apply_device_properties(self, device: Any, desired_device: Any) -> None:
        if device is None or not isinstance(desired_device, dict):
            return

        desired_name = str(desired_device.get("name", getattr(device, "name", "")))
        if hasattr(device, "name") and getattr(device, "name", "") != desired_name:
            try:
                setattr(device, "name", desired_name)
            except Exception as error:
                self._log("Unable to rename device %s: %s" % (desired_name, error))

        if "selected_preset_index" in desired_device and hasattr(device, "selected_preset_index"):
            try:
                setattr(device, "selected_preset_index", int(desired_device["selected_preset_index"]))
            except Exception as error:
                self._log("Unable to set selected_preset_index on %s: %s" % (desired_name, error))

        if "is_using_compare_preset_b" in desired_device and hasattr(device, "is_using_compare_preset_b"):
            try:
                setattr(device, "is_using_compare_preset_b", bool(desired_device["is_using_compare_preset_b"]))
            except Exception:
                pass
        if "is_showing_chains" in desired_device and hasattr(device, "is_showing_chains"):
            try:
                setattr(device, "is_showing_chains", bool(desired_device["is_showing_chains"]))
            except Exception:
                pass
        if "selected_variation_index" in desired_device and hasattr(device, "selected_variation_index"):
            try:
                setattr(device, "selected_variation_index", int(desired_device["selected_variation_index"]))
            except Exception:
                pass

        desired_parameters = desired_device.get("parameters", [])
        current_parameters = self._device_parameters(device)
        for parameter_index, desired_parameter in enumerate(desired_parameters):
            if parameter_index >= len(current_parameters) or not isinstance(desired_parameter, dict):
                continue
            parameter = current_parameters[parameter_index]
            expected_name = str(desired_parameter.get("original_name", ""))
            current_name = str(getattr(parameter, "original_name", getattr(parameter, "name", "")))
            if expected_name and expected_name != current_name:
                continue
            if not bool(desired_parameter.get("is_enabled", True)):
                continue
            if int(getattr(parameter, "state", 0)) == 2 or not hasattr(parameter, "value"):
                continue
            target_value = self._safe_number(desired_parameter.get("value", getattr(parameter, "value", 0.0)))
            if self._numbers_close(getattr(parameter, "value", 0.0), target_value):
                continue
            try:
                parameter.value = target_value
            except Exception as error:
                self._log("Unable to set parameter %s on %s: %s" % (current_name, desired_name, error))

    def _insert_chain_at_index(self, device: Any, chain_index: int, label: str) -> Any:
        insert_chain = getattr(device, "insert_chain", None)
        if not callable(insert_chain):
            self._log("Device %s does not support insert_chain." % label)
            return None
        before = {id(chain) for chain in self._rack_chains(device, "chains")}
        try:
            insert_chain(chain_index)
        except TypeError:
            try:
                insert_chain()
            except Exception as error:
                self._log("Unable to insert chain on %s: %s" % (label, error))
                return None
        except Exception as error:
            self._log("Unable to insert chain on %s: %s" % (label, error))
            return None

        for chain in self._rack_chains(device, "chains"):
            if id(chain) not in before:
                return chain
        chains = self._rack_chains(device, "chains")
        if chain_index < len(chains):
            return chains[chain_index]
        return chains[-1] if chains else None

    def _insert_chain_for_drum_pad(self, device: Any, drum_pad: Any, label: str) -> Any:
        chain = self._insert_chain_at_index(device, len(self._rack_chains(device, "chains")), label)
        if chain is None:
            return None
        if hasattr(chain, "in_note") and hasattr(drum_pad, "note"):
            try:
                setattr(chain, "in_note", int(getattr(drum_pad, "note", 0)))
            except Exception as error:
                self._log("Unable to retarget drum-pad chain on %s: %s" % (label, error))
        drum_pad_chains = list(getattr(drum_pad, "chains", []))
        if chain in drum_pad_chains:
            return chain
        return drum_pad_chains[-1] if drum_pad_chains else chain

    def _application(self) -> Any:
        provider = self._application_provider
        if provider is None:
            return None
        if callable(provider):
            try:
                return provider()
            except TypeError:
                return provider
        return provider

    def _browser_load_target_track(self, container: Any) -> Any:
        song = self._song_provider()
        if container in getattr(song, "tracks", []):
            return container
        if container in getattr(song, "return_tracks", []):
            return container
        if container is getattr(song, "master_track", None):
            return container
        return None

    def _try_load_non_native_device(self, container: Any, desired_device: Any, device_index: int, label: str) -> bool:
        if not isinstance(desired_device, dict):
            return False
        target_track = self._browser_load_target_track(container)
        if target_track is None:
            self._log("Browser loading is only supported for top-level track chains right now: %s." % label)
            return False

        device_name = str(desired_device.get("class_display_name", "") or desired_device.get("name", ""))
        if not device_name:
            return False
        pending_key = (label, device_name, device_index)
        if pending_key in self._pending_browser_device_loads:
            return True

        application = self._application()
        browser = getattr(application, "browser", None) if application is not None else None
        load_item = getattr(browser, "load_item", None)
        if browser is None or not callable(load_item):
            return False

        browser_item = self._find_browser_item(browser, desired_device)
        if browser_item is None:
            self._log("Unable to find browser item for %s on %s." % (device_name, label))
            return False

        song = self._song_provider()
        song_view = getattr(song, "view", None)
        previous_selected_track = getattr(song_view, "selected_track", None) if song_view is not None else None
        try:
            if song_view is not None and hasattr(song_view, "selected_track"):
                song_view.selected_track = target_track
            load_item(browser_item)
            self._pending_browser_device_loads.add(pending_key)
            self._log("Triggered browser load for %s on %s." % (device_name, label))
            return True
        except Exception as error:
            self._log("Unable to browser-load %s on %s: %s" % (device_name, label, error))
            return False
        finally:
            if song_view is not None and previous_selected_track is not None:
                try:
                    song_view.selected_track = previous_selected_track
                except Exception:
                    pass

    def _find_browser_item(self, browser: Any, desired_device: Any) -> Any:
        desired_name = str(desired_device.get("class_display_name", "") or desired_device.get("name", "")).strip()
        if not desired_name:
            return None
        desired_name_lower = desired_name.lower()

        roots = []
        class_name = str(desired_device.get("class_name", ""))
        preferred_attrs = ["plugins"] if class_name == "PluginDevice" else []
        preferred_attrs += ["max_for_live", "max", "audio_effects", "midi_effects", "instruments"]
        seen_root_ids = set()
        for attribute_name in preferred_attrs:
            candidate = getattr(browser, attribute_name, None)
            if candidate is None or id(candidate) in seen_root_ids:
                continue
            roots.append(candidate)
            seen_root_ids.add(id(candidate))
        for attribute_name in dir(browser):
            if attribute_name.startswith("_"):
                continue
            candidate = getattr(browser, attribute_name, None)
            if candidate is None or callable(candidate) or id(candidate) in seen_root_ids:
                continue
            if not self._looks_like_browser_item(candidate):
                continue
            roots.append(candidate)
            seen_root_ids.add(id(candidate))

        best_partial = None
        visited = set()
        queue = list(roots)
        while queue:
            item = queue.pop(0)
            if isinstance(item, (list, tuple)):
                queue[0:0] = list(item)
                continue
            item_id = id(item)
            if item_id in visited:
                continue
            visited.add(item_id)
            item_name = str(getattr(item, "name", "")).strip()
            item_name_lower = item_name.lower()
            is_loadable = bool(getattr(item, "is_loadable", not bool(self._browser_children(item))))
            if item_name_lower == desired_name_lower and is_loadable:
                return item
            if best_partial is None and desired_name_lower and desired_name_lower in item_name_lower and is_loadable:
                best_partial = item
            queue.extend(self._browser_children(item))
        return best_partial

    def _looks_like_browser_item(self, candidate: Any) -> bool:
        if isinstance(candidate, (list, tuple)):
            return True
        return hasattr(candidate, "name") or bool(self._browser_children(candidate))

    def _browser_children(self, item: Any) -> List[Any]:
        if isinstance(item, (list, tuple)):
            return list(item)
        children = getattr(item, "children", None)
        if children is None:
            return []
        try:
            return list(children)
        except Exception:
            return []

    def _delete_device_at_index(self, container: Any, device_index: int, exclude_mixer: bool, label: str) -> None:
        delete_device = getattr(container, "delete_device", None)
        if not callable(delete_device):
            self._log("Container %s does not support delete_device." % label)
            return
        try:
            delete_device(device_index)
        except Exception as error:
            self._log("Unable to delete device %s on %s: %s" % (device_index, label, error))

    def _insert_device_at_index(self, container: Any, desired_device: Any, device_index: int, exclude_mixer: bool, label: str) -> None:
        if not isinstance(desired_device, dict):
            return
        class_name = str(desired_device.get("class_name", ""))
        device_name = str(desired_device.get("class_display_name", "") or desired_device.get("name", ""))
        if not device_name:
            return
        if class_name == "PluginDevice" or class_name.startswith("MxDevice"):
            if not self._try_load_non_native_device(container, desired_device, device_index, label):
                self._log("Cannot instantiate %s on %s via the public Live API." % (class_name or device_name, label))
            return
        insert_device = getattr(container, "insert_device", None)
        if not callable(insert_device):
            self._log("Container %s does not support insert_device." % label)
            return
        try:
            insert_device(device_name, device_index)
        except Exception as error:
            self._log("Unable to insert %s on %s: %s" % (device_name, label, error))

    def _numbers_close(self, left: Any, right: Any, epsilon: float = 1e-6) -> bool:
        return abs(self._safe_number(left) - self._safe_number(right)) <= epsilon

    def _arrangement_clips(self, track: Any) -> List[Any]:
        try:
            clips = getattr(track, "arrangement_clips", [])
        except Exception as error:
            self._log("Arrangement clips unavailable on %s: %s" % (type(track).__name__, error))
            return []
        try:
            return list(clips)
        except Exception as error:
            self._log("Unable to enumerate arrangement clips on %s: %s" % (type(track).__name__, error))
            return []

    def _safe_remove_listener(
        self,
        remove_listener: Callable[[Callable[[], None]], None],
        listener: Callable[[], None],
    ) -> None:
        try:
            remove_listener(listener)
        except Exception:
            pass

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)
