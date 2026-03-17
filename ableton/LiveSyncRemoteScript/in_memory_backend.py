from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Dict, List

from .models import AcceptedOperation, Operation, PullResult, PushResult
from .sync_core import apply_lww_operation, clone_json

COMPACTION_TRIGGER_OP_COUNT = 6
COMPACTION_RETAIN_OP_COUNT = 3
SNAPSHOT_FALLBACK_GAP = 5


class InMemorySyncBackend:
    def __init__(self) -> None:
        self._rooms: Dict[str, Dict[str, Any]] = {}
        self._watchers: Dict[str, List[Callable[[Dict[str, int]], None]]] = {}

    def push_ops(self, room_id: str, client_id: str, ops: List[Operation]) -> PushResult:
        room = self._rooms.setdefault(
            room_id,
            {
                "next_sequence": 1,
                "ops": [],
                "by_op_id": {},
                "state": {},
                "clock_state": {},
                "compacted_through_sequence": 0,
                "max_lamport": 0,
            },
        )
        next_sequence = int(room["next_sequence"])
        entries: List[Operation] = room["ops"]
        by_op_id: Dict[str, Operation] = room["by_op_id"]
        state = room["state"]
        clock_state = room["clock_state"]
        max_lamport = int(room["max_lamport"])
        accepted: List[AcceptedOperation] = []
        had_applied_ops = False

        for operation in ops:
            existing = by_op_id.get(operation.op_id)
            if existing is not None:
                accepted.append(
                    AcceptedOperation(
                        op_id=existing.op_id,
                        sequence=int(existing.sequence or 0),
                        duplicate=True,
                        applied=True,
                    )
                )
                continue

            candidate = replace(operation, client_id=client_id, sequence=None)
            max_lamport = max(max_lamport, int(candidate.lamport))
            next_state, next_clock_state, applied = apply_lww_operation(state, clock_state, candidate)
            if not applied:
                accepted.append(
                    AcceptedOperation(
                        op_id=candidate.op_id,
                        sequence=None,
                        duplicate=False,
                        applied=False,
                    )
                )
                continue

            stored = replace(candidate, sequence=next_sequence)
            next_sequence += 1
            entries.append(stored)
            by_op_id[stored.op_id] = stored
            state = next_state
            clock_state = next_clock_state
            had_applied_ops = True
            accepted.append(
                AcceptedOperation(
                    op_id=stored.op_id,
                    sequence=int(stored.sequence or 0),
                    duplicate=False,
                    applied=True,
                )
            )

        room["next_sequence"] = next_sequence
        room["state"] = state
        room["clock_state"] = clock_state
        room["max_lamport"] = max_lamport
        if had_applied_ops:
            self._compact_if_needed(room_id)
            self._notify_watchers(room_id)
        return PushResult(
            room_id=room_id,
            accepted=accepted,
            last_sequence=next_sequence - 1,
            snapshot_state=clone_json(state),
            clock_state=clone_json(clock_state),
            max_lamport=max_lamport,
        )

    def pull_ops(self, room_id: str, after_sequence: int, limit: int = 200) -> PullResult:
        room = self._rooms.get(room_id)
        if room is None:
            return PullResult(
                room_exists=False,
                latest_sequence=0,
                compacted_through_sequence=0,
                reset_required=False,
                snapshot_state=None,
                clock_state=None,
                snapshot_sequence=0,
                max_lamport=0,
                ops=[],
            )

        latest_sequence = int(room["next_sequence"]) - 1
        compacted_through_sequence = int(room["compacted_through_sequence"])
        if after_sequence < compacted_through_sequence or latest_sequence - after_sequence > SNAPSHOT_FALLBACK_GAP:
            return PullResult(
                room_exists=True,
                latest_sequence=latest_sequence,
                compacted_through_sequence=compacted_through_sequence,
                reset_required=True,
                snapshot_state=clone_json(room["state"]),
                clock_state=clone_json(room["clock_state"]),
                snapshot_sequence=latest_sequence,
                max_lamport=int(room["max_lamport"]),
                ops=[],
            )

        entries: List[Operation] = room["ops"]
        ops = [
            operation
            for operation in entries
            if operation.sequence is not None and operation.sequence > after_sequence
        ][:limit]

        return PullResult(
            room_exists=True,
            latest_sequence=latest_sequence,
            compacted_through_sequence=compacted_through_sequence,
            reset_required=False,
            snapshot_state=None,
            clock_state=None,
            snapshot_sequence=latest_sequence,
            max_lamport=int(room["max_lamport"]),
            ops=ops,
        )

    def start_room_watch(
        self,
        room_id: str,
        on_version: Callable[[Dict[str, int]], None],
    ) -> Callable[[], None]:
        watchers = self._watchers.setdefault(room_id, [])
        watchers.append(on_version)
        on_version(self._watch_payload(room_id))

        def unsubscribe() -> None:
            active_watchers = self._watchers.get(room_id, [])
            if on_version in active_watchers:
                active_watchers.remove(on_version)

        return unsubscribe

    def _compact_if_needed(self, room_id: str) -> None:
        room = self._rooms[room_id]
        entries: List[Operation] = room["ops"]
        if len(entries) <= COMPACTION_TRIGGER_OP_COUNT:
            return

        delete_count = len(entries) - COMPACTION_RETAIN_OP_COUNT
        if delete_count <= 0:
            return

        deleted = entries[:delete_count]
        room["ops"] = entries[delete_count:]
        room["compacted_through_sequence"] = max(
            int(room["compacted_through_sequence"]),
            int(deleted[-1].sequence or 0),
        )

    def _watch_payload(self, room_id: str) -> Dict[str, int]:
        room = self._rooms.get(room_id)
        if room is None:
            return {
                "latestSequence": 0,
                "compactedThroughSequence": 0,
                "updatedAt": 0,
            }
        return {
            "latestSequence": int(room["next_sequence"]) - 1,
            "compactedThroughSequence": int(room["compacted_through_sequence"]),
            "maxLamport": int(room["max_lamport"]),
            "updatedAt": int(room["next_sequence"]) - 1 + int(room["compacted_through_sequence"]),
        }

    def _notify_watchers(self, room_id: str) -> None:
        payload = self._watch_payload(room_id)
        for watcher in list(self._watchers.get(room_id, [])):
            watcher(payload)


class InMemoryConvexClient:
    def __init__(self, backend: InMemorySyncBackend) -> None:
        self._backend = backend
        self._unsubscribe_watch = None

    def push_ops(self, room_id: str, client_id: str, ops: List[Operation]) -> PushResult:
        return self._backend.push_ops(room_id, client_id, list(ops))

    def pull_ops(self, room_id: str, after_sequence: int, limit: int = 200) -> PullResult:
        return self._backend.pull_ops(room_id, after_sequence, limit)

    def start_room_watch(self, room_id: str, on_version: Callable[[Dict[str, int]], None]) -> None:
        if self._unsubscribe_watch is not None:
            self._unsubscribe_watch()
        self._unsubscribe_watch = self._backend.start_room_watch(room_id, on_version)

    def stop(self) -> None:
        if self._unsubscribe_watch is not None:
            self._unsubscribe_watch()
            self._unsubscribe_watch = None
