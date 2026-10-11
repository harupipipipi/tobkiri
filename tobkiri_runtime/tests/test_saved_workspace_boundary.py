"""Canonical regression coverage for saved workspace-bound execution pins."""

from __future__ import annotations

import ast
from contextvars import copy_context
import hashlib
import os
from pathlib import Path
import sys
import time
from threading import Event, RLock, Thread
from types import SimpleNamespace
from typing import Any

import pytest


RUNTIME = Path(
    os.environ.get(
        "TOBKIRI_CANONICAL_TEST_RUNTIME",
        Path(__file__).resolve().parents[1],
    )
)
sys.path.insert(0, str(RUNTIME))

from core_runtime.capture_guard_traversal_v4 import _capture_guard_traversal  # noqa: E402
from core_runtime.invocation_scope_v4 import (  # noqa: E402
    CapturedInvocationScopeV4,
    ParentInvocationScopesV4,
)
from tobkiri_host.saved_tool_consent import (  # noqa: E402
    validate_python_sandbox_arguments,
    validate_python_sandbox_payload,
)
from tobkiri_host.saved_workspace_context import (  # noqa: E402
    ResolvedWorkspaceConversation,
    SavedWorkspaceExecutionBinding,
    SavedWorkspaceExecutionRegistry,
    require_saved_workspace_binding,
    resolve_saved_workspace,
    saved_workspace_binding,
    workspace_resolution,
)
from tobkiri_protocol.canonical import canonical_json  # noqa: E402
from tobkiri_protocol.saved_conversation import (  # noqa: E402
    validate_saved_conversation_context,
)


ROOT_ID = "saved-request-1"
ROOT_DIGEST = "sha256:" + "a" * 64


def _conversation(
    *, revision: int = 12, kind: str = "coding", mode: str = "coding"
) -> dict[str, Any]:
    return {
        "id": "conversation-1",
        "conversation_revision": revision,
        "conversation_kind": kind,
        "metadata": {"mode": mode, "workspace_id": "workspace-1"},
        "tags": [],
    }


def _owner_context(owner: ResolvedWorkspaceConversation) -> bytes:
    metadata = owner.get("metadata")
    return canonical_json(
        {
            "conversation_kind": owner.get("conversation_kind"),
            "metadata_mode": metadata.get("mode") if isinstance(metadata, dict) else None,
        }
    )


def _mount_binding(
    payload: dict[str, Any],
    *,
    mount_revision: int = 4,
    root_st_dev: int = 7,
    root_st_ino: int = 11,
) -> dict[str, Any]:
    assert payload == {
        "profile_id": "profile-1",
        "operation": "binding",
        "workspace_id": "workspace-1",
    }
    return {
        "mount": {"id": "workspace-1", "mount_revision": mount_revision},
        "binding": {
            "workspace_id": "workspace-1",
            "mount_revision": mount_revision,
            "root_st_dev": root_st_dev,
            "root_st_ino": root_st_ino,
        },
    }


def _resolve(
    raw: dict[str, Any] | None = None,
    *,
    mount_revision: int = 4,
    root_st_dev: int = 7,
    root_st_ino: int = 11,
) -> ResolvedWorkspaceConversation:
    return resolve_saved_workspace(
        _conversation() if raw is None else raw,
        profile_id="profile-1",
        read_binding=lambda payload: _mount_binding(
            payload,
            mount_revision=mount_revision,
            root_st_dev=root_st_dev,
            root_st_ino=root_st_ino,
        ),
        guard=lambda: None,
    )


def _selected(**changes: Any) -> dict[str, Any]:
    value: dict[str, Any] = {
        "workspace_id": "workspace-1",
        "access": "read_only",
        "canonical_root": "/synthetic/workspace-1",
        "mount_revision": 4,
        "root_st_dev": 7,
        "root_st_ino": 11,
    }
    value["root_identity"] = hashlib.sha256(canonical_json(value)).hexdigest()
    return {**value, **changes}


def _execution_binding(
    *,
    current_owner: ResolvedWorkspaceConversation | None = None,
    selected: dict[str, Any] | None = None,
    assert_live=lambda: None,
    read_owner=None,
) -> SavedWorkspaceExecutionBinding:
    owner = _resolve()
    return SavedWorkspaceExecutionBinding(
        saved_request_id=ROOT_ID,
        saved_request_digest=ROOT_DIGEST,
        resolution=workspace_resolution(owner),
        canonical_root="/synthetic/workspace-1",
        selected_workspace=lambda: selected or _selected(),
        read_owner=read_owner or (lambda: current_owner or owner),
        owner_context=_owner_context(owner),
        assert_live=assert_live,
    )


def _assert_current(binding: SavedWorkspaceExecutionBinding) -> None:
    lease = object()
    binding.lease.activate(lease)
    try:
        binding.assert_current()
    finally:
        binding.lease.release(lease)


def test_saved_workspace_effect_rechecks_owner_revision_and_mount_identity():
    for changed_owner in (
        _resolve(_conversation(revision=13)),
        _resolve(mount_revision=5),
        _resolve(root_st_dev=8),
        _resolve(root_st_ino=12),
    ):
        with pytest.raises(PermissionError, match="owner changed"):
            _assert_current(_execution_binding(current_owner=changed_owner))


@pytest.mark.parametrize(
    "change",
    [
        {"workspace_id": "workspace-2"},
        {"access": "read_write"},
        {"mount_revision": 5},
        {"canonical_root": "/synthetic/other"},
        {"root_st_dev": 8},
        {"root_st_ino": 12},
        {"root_identity": "0" * 64},
    ],
)
def test_saved_workspace_effect_rejects_selected_binding_drift(change: dict[str, Any]):
    with pytest.raises(PermissionError, match="selected binding changed"):
        _assert_current(_execution_binding(selected=_selected(**change)))


def test_saved_workspace_effect_checks_cancellation_each_time():
    cancelled = False

    def assert_live() -> None:
        if cancelled:
            raise PermissionError("saved workspace request was cancelled")

    binding = _execution_binding(assert_live=assert_live)
    _assert_current(binding)
    cancelled = True
    with pytest.raises(PermissionError, match="cancelled"):
        _assert_current(binding)


def test_saved_workspace_effect_rechecks_cancellation_after_mount_capture():
    cancelled = False

    def assert_live() -> None:
        if cancelled:
            raise PermissionError("saved workspace request was cancelled")

    def selected() -> dict[str, Any]:
        nonlocal cancelled
        cancelled = True
        return _selected()

    binding = SavedWorkspaceExecutionBinding(
        saved_request_id=ROOT_ID,
        saved_request_digest=ROOT_DIGEST,
        resolution=workspace_resolution(_resolve()),
        canonical_root="/synthetic/workspace-1",
        selected_workspace=selected,
        read_owner=_resolve,
        owner_context=_owner_context(_resolve()),
        assert_live=assert_live,
    )
    lease = object()
    binding.lease.activate(lease)
    try:
        with pytest.raises(PermissionError, match="cancelled"):
            binding.assert_current()
    finally:
        binding.lease.release(lease)


def test_workspace_binding_is_exact_ancestor_and_expires_with_its_lease():
    binding = _execution_binding()
    root = SimpleNamespace(
        envelope=SimpleNamespace(
            context=SimpleNamespace(request_id=ROOT_ID),
            request_digest=ROOT_DIGEST,
            contract_id="conversation.saved-turn.v1",
            operation_id="saved_complete",
        ),
        parent=None,
        saved_workspace_binding=binding,
        assert_current=lambda: None,
    )
    invocation = SimpleNamespace(parent_invocation=root, assert_current=lambda: None)
    lease = object()
    binding.lease.activate(lease)
    try:
        assert require_saved_workspace_binding(invocation) is binding
    finally:
        binding.lease.release(lease)
    with pytest.raises(PermissionError, match="lease expired"):
        require_saved_workspace_binding(invocation)

    invocation.parent_invocation = SimpleNamespace(
        parent=None, saved_workspace_binding=None, assert_current=lambda: None
    )
    assert saved_workspace_binding(invocation) is None
    with pytest.raises(PermissionError, match="unavailable"):
        require_saved_workspace_binding(invocation)


def test_file_create_keeps_optional_binding_while_python_sandbox_requires_one():
    source = (RUNTIME / "tobkiri_host/saved_tool_policy_execution.py").read_text()
    tree = ast.parse(source)
    call = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__call__"
    )
    file_create = next(
        node
        for node in ast.walk(call)
        if isinstance(node, ast.If)
        and "file-create-operation" in ast.unparse(node.test)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "saved_workspace_binding"
            for child in ast.walk(node)
        )
    )
    python_sandbox = next(
        node
        for node in ast.walk(call)
        if isinstance(node, ast.If)
        and "PYTHON_SANDBOX_OPERATION" in ast.unparse(node.test)
        and any(
            isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "require_saved_workspace_binding"
            for child in ast.walk(node)
        )
    )
    file_create_calls = {
        node.func.id
        for statement in file_create.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    sandbox_calls = {
        node.func.id
        for statement in python_sandbox.body
        for node in ast.walk(statement)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "saved_workspace_binding" in file_create_calls
    assert "require_saved_workspace_binding" not in file_create_calls
    assert "require_saved_workspace_binding" in sandbox_calls


def _production_capture_fixture(*, cancel_parent_during_owner_read: bool = False):
    counts = {"lease": {}, "dispatch": 0, "capture": 0}
    parents = ParentInvocationScopesV4()
    registry = SavedWorkspaceExecutionRegistry()
    state: dict[str, Any] = {"tool_scope": None, "tool_envelope": None}

    def assert_dispatched(envelope: Any, _authority: Any) -> None:
        request_id = envelope.context.request_id
        counts["lease"][request_id] = counts["lease"].get(request_id, 0) + 1
        if envelope.cancellation_requested.is_set():
            raise PermissionError("captured invocation cancelled")

    def assert_capture() -> None:
        counts["capture"] += 1

    class Dispatch:
        def assert_current(self) -> None:
            counts["dispatch"] += 1

    namespace = {
        "Any": Any,
        "AuthorityDenied": PermissionError,
        "CapturedInvocationScopeV4": CapturedInvocationScopeV4,
        "_capture_guard_traversal": _capture_guard_traversal,
        "assert_current_capture": assert_capture,
        "assert_dispatched_invocation": assert_dispatched,
        "authority_store": object(),
        "caller_session_bindings_lock": RLock(),
        "dispatch_holder": [Dispatch()],
        "parent_invocation_scopes": parents,
        "saved_tool_entry_guards": SimpleNamespace(capture=lambda _envelope: None),
        "saved_workspace_execution_registry": registry,
    }
    source = (RUNTIME / "core_runtime/bootstrap/production_v4.py").read_text()
    tree = ast.parse(source)
    capture = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "capture_invocation_scope"
    )
    exec(
        compile(ast.Module(body=[capture], type_ignores=[]), "production_v4", "exec"),
        namespace,
    )

    def envelope(request_id: str, session_id: str, contract: str, operation: str):
        return SimpleNamespace(
            context=SimpleNamespace(
                caller_session_id=session_id,
                request_id=request_id,
                profile_id="profile-1",
            ),
            request_digest=("sha256:" + request_id.encode().hex().ljust(64, "0"))[:71],
            contract_id=contract,
            operation_id=operation,
            cancellation_requested=Event(),
            deadline_monotonic=time.monotonic() + 60,
        )

    capture_scope = namespace["capture_invocation_scope"]
    root_envelope = envelope(
        ROOT_ID, "root-parent", "conversation.saved-turn.v1", "saved_complete"
    )
    root_envelope.request_digest = ROOT_DIGEST
    parents.retain("saved-to-tool", capture_scope(root_envelope))
    owner = _resolve()

    def read_owner():
        counts["owner_reads"] = counts.get("owner_reads", 0) + 1
        if state["tool_scope"] is None:
            return owner
        if cancel_parent_during_owner_read:
            state["tool_envelope"].cancellation_requested.set()
        owner_envelope = envelope(
            "owner-read-request",
            "tool-to-owner-read",
            "tobkiri.resource.conversation.v1",
            "rumi_conversation_store_pack.conversation-resource",
        )
        owner_scope = capture_scope(owner_envelope)
        assert owner_scope.saved_workspace_binding is None
        owner_scope.assert_current()
        return owner

    return (
        counts,
        parents,
        registry,
        capture_scope,
        envelope,
        _execution_binding(read_owner=read_owner),
        state,
    )


def test_ambient_guard_checks_liveness_and_effect_guard_rechecks_owner():
    counts, parents, registry, capture, envelope, binding, state = (
        _production_capture_fixture()
    )
    with registry.register(binding):
        tool_envelope = envelope(
            "tool-request",
            "saved-to-tool",
            "tobkiri.service.tool.invoke.v1",
            "rumi_tool_broker_pack.tool-invoke",
        )
        state["tool_envelope"] = tool_envelope
        tool_scope = capture(tool_envelope)
        assert tool_scope.saved_workspace_binding is binding
        state["tool_scope"] = tool_scope
        parents.retain("tool-to-owner-read", tool_scope)
        owner_reads_before_guard = counts.get("owner_reads", 0)
        tool_scope.assert_current()
        assert counts.get("owner_reads", 0) == owner_reads_before_guard
        assert counts["lease"].get("owner-read-request", 0) == 0
        assert counts["lease"].get("tool-request", 0) >= 1
        assert counts["dispatch"] >= 2
        assert counts["capture"] == 2

        # The explicit effect-boundary check still performs a fresh owner
        # read. Its nested Broker capture must validate the owner-read lease
        # and the active saved-tool parent before returning.
        binding.assert_current()
        assert counts.get("owner_reads", 0) == owner_reads_before_guard + 1
        assert counts["lease"].get("owner-read-request", 0) >= 1
        assert counts["lease"].get("tool-request", 0) >= 2
        parents.release("tool-to-owner-read")
    parents.release("saved-to-tool")


def test_owner_recheck_still_observes_parent_cancellation():
    counts, parents, registry, capture, envelope, binding, state = (
        _production_capture_fixture(cancel_parent_during_owner_read=True)
    )
    with registry.register(binding):
        tool_envelope = envelope(
            "tool-request",
            "saved-to-tool",
            "tobkiri.service.tool.invoke.v1",
            "rumi_tool_broker_pack.tool-invoke",
        )
        state["tool_envelope"] = tool_envelope
        tool_scope = capture(tool_envelope)
        state["tool_scope"] = tool_scope
        parents.retain("tool-to-owner-read", tool_scope)
        owner_reads_before_guard = counts.get("owner_reads", 0)
        tool_scope.assert_current()
        assert counts.get("owner_reads", 0) == owner_reads_before_guard
        assert counts["lease"].get("tool-request", 0) >= 1
        with pytest.raises(PermissionError, match="cancelled"):
            binding.assert_current()
        assert tool_envelope.cancellation_requested.is_set()
        assert counts.get("owner_reads", 0) == owner_reads_before_guard + 1
        assert counts["lease"].get("tool-request", 0) >= 1
        parents.release("tool-to-owner-read")
    parents.release("saved-to-tool")


def test_ambient_workspace_guard_rejects_cancel_without_owner_io():
    counts, parents, registry, capture, envelope, binding, state = (
        _production_capture_fixture()
    )
    with registry.register(binding):
        tool_envelope = envelope(
            "tool-request", "saved-to-tool",
            "tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke",
        )
        state["tool_envelope"] = tool_envelope
        tool_scope = capture(tool_envelope)
        owner_reads = counts["owner_reads"]
        tool_envelope.cancellation_requested.set()
        with pytest.raises(PermissionError, match="cancelled"):
            tool_scope.assert_current()
        assert counts["owner_reads"] == owner_reads
    parents.release("saved-to-tool")


@pytest.mark.parametrize("failed_owner_read", [False, True])
def test_copied_owner_recheck_context_does_not_skip_later_fresh_read(
    failed_owner_read: bool,
):
    """A copied ContextVar must expire with its dynamic owner-read scope."""
    state: dict[str, Any] = {"reads": 0, "context": None, "fail_next": False}

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        state["context"] = copy_context()
        if state["fail_next"]:
            state["fail_next"] = False
            raise ValueError("owner read failed")
        return _resolve()

    binding = _execution_binding(read_owner=read_owner)
    registry = SavedWorkspaceExecutionRegistry()
    with registry.register(binding):
        # register() validates the binding with one full owner read. The
        # copied context was captured while that read's private recheck scope
        # was active, but the dispatch lease is still live here.
        assert state["reads"] == 1
        if failed_owner_read:
            state["fail_next"] = True
            with pytest.raises(ValueError, match="owner read failed"):
                binding.assert_current()
        copied_context = state["context"]
        assert copied_context is not None
        reads_before = state["reads"]
        copied_context.run(binding.assert_current)
        assert state["reads"] == reads_before + 1


def test_shared_owner_check_runs_once_per_guard_and_again_on_exit():
    """Repeated checks share one entry read, then get a fresh exit read."""
    state = {"reads": 0}

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        return _resolve()

    binding = _execution_binding(read_owner=read_owner)
    lease_token = object()
    binding.lease.activate(lease_token)
    try:
        with _capture_guard_traversal(binding.assert_current):
            assert state["reads"] == 1
            binding.assert_current()
            binding.assert_current()
            assert state["reads"] == 1
        assert state["reads"] == 2
    finally:
        binding.lease.release(lease_token)


def test_shared_owner_check_rejects_owner_change_at_guard_exit():
    """The traversal exit check rejects owner mutation after its entry read."""
    state = {"reads": 0}
    owners = [_resolve(), _resolve(_conversation(revision=13))]

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        return owners.pop(0)

    binding = _execution_binding(read_owner=read_owner)
    lease_token = object()
    binding.lease.activate(lease_token)
    try:
        with pytest.raises(PermissionError, match="owner changed"):
            with _capture_guard_traversal(binding.assert_current):
                assert state["reads"] == 1
                binding.assert_current()
                assert state["reads"] == 1
        assert state["reads"] == 2
    finally:
        binding.lease.release(lease_token)


def test_shared_owner_check_does_not_reuse_expired_copied_traversal():
    """A copied guard context cannot reuse checks after traversal exit."""
    state: dict[str, Any] = {"reads": 0, "context": None}

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        return _resolve()

    binding = _execution_binding(read_owner=read_owner)
    lease_token = object()
    binding.lease.activate(lease_token)
    try:
        with _capture_guard_traversal(binding.assert_current):
            state["context"] = copy_context()
            binding.assert_current()
            assert state["reads"] == 1
        assert state["reads"] == 2

        copied_context = state["context"]
        assert copied_context is not None
        copied_context.run(binding.assert_current)
        assert state["reads"] == 3
    finally:
        binding.lease.release(lease_token)


def test_shared_owner_check_does_not_reuse_traversal_on_another_thread():
    """A propagated context from another thread must perform its own read."""
    state = {"reads": 0}
    worker_errors: list[BaseException] = []

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        return _resolve()

    binding = _execution_binding(read_owner=read_owner)
    lease_token = object()
    binding.lease.activate(lease_token)
    try:
        with _capture_guard_traversal(binding.assert_current):
            assert state["reads"] == 1
            copied_context = copy_context()

            def run_in_worker() -> None:
                try:
                    copied_context.run(binding.assert_current)
                except BaseException as error:
                    worker_errors.append(error)

            worker = Thread(target=run_in_worker)
            worker.start()
            worker.join(timeout=5)
            assert not worker.is_alive()
            assert worker_errors == []
            assert state["reads"] == 2

            # The worker did not poison or populate the calling thread's
            # per-traversal check set.
            binding.assert_current()
            assert state["reads"] == 2
        assert state["reads"] == 3
    finally:
        binding.lease.release(lease_token)


def test_shared_owner_check_never_skips_lease_or_cancellation_checks():
    """Physical owner reads dedupe; live lease and request checks do not."""
    state: dict[str, Any] = {"reads": 0, "cancelled": False, "live_checks": 0}

    def read_owner() -> ResolvedWorkspaceConversation:
        state["reads"] += 1
        return _resolve()

    def assert_live() -> None:
        state["live_checks"] += 1
        if state["cancelled"]:
            raise PermissionError("saved workspace request was cancelled")

    binding = _execution_binding(read_owner=read_owner, assert_live=assert_live)
    lease_token = object()
    binding.lease.activate(lease_token)
    try:
        with _capture_guard_traversal(binding.assert_current):
            assert state["reads"] == 1
            binding.assert_current()
            assert state["reads"] == 1

            state["cancelled"] = True
            with pytest.raises(PermissionError, match="cancelled"):
                binding.assert_current()
            state["cancelled"] = False

            binding.lease.release(lease_token)
            with pytest.raises(PermissionError, match="lease expired"):
                binding.assert_current()
            binding.lease.activate(lease_token)

            binding.assert_current()
            assert state["reads"] == 1
        assert state["reads"] == 2
        assert state["live_checks"] >= 5
    finally:
        binding.lease.release(lease_token)


def test_python_sandbox_consent_validator_closes_modes_and_tool_envelope():
    valid = {
        "mode": "script",
        "script_path": "src/main.py",
        "arguments": ["--safe"],
        "stdin": "",
        "cwd": ".",
        "timeout_seconds": 60,
    }
    assert validate_python_sandbox_arguments(valid) == valid
    payload = {
        "tool_id": "python_sandbox_execute",
        "tool_call_id": "call-1",
        "arguments": valid,
    }
    assert validate_python_sandbox_payload(payload) == payload

    for change in (
        {"workspace_snapshot": []},
        {"cwd": "../outside"},
        {"timeout_seconds": True},
        {"arguments": [""]},
        {"script_path": "/tmp/main.py"},
    ):
        invalid = {**valid, **change}
        with pytest.raises(ValueError):
            validate_python_sandbox_arguments(invalid)
    with pytest.raises(ValueError):
        validate_python_sandbox_payload({**payload, "approved": True})
    with pytest.raises(ValueError):
        validate_python_sandbox_payload({**payload, "tool_call_id": "bad id"})


def test_saved_conversation_owner_projection_stays_host_resolved():
    owner = _resolve()
    validate_saved_conversation_context(
        owner, workspace_resolution=workspace_resolution(owner)
    )
    with pytest.raises(ValueError, match="resolution"):
        validate_saved_conversation_context(dict(owner))
