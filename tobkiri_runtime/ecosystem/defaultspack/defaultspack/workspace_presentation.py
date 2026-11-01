"""Project captured workspace-owner reads for the Defaults coding surface."""

from __future__ import annotations

import re
from typing import Mapping


WORKSPACE_LIST_TARGET = (
    "defaults.workspaces.list",
    "tobkiri.resource.workspace.v1",
    "rumi_workspace_mount_pack.workspace-resource",
    "rumi_workspace_mount_pack.workspace-mount.resource",
    "rumi_workspace_mount_pack.workspace-mount.resource",
)

WORKSPACE_GET_TARGET = (
    "defaults.workspaces.get",
    "tobkiri.resource.workspace.v1",
    "rumi_workspace_mount_pack.workspace-resource",
    "rumi_workspace_mount_pack.workspace-mount.resource",
    "rumi_workspace_mount_pack.workspace-mount.resource",
)

_WORKSPACE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_ABSOLUTE_ROOT = re.compile(r"^(?:/|[A-Za-z]:[\\/]|\\\\)")


def normalize_workspace_read(
    target: tuple[str, str, str, str, str],
    payload: Mapping[str, object],
    *,
    profile_id: str,
) -> dict[str, object]:
    """Bind a finite workspace read to the captured Profile identity."""

    if not profile_id:
        raise ValueError("workspace read requires a captured Profile")
    if target == WORKSPACE_LIST_TARGET:
        if payload:
            raise ValueError("workspace list does not accept caller input")
        return {"profile_id": profile_id, "operation": "list"}
    if target != WORKSPACE_GET_TARGET or set(payload) != {"workspace_id"}:
        raise ValueError("workspace read payload is invalid")
    workspace_id = payload.get("workspace_id")
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
    ):
        raise ValueError("workspace identity is invalid")
    return {
        "profile_id": profile_id,
        "operation": "get",
        "workspace_id": workspace_id,
    }


def present_workspace_list(result: Mapping[str, object]) -> dict[str, object]:
    """Return the canonical workspace snapshot in the coding UI shape."""

    if result.get("state") == "error":
        return dict(result)
    mounts = result.get("mounts")
    if not isinstance(mounts, list):
        raise ValueError("workspace owner returned an invalid mount list")
    revision = result.get("revision")
    if type(revision) is not int or revision < 0:
        raise ValueError("workspace owner returned an invalid revision")
    selected = result.get("selected_workspace_id")
    if selected is not None and not isinstance(selected, str):
        raise ValueError("workspace owner returned an invalid selection")
    return {
        "workspaces": [_present_mount(item) for item in mounts],
        "selected_workspace_id": selected,
        "revision": revision,
    }


def present_workspace_record(result: Mapping[str, object]) -> dict[str, object]:
    """Return one exact canonical workspace record in the coding UI shape."""

    if result.get("state") == "error":
        return dict(result)
    return {"workspace": _present_mount(result)}


def _present_mount(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("workspace owner returned an invalid mount")
    workspace_id = value.get("id")
    root_path = value.get("root_path")
    if (
        not isinstance(workspace_id, str)
        or _WORKSPACE_ID.fullmatch(workspace_id) is None
        or not isinstance(root_path, str)
        or _ABSOLUTE_ROOT.match(root_path) is None
        or "\x00" in root_path
    ):
        raise ValueError("workspace owner returned an invalid mount identity")
    metadata = value.get("metadata")
    if not isinstance(metadata, Mapping):
        raise ValueError("workspace owner returned invalid mount metadata")
    user_metadata = metadata.get("metadata")
    if user_metadata is not None and not isinstance(user_metadata, Mapping):
        raise ValueError("workspace owner returned invalid user metadata")
    trusted = metadata.get("trusted", False)
    if not isinstance(trusted, bool):
        raise ValueError("workspace owner returned an invalid trust state")
    return {
        "workspace_id": workspace_id,
        "label": str(metadata.get("label") or workspace_id),
        "root_path": root_path,
        "trusted": trusted,
        "trust_granted_at": metadata.get("trust_granted_at"),
        "last_used_at": value.get("updated_at"),
        "metadata": dict(user_metadata) if user_metadata is not None else {},
    }


__all__ = [
    "WORKSPACE_GET_TARGET",
    "WORKSPACE_LIST_TARGET",
    "normalize_workspace_read",
    "present_workspace_list",
    "present_workspace_record",
]
