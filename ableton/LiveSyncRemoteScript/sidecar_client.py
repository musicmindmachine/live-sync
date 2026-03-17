from __future__ import annotations

import json
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Optional
from urllib import error, request
from uuid import uuid4

from .debug_log import log_debug
from .models import Operation, PullResult, PushResult
from .sidecar_protocol import deserialize_pull_result, deserialize_push_result


class SidecarProcessManager:
    def __init__(
        self,
        deployment_url: str,
        room_id: str,
        client_id: str,
        script_directory: Path,
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._deployment_url = deployment_url.rstrip("/")
        self._room_id = room_id
        self._client_id = client_id
        self._script_directory = script_directory
        self._logger = logger or (lambda message: None)
        self._token = uuid4().hex
        self._port: Optional[int] = None
        self._server_process: Optional[subprocess.Popen] = None
        self._watch_process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._log_path = (
            Path.home()
            / "Library"
            / "Preferences"
            / "Ableton"
            / ("LiveSyncSidecar-%s.log" % self._client_id)
        )
        self._watch_state_path = (
            Path.home()
            / "Library"
            / "Preferences"
            / "Ableton"
            / ("LiveSyncSidecarState-%s.json" % self._client_id)
        )

    @property
    def log_path(self) -> Path:
        return self._log_path

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise RuntimeError("Sidecar is not started.")
        return "http://127.0.0.1:%s" % self._port

    @property
    def token(self) -> str:
        return self._token

    def ensure_started(self) -> None:
        with self._lock:
            if self._is_running():
                return
            self._stop_locked()
            self._start_locked()

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _start_locked(self) -> None:
        sidecar_python = self._sidecar_python_path()
        server_entry = self._script_directory / "sidecar_main.py"
        watch_entry = self._script_directory / "sidecar_watch.py"
        if not sidecar_python.exists():
            raise RuntimeError(
                "Sidecar Python runtime not found at %s. Re-run the Live installer." % sidecar_python
            )
        if not server_entry.exists():
            raise RuntimeError("Sidecar server entrypoint not found at %s." % server_entry)
        if not watch_entry.exists():
            raise RuntimeError("Sidecar watch entrypoint not found at %s." % watch_entry)

        self._port = self._choose_port()
        self._watch_state_path.parent.mkdir(parents=True, exist_ok=True)
        server_command = [
            str(sidecar_python),
            str(server_entry),
            "--deployment-url",
            self._deployment_url,
            "--room-id",
            self._room_id,
            "--client-id",
            self._client_id,
            "--port",
            str(self._port),
            "--token",
            self._token,
            "--watch-state-path",
            str(self._watch_state_path),
            "--log-path",
            str(self._log_path),
        ]
        watch_command = [
            str(sidecar_python),
            str(watch_entry),
            "--deployment-url",
            self._deployment_url,
            "--room-id",
            self._room_id,
            "--client-id",
            self._client_id,
            "--watch-state-path",
            str(self._watch_state_path),
            "--log-path",
            str(self._log_path),
        ]

        try:
            self._log("Starting sidecar server on localhost:%s." % self._port)
            self._server_process = subprocess.Popen(
                server_command,
                cwd=str(self._script_directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._wait_for_health()
            self._log("Starting sidecar watch process.")
            self._watch_process = subprocess.Popen(
                watch_command,
                cwd=str(self._script_directory),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(0.2)
            if self._watch_process.poll() is not None:
                raise RuntimeError("Sidecar watch exited immediately. See %s." % self._log_path)
        except Exception:
            self._stop_locked()
            raise

    def _stop_locked(self) -> None:
        server_process = self._server_process
        watch_process = self._watch_process

        if server_process is not None:
            try:
                self._post_json("/shutdown", {}, timeout=1.0, ensure_started=False)
            except Exception:
                pass
            self._terminate_process(server_process)
        if watch_process is not None:
            self._terminate_process(watch_process)
        self._server_process = None
        self._watch_process = None
        self._port = None

    def _wait_for_health(self) -> None:
        deadline = time.time() + 10.0
        while time.time() < deadline:
            if self._server_process is not None and self._server_process.poll() is not None:
                raise RuntimeError("Sidecar exited before it became healthy. See %s." % self._log_path)
            if self._health_check():
                self._log("Sidecar is healthy on localhost:%s." % self._port)
                return
            time.sleep(0.2)
        raise RuntimeError("Timed out waiting for sidecar health check on localhost:%s." % self._port)

    def _health_check(self) -> bool:
        if self._port is None:
            return False
        try:
            response = self._post_json("/health", None, timeout=1.0, method="GET", ensure_started=False)
            return bool(response.get("ok"))
        except Exception:
            return False

    def _post_json(
        self,
        path: str,
        payload: Optional[Dict[str, Any]],
        timeout: float,
        method: str = "POST",
        ensure_started: bool = True,
    ) -> Dict[str, Any]:
        if ensure_started:
            self.ensure_started()
        if self._port is None:
            raise RuntimeError("Sidecar is not started.")
        body = b""
        headers = {"X-Live-Sync-Token": self._token}
        if payload is not None:
            body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(self.base_url + path, data=body if method == "POST" else None, method=method, headers=headers)
        try:
            with request.urlopen(req, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as http_error:
            response_body = http_error.read().decode("utf-8", errors="replace")
            raise RuntimeError("Sidecar HTTP %s for %s: %s" % (http_error.code, path, response_body))

    def post_json(self, path: str, payload: Dict[str, Any], timeout: float = 30.0) -> Dict[str, Any]:
        return self._post_json(path, payload, timeout=timeout)

    def get_json(self, path: str, timeout: float = 5.0) -> Dict[str, Any]:
        return self._post_json(path, None, timeout=timeout, method="GET")

    def _sidecar_python_path(self) -> Path:
        bin_dir = self._script_directory / ".sidecar-venv" / "bin"
        for candidate_name in ("python3", "python"):
            candidate = bin_dir / candidate_name
            if candidate.exists():
                return candidate
        return bin_dir / "python3"

    def _is_running(self) -> bool:
        return (
            self._server_process is not None
            and self._server_process.poll() is None
            and self._watch_process is not None
            and self._watch_process.poll() is None
            and self._health_check()
        )

    def _choose_port(self) -> int:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])
        finally:
            sock.close()

    def _terminate_process(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                process.kill()

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)
        log_debug("LiveSync: %s" % message)


class LocalSidecarClient:
    def __init__(
        self,
        deployment_url: str,
        room_id: str,
        client_id: str,
        script_directory: Path,
        logger: Optional[Callable[[str], None]] = None,
        manager: Optional[SidecarProcessManager] = None,
    ) -> None:
        self._room_id = room_id
        self._client_id = client_id
        self._logger = logger or (lambda message: None)
        self._manager = manager or SidecarProcessManager(
            deployment_url=deployment_url,
            room_id=room_id,
            client_id=client_id,
            script_directory=script_directory,
            logger=logger,
        )
        self._watch_stop = threading.Event()
        self._watch_thread: Optional[threading.Thread] = None

    def sidecar_log_path(self) -> str:
        return str(self._manager.log_path)

    def push_ops(self, room_id: str, client_id: str, ops: Iterable[Operation]) -> PushResult:
        payload = {
            "roomId": room_id,
            "clientId": client_id,
            "ops": [operation.to_payload() for operation in ops],
        }
        response = self._manager.post_json("/push_ops", payload)
        return deserialize_push_result(response)

    def pull_ops(self, room_id: str, after_sequence: int, limit: int = 200) -> PullResult:
        payload = {
            "roomId": room_id,
            "afterSequence": int(after_sequence),
            "limit": int(limit),
        }
        response = self._manager.post_json("/pull_ops", payload)
        return deserialize_pull_result(response)

    def start_room_watch(self, room_id: str, on_version: Callable[[Dict[str, int]], None]) -> None:
        self.stop_watch()
        self._watch_stop.clear()

        def watch_loop() -> None:
            event_counter = 0
            while not self._watch_stop.is_set():
                try:
                    response = self._manager.post_json(
                        "/watch_room_version",
                        {
                            "roomId": room_id,
                            "sinceCounter": event_counter,
                            "timeoutSeconds": 30.0,
                        },
                        timeout=35.0,
                    )
                    event_counter = int(response.get("eventCounter", event_counter))
                    if bool(response.get("updated", False)):
                        version = response.get("version", {})
                        on_version(
                            {
                                "latestSequence": int(version.get("latestSequence", 0)),
                                "compactedThroughSequence": int(version.get("compactedThroughSequence", 0)),
                                "maxLamport": int(version.get("maxLamport", 0)),
                                "mediaVersion": int(version.get("mediaVersion", 0)),
                                "updatedAt": int(version.get("updatedAt", 0)),
                            }
                        )
                except Exception as error:
                    if self._watch_stop.is_set():
                        break
                    self._log("Sidecar watch failed, retrying: %s" % error)
                    time.sleep(1.0)

        self._watch_thread = threading.Thread(
            target=watch_loop,
            name="LiveSyncLocalWatch",
            daemon=True,
        )
        self._watch_thread.start()

    def stop_watch(self) -> None:
        self._watch_stop.set()
        thread = self._watch_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._watch_thread = None

    def stop(self) -> None:
        self.stop_watch()
        self._manager.stop()

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)
