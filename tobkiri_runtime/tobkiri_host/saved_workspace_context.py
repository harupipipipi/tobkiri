"""Fresh canonical workspace owner reads for saved-turn context."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from threading import RLock
import re
from typing import Any

from tobkiri_protocol.saved_conversation import (
    SavedWorkspaceResolution,
    validate_saved_conversation_context,
)

WORKSPACE_TARGET = (
    "tobkiri.resource.workspace.v1",
    "rumi_workspace_mount_pack.workspace-resource",
)


class ResolvedWorkspaceConversation(dict[str, Any]):
    """Private owner projection, never serialized into a saved request."""

    def __init__(
        self, conversation: Mapping[str, Any], resolution: SavedWorkspaceResolution
    ) -> None:
        super().__init__(conversation)
        self.workspace_resolution = resolution


def workspace_resolution(
    conversation: Mapping[str, Any],
) -> SavedWorkspaceResolution | None:
    """Return facts only from the exact Host-local wrapper."""
    return (
        conversation.workspace_resolution
        if type(conversation) is ResolvedWorkspaceConversation
        else None
    )


def resolve_saved_workspace(
    conversation: Mapping[str, Any],
    *,
    profile_id: str,
    read_binding: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    guard: Callable[[], None],
) -> Mapping[str, Any]:
    """Read accepted mount facts through the caller's captured owner client."""
    metadata = conversation.get("metadata") or {}
    if not isinstance(metadata, Mapping) or not metadata.get("workspace_id"):
        validate_saved_conversation_context(conversation)
        return conversation
    guard()
    workspace_id = metadata["workspace_id"]
    if type(workspace_id) is not str or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}",
        workspace_id,
    ):
        raise ValueError("saved workspace identity is invalid")
    result = read_binding(
        {"profile_id": profile_id, "operation": "binding", "workspace_id": workspace_id}
    )
    guard()
    mount, binding = result.get("mount"), result.get("binding")
    if (
        not isinstance(mount, Mapping)
        or not isinstance(binding, Mapping)
        or set(binding)
        != {"workspace_id", "mount_revision", "root_st_dev", "root_st_ino"}
        or binding["workspace_id"] != workspace_id
        or mount.get("id") != workspace_id
        or mount.get("mount_revision") != binding["mount_revision"]
    ):
        raise ValueError("saved workspace owner binding is invalid")
    conversation_id = conversation.get("id")
    conversation_revision = conversation.get("conversation_revision")
    if (
        type(conversation_id) is not str
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", conversation_id)
        or type(conversation_revision) is not int
        or conversation_revision < 1
    ):
        raise ValueError("saved workspace conversation identity is invalid")
    resolution = SavedWorkspaceResolution(
        profile_id,
        conversation_id,
        conversation_revision,
        **dict(binding),
    )
    validate_saved_conversation_context(
        conversation,
        workspace_resolution=resolution,
    )
    return ResolvedWorkspaceConversation(conversation, resolution)


def recheck_saved_workspace(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
) -> None:
    """Fence immutable mount facts and the current canonical owner revision."""
    old, fresh = workspace_resolution(previous), workspace_resolution(current)
    if old is None and fresh is None:
        return
    if old is None or fresh is None or old != fresh:
        raise ValueError("saved workspace context changed before execution")


def same_workspace_mount(
    previous: SavedWorkspaceResolution,
    current: SavedWorkspaceResolution,
) -> bool:
    """Compare mount facts while retaining each actual conversation revision."""
    return (
        replace(previous, conversation_revision=current.conversation_revision)
        == current
    )


class SavedWorkspacePins:
    """Retained Saved exchange lifetime owner of accepted immutable mount facts."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._accepted: SavedWorkspaceResolution | None = None
        self._observed = False

    def accept(self, resolution: SavedWorkspaceResolution | None) -> None:
        """Bind first verified read; retain mount identity across owner appends."""
        if resolution is not None and type(resolution) is not SavedWorkspaceResolution:
            raise ValueError("saved workspace projection is invalid")
        with self._lock:
            if not self._observed:
                self._accepted = resolution
                self._observed = True
            elif self._accepted is None and resolution is None:
                return
            elif (
                self._accepted is None
                or resolution is None
                or not same_workspace_mount(self._accepted, resolution)
            ):
                raise ValueError("saved workspace mount changed during exchange")
