from __future__ import annotations

import json
import os
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class LiveSyncConfig:
    deployment_url: str
    site_url: str
    room_id: str
    client_id: str
    project_root: Optional[str] = None

    @classmethod
    def load(cls, script_directory: Optional[Path] = None) -> Optional["LiveSyncConfig"]:
        values = cls._load_file_values(script_directory)
        deployment_url = (
            os.environ.get("LIVE_SYNC_DEPLOYMENT_URL")
            or os.environ.get("LIVE_SYNC_SERVER_URL")
            or os.environ.get("CONVEX_URL")
            or values.get("deployment_url")
            or values.get("server_url")
        )
        normalized_deployment_url = cls._normalize_url(deployment_url)
        site_url = (
            os.environ.get("LIVE_SYNC_SITE_URL")
            or values.get("site_url")
            or (cls._derive_site_url(str(normalized_deployment_url)) if normalized_deployment_url else None)
        )
        normalized_site_url = cls._normalize_url(site_url)
        room_id = os.environ.get("LIVE_SYNC_ROOM_ID") or values.get("room_id")
        client_id = (
            os.environ.get("LIVE_SYNC_CLIENT_ID")
            or values.get("client_id")
            or socket.gethostname()
        )
        project_root = os.environ.get("LIVE_SYNC_PROJECT_ROOT") or values.get("project_root")

        if not normalized_deployment_url or not normalized_site_url or not room_id:
            return None

        return cls(
            deployment_url=str(normalized_deployment_url),
            site_url=str(normalized_site_url),
            room_id=str(room_id),
            client_id=str(client_id),
            project_root=str(project_root) if project_root else None,
        )

    @staticmethod
    def _load_file_values(script_directory: Optional[Path]) -> dict:
        directory = script_directory or Path(__file__).resolve().parent
        config_path = directory / "config.json"
        if not config_path.exists():
            return {}
        return json.loads(config_path.read_text(encoding="utf-8"))

    @staticmethod
    def _derive_site_url(deployment_url: str) -> Optional[str]:
        if deployment_url.endswith(".convex.cloud"):
            return deployment_url[:-len(".convex.cloud")] + ".convex.site"
        return None

    @staticmethod
    def _normalize_url(value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = str(value).strip().rstrip("/")
        return normalized or None
