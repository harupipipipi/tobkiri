"""Application-owned Project membership DATA at the saved-send boundary.

Existing signed owner reads determine the Profile/caller namespace. Project
roots and labels remain inert metadata; only the conversation's existing
execution workspace owner and Gate can authorize filesystem operations. This
module neither reads a path nor accepts reference snapshots, approval flags or new grants.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from ecosystem.rumi_conversation_store_pack.runtime.chat_reference import (
    _membership, identifier,
)
from tobkiri_protocol.saved_conversation import (
    ResolvedSavedConversation, SavedOwnedContextResolution, owned_context_digest,
    owned_context_resolution,
)

PROJECT_TARGET = (
    "tobkiri.resource.project.state.v1", "tobkiri_ui_settings_pack.projects-read",
)
NAMESPACE = "defaultspack.projects.v1"
_PROJECT_METADATA = (
    "group_id", "groupId", "workspaceId", "workspace_root", "workspaceRoot",
    "rootPath", "rumi_data_path", "rumiDataPath", "rumi_dp_path",
    "workspace_bindings", "workspaceBindings",
)


def project_context(conversation: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return builtin facts only, never a captured module's class instance."""
    if type(conversation) is not ResolvedSavedConversation:
        return None
    owner = conversation.owned_context_owner
    return owner if type(owner) is dict else None


def _builtin_data(value: Any, depth: int = 0) -> None:
    """Reject code-bearing values without copying owner message history."""
    if depth > 64:
        raise ValueError("saved context DATA nesting is invalid")
    if value is None or type(value) in (str, bool, int, float):
        return
    if type(value) in (list, tuple):
        for item in value:
            _builtin_data(item, depth + 1)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _builtin_data(item, depth + 1)
        return
    raise ValueError("saved context contains non-builtin DATA")


def needs_project_context(conversation: Mapping[str, Any]) -> bool:
    metadata = conversation.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("saved Project metadata is invalid")
    return bool(conversation.get("group_id")) or any(
        key in metadata and metadata[key] is not None for key in _PROJECT_METADATA
    )


def requires_project_owner(conversation: Mapping[str, Any]) -> bool:
    metadata = conversation.get("metadata") or {}
    if not isinstance(metadata, Mapping):
        raise ValueError("saved Project metadata is invalid")
    return any(value is not None for value in (
        conversation.get("group_id"), metadata.get("group_id"), metadata.get("groupId"),
    ))


def _conversation_workspace(metadata: Mapping[str, Any]) -> str | None:
    values = [metadata[key] for key in ("workspace_id", "workspaceId")
              if metadata.get(key) is not None]
    if any(type(value) is not str for value in values) or len(set(values)) > 1:
        raise ValueError("saved conversation workspace aliases conflict")
    return identifier(values[0]) if values else None


def _conversation_bindings(metadata: Mapping[str, Any], workspace: str | None) -> tuple[tuple[str, str, str], ...]:
    previous = None
    for key in ("workspace_bindings", "workspaceBindings"):
        if key not in metadata:
            continue
        supplied = metadata[key]
        fields = ("workspace_id", "workspace_label", "workspace_root") if key == "workspace_bindings" else (
            "workspaceId", "workspaceLabel", "workspaceRoot",
        )
        if (not isinstance(supplied, list) or not 1 <= len(supplied) <= 32
                or any(not isinstance(item, Mapping) or set(item) != set(fields)
                       or any(type(item[field]) is not str for field in fields)
                       for item in supplied)):
            raise ValueError("saved conversation workspace bindings are invalid")
        current = tuple(tuple(item[field] for field in fields) for item in supplied)
        ids = [identifier(item[0]) for item in current]
        if (len(set(ids)) != len(ids) or workspace is not None and workspace not in ids
                or previous is not None and current != previous):
            raise ValueError("saved conversation workspace binding aliases conflict")
        previous = current
    return previous or ()


def _resolved(conversation, metadata, profile_id, owner=None):
    if type(profile_id) is not str:
        raise ValueError("saved context Profile DATA is invalid")
    result = {**conversation, "metadata": metadata}
    conversation_id = identifier(conversation.get("id"))
    revision = conversation.get("conversation_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("saved conversation revision is invalid")
    resolution = SavedOwnedContextResolution(
        identifier(profile_id), conversation_id, revision, owned_context_digest(result),
    )
    return ResolvedSavedConversation(
        result, owned_context_resolution=resolution, owned_context_owner=owner,
    )


def retain_saved_project_effect_snapshot(
    conversation: Mapping[str, Any], accepted: Mapping[str, Any], *, profile_id: str,
) -> Mapping[str, Any]:
    """Retain this tool effect's final read during existing workspace guards.

    The private composition supplies this exact nonserialized snapshot only
    within one admitted effect. Every new effect resolves the Project again.
    Fresh conversation/lineage and workspace owner reads still run; independent
    Project changes after the accepted read do not retroactively revoke it.
    """
    resolution = owned_context_resolution(accepted)
    if resolution is None and not needs_project_context(conversation):
        return conversation
    owner = project_context(accepted)
    _builtin_data(owner)
    if type(resolution) is SavedOwnedContextResolution:
        _builtin_data((resolution.profile_id, resolution.conversation_id,
                       resolution.conversation_revision, resolution.context_digest))
    metadata = dict(conversation.get("metadata") or {})
    workspace = _conversation_workspace(metadata)
    if owner is not None and "workspace_id" not in metadata and workspace is not None:
        metadata["workspace_id"] = workspace
    candidate = {**conversation, "metadata": metadata}
    if (type(resolution) is not SavedOwnedContextResolution
            or resolution.profile_id != profile_id
            or resolution.conversation_id != conversation.get("id")
            or resolution.conversation_revision != conversation.get("conversation_revision")
            or resolution.context_digest != owned_context_digest(accepted)
            or resolution.context_digest != owned_context_digest(candidate)
            or requires_project_owner(candidate) and owner is None):
        raise ValueError("saved Project effect conversation changed")
    return ResolvedSavedConversation(
        candidate, owned_context_resolution=resolution, owned_context_owner=owner,
    )


def resolve_saved_project(
    conversation: Mapping[str, Any], *, profile_id: str,
    presentation_owner: tuple[str, str],
    read_owner: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    guard: Callable[[], None],
    validate_projects: Callable[[Any], list[dict[str, Any]]] | None = None,
) -> Mapping[str, Any]:
    """Resolve actual persisted membership with the captured Project owner.

    Each invocation accepts its final owner read as this effect's context
    snapshot. Independent owner reads are not an atomic cross-owner transaction.
    A later effect calls this again; no post-effect retroactive revocation or
    automatic execution retry is introduced.
    """
    if not needs_project_context(conversation):
        return conversation
    metadata = conversation.get("metadata") or {}
    candidates = [conversation.get("group_id"), metadata.get("group_id"), metadata.get("groupId")]
    candidates = [value for value in candidates if value is not None]
    if not candidates:
        # A History move to No Project retains its conversation workspace and
        # inert bindings. Validate those owner-persisted facts without reading
        # any Project namespace. If a scalar execution workspace exists, its
        # ordinary workspace resolution is still mandatory. A complete inert
        # binding array alone does not create such a workspace or tool proof.
        workspace = _conversation_workspace(metadata)
        bindings = _conversation_bindings(metadata, workspace)
        if ((workspace is None and not bindings)
                or workspace is not None and metadata.get("workspace_id") != workspace):
            raise ValueError("saved bridge context resolution is required: workspace identity is unavailable")
        return _resolved(conversation, dict(metadata), profile_id)
    _membership(conversation, {identifier(value): None for value in candidates})
    if (not isinstance(presentation_owner, tuple) or len(presentation_owner) != 2
            or any(type(value) is not str or not value for value in presentation_owner)):
        raise PermissionError("saved bridge context resolution is required: Project presentation owner is unavailable")
    profile_id = identifier(profile_id)
    conversation_id = identifier(conversation.get("id"))
    revision = conversation.get("conversation_revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("saved Project conversation revision is invalid")
    guard()
    snapshot = read_owner({"profile_id": profile_id})
    guard()
    if (not isinstance(snapshot, Mapping)
            or set(snapshot) != {"namespace", "revision", "projects"}
            or snapshot.get("namespace") != NAMESPACE
            or type(snapshot.get("revision")) is not int
            or snapshot["revision"] < 0):
        raise ValueError("saved Project owner snapshot is invalid")
    if validate_projects is None:
        raise PermissionError("saved Project captured validator is unavailable")
    records = validate_projects(snapshot["projects"])
    _builtin_data(records)
    projects = {item["id"]: item for item in records}
    # Reuse the original owner membership rules, including alias conflicts.
    group_id = _membership(conversation, projects)
    if group_id is None:
        raise ValueError("saved Project membership is orphaned or unavailable")
    project = projects[group_id]
    metadata = dict(conversation.get("metadata") or {})
    project_primary = project["workspace_id"]
    if project_primary is not None:
        project_primary = identifier(project_primary)
    conversation_workspace = _conversation_workspace(metadata)
    bindings = project.get("workspace_bindings")
    if bindings is None:
        bindings = [] if project_primary is None else [{
            "workspace_id": project["workspace_id"],
            "workspace_label": project["workspace_label"],
            "workspace_root": project["workspace_root"],
        }]
    conversation_bindings = _conversation_bindings(metadata, conversation_workspace)
    # History moves change membership only. Keep the canonical conversation's
    # execution workspace and every inert binding, even if the selected Project
    # has a different primary. Composer selection already copies its intended
    # workspace explicitly through the existing conversation CAS operation.
    if "workspace_id" not in metadata and conversation_workspace is not None:
        metadata["workspace_id"] = conversation_workspace
    owner = {
        "profile_id": profile_id, "owner_principal_id": presentation_owner[0],
        "namespace": NAMESPACE, "project_revision": snapshot["revision"],
        "conversation_id": conversation_id, "conversation_revision": revision,
        "group_id": group_id, "project_title": project["title"],
        "project_primary_workspace_id": project_primary,
        "project_bindings": tuple((item["workspace_id"], item["workspace_label"], item["workspace_root"])
                                  for item in bindings),
        "conversation_workspace_id": conversation_workspace,
        "conversation_bindings": conversation_bindings,
    }
    _builtin_data(owner)
    return _resolved(conversation, metadata, profile_id, owner)


class BorrowedSavedContext:
    """Operation-local App code. Only detached builtin facts may escape."""

    def __init__(self, *, operation, profile_id, presentation_owner, read_owner, guard, validator):
        self._operation = operation
        self._profile_id = profile_id
        self._presentation_owner = presentation_owner
        self._read_owner = read_owner
        self._guard = guard
        self._validator = validator
        self._closed = False

    def _current(self, operation):
        if self._closed:
            raise PermissionError("saved context borrow is closed")
        if operation is not self._operation:
            raise PermissionError("saved context operation changed")
        self._guard()

    def resolve(self, operation, conversation):
        self._current(operation)
        _builtin_data(conversation)
        project = requires_project_owner(conversation)
        validator = self._validator() if project else None
        result = resolve_saved_project(
            conversation, profile_id=self._profile_id,
            presentation_owner=self._presentation_owner() if project else ("", ""), read_owner=self._read_owner,
            guard=self._guard, validate_projects=validator,
        )
        self._current(operation)
        return result

    def retain_effect(self, operation, conversation, accepted):
        self._current(operation)
        _builtin_data(conversation)
        result = retain_saved_project_effect_snapshot(
            conversation, accepted, profile_id=self._profile_id,
        )
        self._current(operation)
        return result

    def close(self):
        self._closed = True
        self._operation = None
        self._read_owner = self._guard = self._validator = None
        self._presentation_owner = None
        self._profile_id = ""
