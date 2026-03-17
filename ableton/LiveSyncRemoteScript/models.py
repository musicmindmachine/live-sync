from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

JsonScalar = Union[None, bool, int, float, str]
JsonValue = Union[JsonScalar, Dict[str, "JsonValue"], List["JsonValue"]]
ClockEntry = Dict[str, Union[int, str]]
ClockState = Dict[str, ClockEntry]


@dataclass(frozen=True)
class Operation:
    op_id: str
    client_id: str
    lamport: int
    kind: str
    path: str
    value: Optional[JsonValue] = None
    sequence: Optional[int] = None

    def to_payload(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "opId": self.op_id,
            "clientId": self.client_id,
            "lamport": self.lamport,
            "kind": self.kind,
            "path": self.path,
        }
        if self.kind == "set":
            payload["valueJson"] = json.dumps(self.value, separators=(",", ":"), sort_keys=True)
        return payload

    @classmethod
    def from_payload(cls, payload: Dict[str, Any]) -> "Operation":
        value = None
        if "valueJson" in payload and payload["valueJson"] is not None:
            value = json.loads(payload["valueJson"])
        return cls(
            op_id=payload["opId"],
            client_id=payload["clientId"],
            lamport=int(payload["lamport"]),
            kind=payload["kind"],
            path=payload["path"],
            value=value,
            sequence=payload.get("sequence"),
        )


@dataclass(frozen=True)
class AcceptedOperation:
    op_id: str
    sequence: Optional[int]
    duplicate: bool
    applied: bool


@dataclass(frozen=True)
class PushResult:
    room_id: str
    accepted: List[AcceptedOperation]
    last_sequence: int
    snapshot_state: JsonValue
    clock_state: ClockState
    max_lamport: int


@dataclass(frozen=True)
class PullResult:
    room_exists: bool
    latest_sequence: int
    compacted_through_sequence: int
    reset_required: bool
    snapshot_state: Optional[JsonValue]
    clock_state: Optional[ClockState]
    snapshot_sequence: int
    max_lamport: int
    ops: List[Operation]
