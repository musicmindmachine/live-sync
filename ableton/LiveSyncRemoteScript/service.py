from __future__ import annotations

import threading
from typing import Any, Callable, Optional

from .sync_core import apply_lww_operation, clone_json, decode_pointer, diff_states


class SyncService:
    def __init__(
        self,
        adapter: Any,
        client: Any,
        room_id: str,
        client_id: str,
        media_sync: Optional[Any] = None,
        schedule_main_thread: Optional[Callable[[Callable[[], None]], None]] = None,
        logger: Optional[Callable[[str], None]] = None,
        pull_limit: int = 200,
    ) -> None:
        self._adapter = adapter
        self._client = client
        self._room_id = room_id
        self._client_id = client_id
        self._media_sync = media_sync
        self._schedule_main_thread = schedule_main_thread or (lambda callback: callback())
        self._logger = logger or (lambda message: None)
        self._pull_limit = pull_limit
        self._shadow_state: Any = {}
        self._clock_state: Any = {}
        self._last_sequence = 0
        self._lamport = 0
        self._started = False
        self._watch_started = False
        self._listening_started = False
        self._pending_remote_sequence = 0
        self._pending_remote_refresh = False
        self._pending_local_change = False
        self._process_scheduled = False
        self._pending_lock = threading.Lock()

    def start(self) -> None:
        if self._started:
            return

        pull_result = self._client.pull_ops(self._room_id, after_sequence=0, limit=self._pull_limit)
        self._lamport = max(self._lamport, pull_result.max_lamport)
        if (
            pull_result.reset_required
            and pull_result.snapshot_state is not None
            and pull_result.clock_state is not None
        ):
            self._apply_snapshot(
                pull_result.snapshot_state,
                pull_result.clock_state,
                pull_result.snapshot_sequence,
                pull_result.max_lamport,
            )
            self._log("Joined room from compacted snapshot at sequence %s." % pull_result.snapshot_sequence)
        elif pull_result.ops:
            self._apply_remote_ops(pull_result.ops)
            self._last_sequence = pull_result.latest_sequence
            self._log("Joined existing room with %s operations." % len(pull_result.ops))
        else:
            current_state = self._adapter.capture_state()
            bootstrap_ops, self._lamport = diff_states({}, current_state, self._client_id, self._lamport)
            if bootstrap_ops:
                push_result = self._client.push_ops(self._room_id, self._client_id, bootstrap_ops)
                self._last_sequence = push_result.last_sequence
                self._apply_push_result(push_result, current_state)
            else:
                self._shadow_state = clone_json(current_state)
                self._clock_state = {}
            self._log("Published initial snapshot with %s operations." % len(bootstrap_ops))

        self._start_adapter_listening()
        self._start_watch_if_supported()
        self._start_media_sync_if_supported()
        self._refresh_media_references()
        self._started = True

    def shutdown(self) -> None:
        if self._listening_started:
            stop_listening = getattr(self._adapter, "stop_listening", None)
            if callable(stop_listening):
                stop_listening()
            self._listening_started = False

        stop = getattr(self._client, "stop", None)
        if callable(stop):
            stop()

        if self._media_sync is not None:
            shutdown_media = getattr(self._media_sync, "shutdown", None)
            if callable(shutdown_media):
                shutdown_media()

    def process_pending(self) -> dict:
        pushed = 0
        pulled = 0

        while True:
            with self._pending_lock:
                self._process_scheduled = False
                local_dirty = self._pending_local_change
                self._pending_local_change = False
                pending_remote_sequence = self._pending_remote_sequence
                pending_remote_refresh = self._pending_remote_refresh
                self._pending_remote_refresh = False

            if local_dirty:
                self._refresh_media_references()
                pushed += self._push_local_changes()

            if pending_remote_refresh or pending_remote_sequence > self._last_sequence:
                pulled += self._pull_remote_until_caught_up()

            with self._pending_lock:
                needs_more_work = (
                    self._pending_local_change
                    or self._pending_remote_refresh
                    or self._pending_remote_sequence > self._last_sequence
                )
                if not needs_more_work:
                    break
                if not self._process_scheduled:
                    self._process_scheduled = True
                    self._schedule_main_thread(self.process_pending)
                break

        return {
            "pulled": pulled,
            "pushed": pushed,
            "last_sequence": self._last_sequence,
            "lamport": self._lamport,
        }

    def request_local_sync(self) -> None:
        with self._pending_lock:
            self._pending_local_change = True
        self._schedule_process()

    def handle_media_ready(self) -> None:
        self._reconcile_adapter_to_state(self._shadow_state)

    def poll_local_state(self) -> bool:
        poll_for_clip_note_changes = getattr(self._adapter, "poll_for_clip_note_changes", None)
        if not callable(poll_for_clip_note_changes):
            return False
        try:
            has_changes = bool(poll_for_clip_note_changes())
        except Exception as error:
            self._log("Local poll failed: %s" % error)
            return False
        if has_changes:
            self.request_local_sync()
        return has_changes

    def _push_local_changes(self) -> int:
        current_state = self._adapter.capture_state()
        local_ops, self._lamport = diff_states(
            self._shadow_state,
            current_state,
            self._client_id,
            self._lamport,
        )
        filter_outbound_operations = getattr(self._adapter, "filter_outbound_operations", None)
        if callable(filter_outbound_operations):
            local_ops = filter_outbound_operations(
                local_ops,
                previous_state=self._shadow_state,
                current_state=current_state,
            )

        if not local_ops:
            return 0

        result = self._client.push_ops(self._room_id, self._client_id, local_ops)
        self._last_sequence = max(self._last_sequence, result.last_sequence)
        self._lamport = max(self._lamport, result.max_lamport)
        self._apply_push_result(result, current_state)
        return len([item for item in result.accepted if item.applied])

    def _pull_remote_once(self) -> int:
        pull_result = self._client.pull_ops(
            self._room_id,
            after_sequence=self._last_sequence,
            limit=self._pull_limit,
        )
        self._lamport = max(self._lamport, pull_result.max_lamport)
        if (
            pull_result.reset_required
            and pull_result.snapshot_state is not None
            and pull_result.clock_state is not None
        ):
            self._apply_snapshot(
                pull_result.snapshot_state,
                pull_result.clock_state,
                pull_result.snapshot_sequence,
                pull_result.max_lamport,
            )
            return 1
        if not pull_result.ops:
            return 0

        self._apply_remote_ops(pull_result.ops)
        self._last_sequence = max(self._last_sequence, pull_result.latest_sequence)
        return len(pull_result.ops)

    def _pull_remote_until_caught_up(self) -> int:
        pulled = 0
        while self._pending_target_sequence() > self._last_sequence:
            batch_count = self._pull_remote_once()
            if batch_count == 0:
                self._acknowledge_pending_sequence(self._last_sequence)
                break
            pulled += batch_count
        return pulled

    def _apply_remote_ops(self, operations: Any) -> None:
        requires_full_reconcile = False
        for operation in operations:
            self._lamport = max(self._lamport, operation.lamport)
            previous_state = self._shadow_state
            next_state, next_clock_state, applied = apply_lww_operation(
                self._shadow_state,
                self._clock_state,
                operation,
            )
            self._shadow_state = next_state
            self._clock_state = next_clock_state
            if not applied or operation.client_id == self._client_id or previous_state == next_state:
                continue
            if self._requires_full_reconcile(operation.path):
                requires_full_reconcile = True
                continue
            self._reconcile_adapter_to_state(next_state)

        if requires_full_reconcile:
            self._reconcile_adapter_to_state(self._shadow_state)

    def _apply_snapshot(self, snapshot_state: Any, clock_state: Any, snapshot_sequence: int, max_lamport: int) -> None:
        self._shadow_state = clone_json(snapshot_state)
        self._clock_state = clone_json(clock_state or {})
        self._last_sequence = max(self._last_sequence, snapshot_sequence)
        self._lamport = max(self._lamport, max_lamport)
        self._reconcile_adapter_to_state(snapshot_state)

    def _apply_push_result(self, push_result: Any, current_state: Any) -> None:
        self._shadow_state = clone_json(push_result.snapshot_state)
        self._clock_state = clone_json(push_result.clock_state)
        if current_state != push_result.snapshot_state:
            self._reconcile_adapter_to_state(push_result.snapshot_state)

    def _start_adapter_listening(self) -> None:
        start_listening = getattr(self._adapter, "start_listening", None)
        if not callable(start_listening) or self._listening_started:
            return
        start_listening(self._handle_local_change)
        self._listening_started = True

    def _start_watch_if_supported(self) -> None:
        start_room_watch = getattr(self._client, "start_room_watch", None)
        if not callable(start_room_watch) or self._watch_started:
            return
        start_room_watch(self._room_id, self._handle_remote_version)
        self._watch_started = True

    def _start_media_sync_if_supported(self) -> None:
        if self._media_sync is None:
            return
        start_media = getattr(self._media_sync, "start", None)
        if callable(start_media):
            start_media()

    def _handle_local_change(self) -> None:
        self.request_local_sync()

    def _handle_remote_version(self, payload: Any) -> None:
        with self._pending_lock:
            self._pending_remote_refresh = True
            latest_sequence = int(payload.get("latestSequence", 0))
            self._pending_remote_sequence = max(self._pending_remote_sequence, latest_sequence)
            self._lamport = max(self._lamport, int(payload.get("maxLamport", 0)))
        if self._media_sync is not None:
            note_remote_version = getattr(self._media_sync, "note_remote_version", None)
            if callable(note_remote_version):
                note_remote_version(int(payload.get("mediaVersion", 0)))
        self._schedule_process()

    def _schedule_process(self) -> None:
        should_schedule = False
        with self._pending_lock:
            if not self._process_scheduled:
                self._process_scheduled = True
                should_schedule = True
        if should_schedule:
            self._schedule_main_thread(self.process_pending)

    def _pending_target_sequence(self) -> int:
        with self._pending_lock:
            return self._pending_remote_sequence

    def _acknowledge_pending_sequence(self, sequence: int) -> None:
        with self._pending_lock:
            if self._pending_remote_sequence <= sequence:
                self._pending_remote_sequence = sequence

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)

    def _reconcile_adapter_to_state(self, target_state: Any) -> None:
        current_state = self._adapter.capture_state()
        if current_state == target_state:
            return

        apply_snapshot = getattr(self._adapter, "apply_snapshot", None)
        if callable(apply_snapshot):
            apply_snapshot(target_state)
            return

        snapshot_ops, _ = diff_states(current_state, target_state, "__reconcile__", 0)
        for operation in snapshot_ops:
            self._adapter.apply_operation(operation)

    def _refresh_media_references(self) -> None:
        if self._media_sync is None:
            return
        get_project_root = getattr(self._adapter, "get_project_root", None)
        set_project_root = getattr(self._media_sync, "set_project_root", None)
        if callable(get_project_root) and callable(set_project_root):
            set_project_root(get_project_root())
        capture_media_references = getattr(self._adapter, "capture_media_references", None)
        replace_local_references = getattr(self._media_sync, "replace_local_references", None)
        if not callable(capture_media_references) or not callable(replace_local_references):
            return
        replace_local_references(capture_media_references(), self._lamport)

    def _requires_full_reconcile(self, path: str) -> bool:
        if "/arrangement_clips" in path:
            return True
        if "/devices" in path or "/parameters" in path:
            return True
        if "/clip_slots/" not in path:
            return False
        segments = decode_pointer(path)
        if "clip_slots" not in segments:
            return False
        clip_slot_index = segments.index("clip_slots")
        remainder = segments[clip_slot_index + 2 :]
        if not remainder:
            return True
        if remainder[0] == "has_clip":
            return True
        return len(remainder) == 1 and remainder[0] == "clip"
