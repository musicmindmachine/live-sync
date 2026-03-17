from __future__ import annotations

import copy
import uuid
from typing import Any, Dict, List, Tuple

from .models import ClockState, JsonValue, Operation

_MISSING = object()


def clone_json(value: JsonValue) -> JsonValue:
    return copy.deepcopy(value)


def encode_pointer(segments: List[str]) -> str:
    if not segments:
        return ""
    escaped = [segment.replace("~", "~0").replace("/", "~1") for segment in segments]
    return "/" + "/".join(escaped)


def decode_pointer(path: str) -> List[str]:
    if path in ("", "/"):
        return []
    return [segment.replace("~1", "/").replace("~0", "~") for segment in path.lstrip("/").split("/")]


def diff_states(
    previous: Any,
    current: Any,
    client_id: str,
    lamport_start: int,
) -> Tuple[List[Operation], int]:
    lamport = lamport_start
    operations: List[Operation] = []

    def emit(kind: str, segments: List[str], value: Any = None) -> None:
        nonlocal lamport
        lamport += 1
        operations.append(
            Operation(
                op_id=uuid.uuid4().hex,
                client_id=client_id,
                lamport=lamport,
                kind=kind,
                path=encode_pointer(segments),
                value=clone_json(value) if kind == "set" else None,
            )
        )

    def walk(previous_value: Any, current_value: Any, segments: List[str]) -> None:
        if current_value is _MISSING:
            emit("delete", segments)
            return

        if previous_value is _MISSING:
            if isinstance(current_value, dict):
                if not current_value:
                    emit("set", segments, {})
                    return
                for key in sorted(current_value.keys()):
                    walk(_MISSING, current_value[key], segments + [str(key)])
                return

            if isinstance(current_value, list):
                emit("set", segments, current_value)
                return

            emit("set", segments, current_value)
            return

        if isinstance(previous_value, dict) and isinstance(current_value, dict):
            keys = sorted(set(previous_value.keys()) | set(current_value.keys()))
            for key in keys:
                walk(
                    previous_value.get(key, _MISSING),
                    current_value.get(key, _MISSING),
                    segments + [str(key)],
                )
            return

        if isinstance(previous_value, list) and isinstance(current_value, list):
            if len(previous_value) != len(current_value):
                emit("set", segments, current_value)
                return

            for index, (previous_item, current_item) in enumerate(zip(previous_value, current_value)):
                walk(previous_item, current_item, segments + [str(index)])
            return

        if previous_value != current_value:
            emit("set", segments, current_value)

    walk(previous, current, [])
    return operations, lamport


def apply_operation_to_state(state: Any, operation: Operation) -> JsonValue:
    if operation.kind == "set":
        return set_json_value(state, operation.path, operation.value)
    return delete_json_value(state, operation.path)


def compare_clocks(left: Dict[str, Any], right: Dict[str, Any]) -> int:
    left_key = (
        int(left.get("lamport", 0)),
        str(left.get("client_id", "")),
        str(left.get("op_id", "")),
    )
    right_key = (
        int(right.get("lamport", 0)),
        str(right.get("client_id", "")),
        str(right.get("op_id", "")),
    )
    if left_key < right_key:
        return -1
    if left_key > right_key:
        return 1
    return 0


def apply_lww_operation(
    state: Any,
    clock_state: ClockState,
    operation: Operation,
) -> Tuple[JsonValue, ClockState, bool]:
    next_clock_state = clone_json(clock_state or {})
    op_clock = {
        "lamport": int(operation.lamport),
        "client_id": operation.client_id,
        "op_id": operation.op_id,
        "kind": operation.kind,
    }

    winning_prefix_clock = None
    for prefix in pointer_prefixes(operation.path):
        existing_clock = next_clock_state.get(prefix)
        if existing_clock is None:
            continue
        if winning_prefix_clock is None or compare_clocks(existing_clock, winning_prefix_clock) > 0:
            winning_prefix_clock = existing_clock

    if winning_prefix_clock is not None and compare_clocks(winning_prefix_clock, op_clock) >= 0:
        return clone_json(state), next_clock_state, False

    preserved_descendants = []
    for path, clock in next_clock_state.items():
        if not is_descendant_path(path, operation.path):
            continue
        if compare_clocks(clock, op_clock) <= 0:
            continue
        preserved_descendants.append(
            {
                "path": path,
                "clock": clone_json(clock),
                "value": clone_json(get_json_value(state, path)) if clock.get("kind") == "set" else _MISSING,
            }
        )

    next_state = apply_operation_to_state(state, operation)

    for path in list(next_clock_state.keys()):
        if is_descendant_path(path, operation.path) and compare_clocks(next_clock_state[path], op_clock) <= 0:
            next_clock_state.pop(path, None)

    next_clock_state[operation.path] = clone_json(op_clock)

    for descendant in sorted(
        preserved_descendants,
        key=lambda item: (pointer_depth(item["path"]), item["path"]),
    ):
        path = str(descendant["path"])
        clock = descendant["clock"]
        if clock.get("kind") == "set":
            next_state = set_json_value(next_state, path, descendant["value"])
        else:
            next_state = delete_json_value(next_state, path)
        next_clock_state[path] = clone_json(clock)

    return next_state, next_clock_state, True


def pointer_prefixes(path: str) -> List[str]:
    segments = decode_pointer(path)
    if not segments:
        return [""]

    prefixes = [""]
    for index in range(1, len(segments) + 1):
        prefixes.append(encode_pointer(segments[:index]))
    return prefixes


def is_descendant_path(candidate: str, parent: str) -> bool:
    if parent in ("", "/"):
        return candidate not in ("", "/")
    normalized_parent = "/" + "/".join(decode_pointer(parent))
    normalized_candidate = "/" + "/".join(decode_pointer(candidate))
    return normalized_candidate.startswith(normalized_parent + "/")


def pointer_depth(path: str) -> int:
    if path in ("", "/"):
        return 0
    return len(decode_pointer(path))


def get_json_value(state: Any, path: str) -> Any:
    if path in ("", "/"):
        return clone_json(state)

    cursor = state
    for segment in decode_pointer(path):
        if isinstance(cursor, list):
            index = int(segment)
            if index >= len(cursor):
                return _MISSING
            cursor = cursor[index]
            continue
        if not isinstance(cursor, dict) or segment not in cursor:
            return _MISSING
        cursor = cursor[segment]
    return clone_json(cursor)


def set_json_value(state: Any, path: str, value: Any) -> JsonValue:
    if path in ("", "/"):
        return clone_json(value)

    root = clone_json(state) if isinstance(state, (dict, list)) else {}
    segments = decode_pointer(path)
    cursor: Any = root

    for index, segment in enumerate(segments[:-1]):
        next_segment = segments[index + 1]
        expects_list = next_segment.isdigit()

        if isinstance(cursor, list):
            list_index = int(segment)
            while len(cursor) <= list_index:
                cursor.append([] if expects_list else {})
            if not isinstance(cursor[list_index], (dict, list)):
                cursor[list_index] = [] if expects_list else {}
            cursor = cursor[list_index]
            continue

        if segment not in cursor or not isinstance(cursor[segment], (dict, list)):
            cursor[segment] = [] if expects_list else {}
        cursor = cursor[segment]

    final_segment = segments[-1]
    if isinstance(cursor, list):
        list_index = int(final_segment)
        while len(cursor) <= list_index:
            cursor.append(None)
        cursor[list_index] = clone_json(value)
        return root

    cursor[final_segment] = clone_json(value)
    return root


def delete_json_value(state: Any, path: str) -> JsonValue:
    if path in ("", "/"):
        return {}

    if not isinstance(state, (dict, list)):
        return {}

    root = clone_json(state)
    segments = decode_pointer(path)
    cursor: Any = root

    for segment in segments[:-1]:
        if isinstance(cursor, list):
            index = int(segment)
            if index >= len(cursor):
                return root
            cursor = cursor[index]
            continue

        if segment not in cursor:
            return root
        cursor = cursor[segment]

    final_segment = segments[-1]
    if isinstance(cursor, list):
        index = int(final_segment)
        if index < len(cursor):
            cursor.pop(index)
        return root

    cursor.pop(final_segment, None)
    return root
