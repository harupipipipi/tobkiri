"""Fresh canonical workspace owner reads for saved-turn context."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, replace
import hashlib
from threading import RLock
import re
from typing import Any, Iterator

from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.saved_conversation import (
    SavedWorkspaceResolution,
    validate_saved_conversation_context,
)

WORKSPACE_TARGET = (
    "tobkiri.resource.workspace.v1",
    "rumi_workspace_mount_pack.workspace-resource",
)
_SAVED_WORKSPACE_OWNER_RECHECK: ContextVar[object | None] = ContextVar(
    "saved_workspace_owner_recheck", default=None
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


class SavedWorkspaceExecutionLease:
    """Single-dispatch lifetime for every inherited workspace proof."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._token: object | None = None

    def activate(self, token: object) -> None:
        """Activate this binding for exactly one saved tool dispatch."""
        with self._lock:
            if self._token is not None:
                raise PermissionError("saved workspace execution lease is active")
            self._token = token

    def release(self, token: object) -> None:
        """End the exact dispatch that owns this proof."""
        with self._lock:
            if self._token is token:
                self._token = None

    def assert_active(self) -> None:
        """Reject captured descendants after the saved dispatch has ended."""
        with self._lock:
            if self._token is None:
                raise PermissionError("saved workspace execution lease expired")


@dataclass(frozen=True)
class SavedWorkspaceExecutionBinding:
    """Private exact owner and selected-mount proof for one saved tool effect."""

    saved_request_id: str
    saved_request_digest: str
    resolution: SavedWorkspaceResolution
    canonical_root: str
    selected_workspace: Callable[[], Mapping[str, Any]]
    read_owner: Callable[[], Mapping[str, Any]]
    owner_context: bytes
    assert_live: Callable[[], None]
    lease: SavedWorkspaceExecutionLease = field(
        default_factory=SavedWorkspaceExecutionLease, compare=False, repr=False
    )

    def assert_current(self) -> None:
        """Re-read the exact saved owner, active request and Host-selected mount."""
        if (
            not self.saved_request_id
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", self.saved_request_digest)
            or type(self.resolution) is not SavedWorkspaceResolution
            or not self.canonical_root
            or not callable(self.assert_live)
            or type(self.lease) is not SavedWorkspaceExecutionLease
        ):
            raise PermissionError("saved workspace execution binding is invalid")
        self.lease.assert_active()
        self.assert_live()
        if _SAVED_WORKSPACE_OWNER_RECHECK.get() is self:
            # The active Host owner read creates Broker descendants whose
            # parent ancestry includes this binding. Keep those descendants'
            # own lease, cancellation, parent, and dispatch guards live while
            # avoiding recursively launching another owner read.
            return
        token = _SAVED_WORKSPACE_OWNER_RECHECK.set(self)
        try:
            current = self.read_owner()
        finally:
            _SAVED_WORKSPACE_OWNER_RECHECK.reset(token)
        fresh = workspace_resolution(current)
        metadata = current.get("metadata")
        if (
            fresh != self.resolution
            or type(current) is not ResolvedWorkspaceConversation
            or not isinstance(metadata, Mapping)
            or metadata.get("workspace_id") != self.resolution.workspace_id
            or canonical_json(
                {
                    "conversation_kind": current.get("conversation_kind"),
                    "metadata_mode": metadata.get("mode"),
                }
            )
            != self.owner_context
            or current.get("conversation_revision")
            != self.resolution.conversation_revision
        ):
            raise PermissionError("saved workspace owner changed before effect")
        self.assert_live()
        selected = self.selected_workspace()
        self.assert_live()
        selected_identity = {
            "workspace_id": self.resolution.workspace_id,
            "access": "read_only",
            "canonical_root": self.canonical_root,
            "mount_revision": self.resolution.mount_revision,
            "root_st_dev": self.resolution.root_st_dev,
            "root_st_ino": self.resolution.root_st_ino,
        }
        if (
            not isinstance(selected, Mapping)
            or any(
                selected.get(key) != value
                for key, value in selected_identity.items()
            )
            or selected.get("root_identity")
            != hashlib.sha256(canonical_json(selected_identity)).hexdigest()
        ):
            raise PermissionError("saved workspace selected binding changed")


class SavedWorkspaceExecutionRegistry:
    """Retain one exact owner proof only while its saved tool dispatch is live."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._entries: dict[
            tuple[str, str], tuple[SavedWorkspaceExecutionBinding, object]
        ] = {}

    @contextmanager
    def register(
        self, binding: SavedWorkspaceExecutionBinding
    ) -> Iterator[None]:
        """Bind a proof to one exact Host saved request and its accepted digest."""
        if type(binding) is not SavedWorkspaceExecutionBinding:
            raise PermissionError("saved workspace execution binding is invalid")
        key = (binding.saved_request_id, binding.saved_request_digest)
        with self._lock:
            if key in self._entries:
                raise PermissionError("saved workspace execution is already registered")
        token = object()
        binding.lease.activate(token)
        try:
            binding.assert_current()
            with self._lock:
                if key in self._entries:
                    raise PermissionError(
                        "saved workspace execution is already registered"
                    )
                self._entries[key] = (binding, token)
        except BaseException:
            binding.lease.release(token)
            raise
        try:
            yield
        finally:
            with self._lock:
                current = self._entries.get(key)
                if current is not None and current[1] is token:
                    del self._entries[key]
            binding.lease.release(token)

    def capture(
        self, envelope: Any, parent: Any
    ) -> SavedWorkspaceExecutionBinding | None:
        """Capture only through an exact saved_complete ancestor scope."""
        if (envelope.contract_id, envelope.operation_id) not in {
            ("tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke"),
            (
                "tobkiri.service.tool.local.operation.v1",
                "rumi_tool_local_executor_pack.tool-local-execute",
            ),
            ("tobkiri.service.file.create.v1", "rumi_default_tools_pack.file-create"),
        }:
            return None
        scopes: list[Any] = []
        scope = parent
        while scope is not None:
            if len(scopes) >= 16 or any(scope is prior for prior in scopes):
                raise PermissionError("saved workspace ancestry is invalid")
            scope.assert_current()
            scopes.append(scope)
            scope = scope.parent
        matches: list[SavedWorkspaceExecutionBinding] = []
        with self._lock:
            for ancestor in scopes:
                request_id = ancestor.envelope.context.request_id
                request_digest = ancestor.envelope.request_digest
                entry = self._entries.get((request_id, request_digest))
                if entry is None:
                    continue
                if (
                    (ancestor.envelope.contract_id, ancestor.envelope.operation_id)
                    != ("conversation.saved-turn.v1", "saved_complete")
                ):
                    raise PermissionError("saved workspace owner scope changed")
                matches.append(entry[0])
        if len(matches) > 1:
            raise PermissionError("saved workspace ancestry is ambiguous")
        if not matches:
            return None
        binding = matches[0]
        if (
            getattr(envelope.context, "profile_id", None)
            != binding.resolution.profile_id
        ):
            raise PermissionError("saved workspace profile changed")
        binding.assert_current()
        return binding


def require_saved_workspace_binding(invocation: Any) -> SavedWorkspaceExecutionBinding:
    """Require the exact workspace proof inherited through captured scopes."""
    binding = saved_workspace_binding(invocation)
    if binding is None:
        raise PermissionError("saved workspace proof is unavailable")
    return binding


def saved_workspace_binding(
    invocation: Any,
) -> SavedWorkspaceExecutionBinding | None:
    """Return an inherited saved owner proof, if this call has one."""
    invocation.assert_current()
    found: list[SavedWorkspaceExecutionBinding] = []
    scopes: list[Any] = []
    scope = getattr(invocation, "parent_invocation", None)
    while scope is not None:
        if len(scopes) >= 16 or any(scope is prior for prior in scopes):
            raise PermissionError("saved workspace ancestry is invalid")
        scope.assert_current()
        scopes.append(scope)
        binding = getattr(scope, "saved_workspace_binding", None)
        if binding is not None:
            if type(binding) is not SavedWorkspaceExecutionBinding:
                raise PermissionError("saved workspace ancestry proof is invalid")
            found.append(binding)
        scope = scope.parent
    if not found:
        return None
    if any(item is not found[0] for item in found[1:]):
        raise PermissionError("saved workspace ancestry proof is ambiguous")
    found[0].assert_current()
    return found[0]


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
