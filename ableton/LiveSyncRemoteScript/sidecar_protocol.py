from __future__ import annotations

from typing import Any, Dict, List

from .models import AcceptedOperation, Operation, PullResult, PushResult


def serialize_operation(operation: Operation) -> Dict[str, Any]:
    payload = operation.to_payload()
    if operation.sequence is not None:
        payload["sequence"] = int(operation.sequence)
    return payload


def deserialize_operation(payload: Dict[str, Any]) -> Operation:
    return Operation.from_payload(payload)


def serialize_push_result(result: PushResult) -> Dict[str, Any]:
    return {
        "roomId": result.room_id,
        "accepted": [
            {
                "opId": item.op_id,
                "sequence": item.sequence,
                "duplicate": item.duplicate,
                "applied": item.applied,
            }
            for item in result.accepted
        ],
        "lastSequence": int(result.last_sequence),
        "snapshotState": result.snapshot_state,
        "clockState": result.clock_state,
        "maxLamport": int(result.max_lamport),
    }


def deserialize_push_result(payload: Dict[str, Any]) -> PushResult:
    return PushResult(
        room_id=str(payload["roomId"]),
        accepted=[
            AcceptedOperation(
                op_id=str(item["opId"]),
                sequence=int(item["sequence"]) if item.get("sequence") is not None else None,
                duplicate=bool(item.get("duplicate", False)),
                applied=bool(item.get("applied", False)),
            )
            for item in payload.get("accepted", [])
        ],
        last_sequence=int(payload.get("lastSequence", 0)),
        snapshot_state=payload.get("snapshotState", {}),
        clock_state=payload.get("clockState", {}),
        max_lamport=int(payload.get("maxLamport", 0)),
    )


def serialize_pull_result(result: PullResult) -> Dict[str, Any]:
    return {
        "roomExists": bool(result.room_exists),
        "latestSequence": int(result.latest_sequence),
        "compactedThroughSequence": int(result.compacted_through_sequence),
        "resetRequired": bool(result.reset_required),
        "snapshotState": result.snapshot_state,
        "clockState": result.clock_state,
        "snapshotSequence": int(result.snapshot_sequence),
        "maxLamport": int(result.max_lamport),
        "ops": [serialize_operation(operation) for operation in result.ops],
    }


def deserialize_pull_result(payload: Dict[str, Any]) -> PullResult:
    return PullResult(
        room_exists=bool(payload.get("roomExists", False)),
        latest_sequence=int(payload.get("latestSequence", 0)),
        compacted_through_sequence=int(payload.get("compactedThroughSequence", 0)),
        reset_required=bool(payload.get("resetRequired", False)),
        snapshot_state=payload.get("snapshotState"),
        clock_state=payload.get("clockState"),
        snapshot_sequence=int(payload.get("snapshotSequence", 0)),
        max_lamport=int(payload.get("maxLamport", 0)),
        ops=[deserialize_operation(item) for item in payload.get("ops", [])],
    )


def serialize_watch_payload(event_counter: int, updated: bool, version: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "eventCounter": int(event_counter),
        "updated": bool(updated),
        "version": dict(version),
    }
