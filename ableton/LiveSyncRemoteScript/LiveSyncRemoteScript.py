from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from .config import LiveSyncConfig
from .debug_log import log_debug
from .live_adapter import LiveSongAdapter
from .media_sync import ProjectMediaSync
from .sidecar_client import LocalSidecarClient
from .service import SyncService

try:
    from ableton.v2.control_surface import ControlSurface
except ImportError:
    try:
        from _Framework.ControlSurface import ControlSurface  # type: ignore
    except ImportError:
        class ControlSurface(object):  # type: ignore
            def __init__(self, c_instance=None, *args, **kwargs):
                self._c_instance = c_instance

            def schedule_message(self, delay, callback):
                return None

            def log_message(self, message):
                print(message)


class LiveSyncRemoteScript(ControlSurface):
    _NOTE_POLL_DELAY = 15

    def __init__(self, c_instance) -> None:
        log_debug("LiveSyncRemoteScript.__init__ entered")
        super(LiveSyncRemoteScript, self).__init__(c_instance)
        self._service = None
        self._note_poll_running = False

        try:
            self._log("Loading config.")
            script_directory = Path(__file__).resolve().parent
            config = LiveSyncConfig.load(script_directory)
            if config is None:
                self._log("Disabled because config.json or LIVE_SYNC_* values are missing.")
            else:
                runtime_client_id = "%s-%s" % (config.client_id, uuid4().hex[:8])
                self._log(
                    "Config loaded for deployment %s room %s client %s (runtime %s)."
                    % (config.deployment_url, config.room_id, config.client_id, runtime_client_id)
                )
                adapter = LiveSongAdapter(
                    self.song,
                    application_provider=getattr(self, "application", None),
                    logger=self._log,
                    project_root=config.project_root,
                )
                client = LocalSidecarClient(
                    deployment_url=config.deployment_url,
                    room_id=config.room_id,
                    client_id=runtime_client_id,
                    script_directory=script_directory,
                    logger=self._log,
                )
                self._log("Using local sidecar at %s." % client.sidecar_log_path())
                media_sync = ProjectMediaSync(
                    site_url=config.site_url,
                    room_id=config.room_id,
                    client_id=runtime_client_id,
                    project_root=config.project_root or adapter.get_project_root(),
                    schedule_main_thread=self._schedule_on_ui_thread,
                    on_media_ready=self._handle_media_ready,
                    logger=self._log,
                )
                self._log("Starting sync service.")
                self._service = SyncService(
                    adapter=adapter,
                    client=client,
                    room_id=config.room_id,
                    client_id=runtime_client_id,
                    media_sync=media_sync,
                    schedule_main_thread=self._schedule_on_ui_thread,
                    logger=self._log,
                )
                self._service.start()
                self._start_note_poll()
                self._log(
                    "Connected to %s for room %s as %s."
                    % (config.deployment_url, config.room_id, runtime_client_id)
                )
        except Exception as error:
            self._log("Initialization failed: %s" % error)

    def disconnect(self) -> None:
        self._note_poll_running = False
        if self._service is not None:
            self._service.shutdown()
        self._service = None
        disconnect = getattr(super(LiveSyncRemoteScript, self), "disconnect", None)
        if callable(disconnect):
            disconnect()

    def _schedule_on_ui_thread(self, callback) -> None:
        if hasattr(self, "schedule_message"):
            self.schedule_message(1, callback)
        else:
            callback()

    def _log(self, message: str) -> None:
        log_debug(message)
        if hasattr(self, "log_message"):
            self.log_message(message)
        else:
            print(message)

    def _handle_media_ready(self) -> None:
        if self._service is not None:
            self._service.handle_media_ready()

    def _start_note_poll(self) -> None:
        if self._note_poll_running:
            return
        self._note_poll_running = True
        self._schedule_note_poll()

    def _schedule_note_poll(self) -> None:
        if not self._note_poll_running or self._service is None:
            return
        if hasattr(self, "schedule_message"):
            self.schedule_message(self._NOTE_POLL_DELAY, self._run_note_poll)
        else:
            self._run_note_poll()

    def _run_note_poll(self) -> None:
        if not self._note_poll_running or self._service is None:
            return
        try:
            self._service.poll_local_state()
        except Exception as error:
            self._log("Note poll failed: %s" % error)
        finally:
            self._schedule_note_poll()
