from __future__ import annotations

from typing import Any, Callable, Optional

from .sync_core import apply_operation_to_state, clone_json, delete_json_value, set_json_value


class MockLiveAdapter:
    def __init__(self, initial_state: Any) -> None:
        self._state = clone_json(initial_state)
        self._on_change: Optional[Callable[[], None]] = None

    def capture_state(self) -> Any:
        return clone_json(self._state)

    def start_listening(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change

    def stop_listening(self) -> None:
        self._on_change = None

    def apply_operation(self, operation) -> None:
        self._state = apply_operation_to_state(self._state, operation)

    def apply_snapshot(self, snapshot_state: Any) -> None:
        self._state = clone_json(snapshot_state)

    def set_path(self, path: str, value: Any) -> None:
        self._state = set_json_value(self._state, path, value)
        self._emit_change()

    def delete_path(self, path: str) -> None:
        self._state = delete_json_value(self._state, path)
        self._emit_change()

    def _emit_change(self) -> None:
        if self._on_change is not None:
            self._on_change()
