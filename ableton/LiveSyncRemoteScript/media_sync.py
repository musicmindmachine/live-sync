from __future__ import annotations

import hashlib
import json
import mimetypes
import threading
from dataclasses import dataclass
from http.client import HTTPConnection, HTTPSConnection
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib import error, parse, request


@dataclass(frozen=True)
class LocalMediaReference:
    reference_id: str
    lom_path: str
    relative_path: str
    absolute_path: str
    role: str


class ProjectMediaSync:
    def __init__(
        self,
        site_url: str,
        room_id: str,
        client_id: str,
        project_root: Optional[str],
        schedule_main_thread: Callable[[Callable[[], None]], None],
        on_media_ready: Callable[[], None],
        logger: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._site_url = site_url.rstrip("/")
        self._room_id = room_id
        self._client_id = client_id
        self._project_root = Path(project_root).expanduser().resolve() if project_root else None
        self._schedule_main_thread = schedule_main_thread
        self._on_media_ready = on_media_ready
        self._logger = logger or (lambda message: None)

        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

        self._local_references: List[LocalMediaReference] = []
        self._local_lamport = 0
        self._pending_remote_refresh = True
        self._last_media_version = 0
        self._hash_cache: Dict[str, tuple] = {}

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="LiveSyncMediaSync",
            daemon=True,
        )
        self._thread.start()

    def shutdown(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._thread = None

    def replace_local_references(self, references: Iterable[Dict[str, Any]], lamport: int) -> None:
        normalized = [
            LocalMediaReference(
                reference_id=str(reference["reference_id"]),
                lom_path=str(reference["lom_path"]),
                relative_path=str(reference["relative_path"]),
                absolute_path=str(reference["absolute_path"]),
                role=str(reference.get("role", "media")),
            )
            for reference in references
            if reference.get("relative_path") and reference.get("absolute_path")
        ]
        with self._lock:
            self._local_references = normalized
            self._local_lamport = int(lamport)
        self._wake_event.set()

    def note_remote_version(self, media_version: int) -> None:
        with self._lock:
            if media_version <= self._last_media_version:
                return
            self._pending_remote_refresh = True
        self._wake_event.set()

    def set_project_root(self, project_root: Optional[str]) -> None:
        with self._lock:
            self._project_root = Path(project_root).expanduser().resolve() if project_root else None
        self._wake_event.set()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait(1.0)
            self._wake_event.clear()
            if self._stop_event.is_set():
                break

            try:
                self._sync_once()
            except Exception as error:
                self._log("Media sync failed: %s" % error)

    def _sync_once(self) -> None:
        project_root = self._project_root
        if project_root is None:
            return

        with self._lock:
            references = list(self._local_references)
            lamport = self._local_lamport
            pending_remote_refresh = self._pending_remote_refresh
            self._pending_remote_refresh = False

        did_local_work = self._sync_local_references(references, lamport)
        if not pending_remote_refresh and not did_local_work:
            return

        manifest = self._post_json("/media/pull", {"roomId": self._room_id})
        if not manifest.get("roomExists"):
            return

        self._last_media_version = int(manifest.get("mediaVersion", 0))
        downloaded_any = False
        for reference in manifest.get("references", []):
            if reference.get("assetStatus") != "ready":
                continue
            if self._ensure_local_reference(project_root, reference):
                downloaded_any = True

        if downloaded_any:
            self._schedule_main_thread(self._on_media_ready)

    def _sync_local_references(self, references: List[LocalMediaReference], lamport: int) -> bool:
        did_work = False
        for reference in references:
            path = Path(reference.absolute_path).expanduser()
            if not path.exists() or not path.is_file():
                continue

            content_hash = self._hash_file(path)
            size = path.stat().st_size
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            register_result = self._post_json(
                "/media/register-reference",
                {
                    "roomId": self._room_id,
                    "clientId": self._client_id,
                    "referenceId": reference.reference_id,
                    "lomPath": reference.lom_path,
                    "relativePath": reference.relative_path,
                    "role": reference.role,
                    "contentHash": content_hash,
                    "contentType": content_type,
                    "size": size,
                    "lamport": lamport,
                },
            )
            did_work = did_work or bool(register_result.get("updated"))

            if register_result.get("assetStatus") == "ready":
                continue

            upload_plan = self._post_json(
                "/media/prepare-upload",
                {
                    "roomId": self._room_id,
                    "clientId": self._client_id,
                    "contentHash": content_hash,
                    "relativePath": reference.relative_path,
                    "contentType": content_type,
                    "size": size,
                },
            )
            if not upload_plan.get("uploadRequired"):
                continue

            self._put_file(str(upload_plan["uploadUrl"]), path, content_type)
            self._post_json(
                "/media/complete-upload",
                {
                    "roomId": self._room_id,
                    "clientId": self._client_id,
                    "contentHash": content_hash,
                },
            )
            did_work = True

        return did_work

    def _ensure_local_reference(self, project_root: Path, reference: Dict[str, Any]) -> bool:
        relative_path = str(reference.get("relativePath", ""))
        content_hash = str(reference.get("contentHash", ""))
        if not relative_path or not content_hash:
            return False

        target_path = (project_root / relative_path).resolve()
        if target_path.exists() and target_path.is_file() and self._hash_file(target_path) == content_hash:
            return False

        download_result = self._post_json(
            "/media/download-url",
            {
                "roomId": self._room_id,
                "contentHash": content_hash,
            },
        )
        download_url = str(download_result["url"])
        target_path.parent.mkdir(parents=True, exist_ok=True)

        with NamedTemporaryFile(delete=False, dir=str(target_path.parent), suffix=".part") as temporary_file:
            temporary_path = Path(temporary_file.name)

        try:
            self._download_file(download_url, temporary_path)
            if self._hash_file(temporary_path) != content_hash:
                raise RuntimeError("Downloaded media hash mismatch for %s" % relative_path)
            temporary_path.replace(target_path)
            self._log("Downloaded media %s -> %s" % (content_hash, target_path))
            return True
        finally:
            if temporary_path.exists():
                try:
                    temporary_path.unlink()
                except OSError:
                    pass

    def _post_json(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = request.Request(
            self._site_url + path,
            data=body,
            method="POST",
            headers={"content-type": "application/json"},
        )
        try:
            with request.urlopen(req, timeout=60) as response:
                return json.loads(response.read().decode("utf-8"))
        except error.HTTPError as http_error:
            response_body = http_error.read().decode("utf-8", errors="replace")
            raise RuntimeError("HTTP %s for %s: %s" % (http_error.code, path, response_body))

    def _hash_file(self, path: Path) -> str:
        stat = path.stat()
        cache_key = str(path.resolve())
        cached = self._hash_cache.get(cache_key)
        signature = (int(stat.st_size), int(stat.st_mtime))
        if cached is not None and cached[:2] == signature:
            return str(cached[2])

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        hexdigest = digest.hexdigest()
        self._hash_cache[cache_key] = signature + (hexdigest,)
        return hexdigest

    def _put_file(self, url: str, path: Path, content_type: str) -> None:
        parsed = parse.urlparse(url)
        connection_class = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
        connection = connection_class(parsed.hostname, parsed.port, timeout=300)
        request_path = parsed.path + (("?" + parsed.query) if parsed.query else "")
        try:
            connection.putrequest("PUT", request_path)
            connection.putheader("Content-Length", str(path.stat().st_size))
            connection.putheader("Content-Type", content_type)
            connection.endheaders()
            with path.open("rb") as handle:
                while True:
                    chunk = handle.read(1024 * 1024)
                    if not chunk:
                        break
                    connection.send(chunk)
            response = connection.getresponse()
            response_body = response.read().decode("utf-8", errors="replace")
            if response.status >= 400:
                raise RuntimeError("Upload failed with %s: %s" % (response.status, response_body))
        finally:
            connection.close()

    def _download_file(self, url: str, target_path: Path) -> None:
        with request.urlopen(url, timeout=300) as response, target_path.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)

    def _log(self, message: str) -> None:
        self._logger("LiveSync: %s" % message)
