"""Profile-scoped state paths backed by the verified Pack v4 activation.

This module owns storage layout only.  Files below ``workspaces/<profile_id>``
must never be interpreted as Profile, Pack, policy, or permission authority.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


PROFILE_WORKSPACE_VERSION = 4


@dataclass(frozen=True)
class ProfileWorkspacePaths:
    profile_id: str
    root: Path
    state_dir: Path
    database_path: Path
    artifacts_dir: Path
    audit_dir: Path
    snapshots_dir: Path


def _default_user_data_root() -> Path:
    base_dir = Path(__file__).resolve().parent.parent
    configured = os.environ.get("RUMI_USER_DATA")
    return Path(configured) if configured else base_dir / "user_data"


def validate_profile_id(profile_id: str) -> str:
    candidate = str(profile_id or "").strip()
    if not candidate:
        raise ValueError("profile_id must not be empty")
    if "/" in candidate or "\\" in candidate:
        raise ValueError("profile_id must not contain path separators")
    if candidate == ".." or ".." in candidate:
        raise ValueError("profile_id must not contain path traversal segments")
    return candidate


def profile_workspace_payload(paths: ProfileWorkspacePaths) -> dict[str, Any]:
    payload = asdict(paths)
    return {key: str(value) if isinstance(value, Path) else value for key, value in payload.items()}


class ProfileWorkspaceManager:
    """Create non-authoritative state directories for one v4 Profile."""

    def __init__(self, user_data_root: Path | None = None) -> None:
        self.user_data_root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()

    def root_for_profile(self, profile_id: str) -> Path:
        return self.user_data_root / "workspaces" / validate_profile_id(profile_id)

    def paths_for_profile(self, profile_id: str) -> ProfileWorkspacePaths:
        safe_id = validate_profile_id(profile_id)
        root = self.root_for_profile(safe_id)
        state_dir = root / "state"
        return ProfileWorkspacePaths(
            profile_id=safe_id,
            root=root,
            state_dir=state_dir,
            database_path=state_dir / "rumi.sqlite",
            artifacts_dir=root / "artifacts",
            audit_dir=root / "audit",
            snapshots_dir=root / "snapshots",
        )

    def initialize_profile_workspace(
        self,
        profile: dict[str, Any],
        *,
        create_missing: bool = True,
    ) -> ProfileWorkspacePaths:
        profile_id = validate_profile_id(str(profile.get("profile_id") or ""))
        paths = self.paths_for_profile(profile_id)
        if not create_missing:
            return paths
        for directory in (
            paths.root,
            paths.state_dir,
            paths.artifacts_dir,
            paths.audit_dir,
            paths.snapshots_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        paths.database_path.touch(exist_ok=True)
        (paths.audit_dir / "events.jsonl").touch(exist_ok=True)
        self._write_json_if_missing(
            paths.state_dir / "workspace.json",
            {
                "version": PROFILE_WORKSPACE_VERSION,
                "profile_id": profile_id,
                "authority": "state-only",
            },
        )
        return paths

    def profile_database_path(self, profile_id: str) -> Path:
        return self.paths_for_profile(profile_id).database_path

    def profile_user_data_dir(self, profile_id: str) -> Path:
        return self.paths_for_profile(profile_id).state_dir

    def payload_for_profile(self, profile_id: str) -> dict[str, Any]:
        return profile_workspace_payload(self.paths_for_profile(profile_id))

    def mark_workspace_orphaned(self, profile_id: str, profile: dict[str, Any] | None = None) -> None:
        paths = self.paths_for_profile(profile_id)
        paths.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": PROFILE_WORKSPACE_VERSION,
            "profile_id": paths.profile_id,
            "orphaned": True,
            "note": "state retained after v4 activation removal",
        }
        self._atomic_write_text(
            paths.state_dir / "orphaned.json",
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _write_json_if_missing(self, path: Path, payload: dict[str, Any]) -> None:
        if path.exists():
            return
        self._atomic_write_text(
            path,
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )

    def _atomic_write_text(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)
