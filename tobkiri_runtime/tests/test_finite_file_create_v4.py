"""Create-only Host CAS, approval ancestry, and finite tool denial tests."""

from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace as NS
from typing import Any

import pytest

from core_runtime.owned_file_approval_v4 import (
    BROKER,
    EXECUTOR,
    LOCAL,
    SAVED,
    authenticated_file_tool_owner,
    open_file_tool_approval,
    register_file_tool_request,
    close_file_tool_request,
    bind_file_effect,
    file_effect_execution_guard,
)
from ecosystem.rumi_default_tools_pack.runtime import file_create as create
from tobkiri_host.models import OpaqueAuthorityRef, RequestContext
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationBinding,
    WorkspaceMutationCoordinator,
)


def context(**changes: Any) -> RequestContext:
    value = RequestContext(
        request_id="request",
        trace_id="trace",
        caller_principal=OpaqueAuthorityRef("owner"),
        profile_id="profile",
        profile_revision="sha256:" + "e" * 64,
        activation_id="activation",
        activation_digest="sha256:" + "a" * 64,
        plan_digest="sha256:" + "b" * 64,
        security_epoch=3,
        caller_session_id="owner-session",
        caller_domain_id="caller-domain",
        caller_boot_epoch=1,
        target_domain_id="target-domain",
        target_boot_epoch=2,
        target_backend_digest="sha256:" + "c" * 64,
        profile_authority_digest="sha256:" + "d" * 64,
        fencing_token=4,
        handle_namespace="namespace",
    )
    return replace(value, **changes)


def envelope(operation, value=None):
    return NS(
        contract_id=operation[0],
        operation_id=operation[1],
        context=value or context(),
        target_principal=OpaqueAuthorityRef("target"),
    )


def ancestry():
    calls = []

    def guard():
        calls.append("guard")

    root = NS(envelope=envelope(SAVED), parent=None, assert_current=guard)
    broker = NS(envelope=envelope(BROKER), parent=root, assert_current=guard)
    executor = NS(envelope=envelope(EXECUTOR), parent=broker, assert_current=guard)
    invocation = NS(
        envelope=envelope(
            LOCAL,
            context(
                caller_principal=OpaqueAuthorityRef("executor"),
                caller_session_id="nested",
            ),
        ),
        parent_invocation=executor,
        assert_current=guard,
        presentation_owner_principal_id="owner",
        presentation_owner_session_id="owner-session",
    )
    return invocation, root, calls


def test_native_window_uses_actual_live_root_context():
    invocation, root, calls = ancestry()
    observed = []

    def get(query):
        assert query.context is root.envelope.context
        observed.append(query)
        return NS(request_id="approval", state="pending")

    def open_window(command):
        assert command.context is root.envelope.context
        assert command.presentation_owner_principal_id == "owner"
        observed.append(command)
        return {"opened": True, "request_id": "approval"}

    open_file_tool_approval(
        invocation,
        effect_status={
            "state": "approval_pending",
            "effect_id": "effect",
            "approval_request_id": "approval",
        },
        approval_port=NS(get_interactive_approval=get),
        window_port=NS(open_authority_approval_window=open_window),
    )
    assert len(observed) == 2 and len(calls) >= 12


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "foreign"),
        ("activation_id", "foreign"),
        ("security_epoch", 99),
        ("fencing_token", 99),
        ("plan_digest", "sha256:" + "f" * 64),
        ("caller_session_id", "foreign-session"),
        ("caller_principal", OpaqueAuthorityRef("foreign-owner")),
    ],
)
def test_native_bridge_rejects_foreign_or_stale_root(field, value):
    invocation, root, _ = ancestry()
    root.envelope.context = replace(root.envelope.context, **{field: value})
    with pytest.raises(PermissionError):
        authenticated_file_tool_owner(invocation)


def test_native_bridge_rejects_cancelled_parent_and_unknown_chain():
    invocation, _, _ = ancestry()
    invocation.parent_invocation.assert_current = lambda: (_ for _ in ()).throw(
        PermissionError("cancelled")
    )
    with pytest.raises(PermissionError, match="cancelled"):
        authenticated_file_tool_owner(invocation)
    invocation, _, _ = ancestry()
    invocation.parent_invocation.envelope.operation_id = "foreign"
    with pytest.raises(PermissionError, match="ancestry"):
        authenticated_file_tool_owner(invocation)


def test_native_bridge_foreign_request_record_cannot_open_window():
    invocation, _, _ = ancestry()
    opened = []

    def denied(_query):
        raise PermissionError("foreign durable approval owner")

    with pytest.raises(PermissionError, match="foreign durable"):
        open_file_tool_approval(
            invocation,
            effect_status={
                "state": "approval_pending",
                "effect_id": "effect",
                "approval_request_id": "foreign",
            },
            approval_port=NS(get_interactive_approval=denied),
            window_port=NS(open_authority_approval_window=lambda command: opened.append(command)),
        )
    assert not opened


def test_native_bridge_cancelled_after_record_read_never_opens_window():
    invocation, root, _ = ancestry()
    opened = []

    def queried(_query):
        root.assert_current = lambda: (_ for _ in ()).throw(PermissionError("cancelled"))
        return NS(request_id="approval", state="pending")

    with pytest.raises(PermissionError, match="cancelled"):
        open_file_tool_approval(
            invocation,
            effect_status={
                "state": "approval_pending",
                "effect_id": "effect",
                "approval_request_id": "approval",
            },
            approval_port=NS(get_interactive_approval=queried),
            window_port=NS(open_authority_approval_window=lambda command: opened.append(command)),
        )
    assert not opened


@pytest.mark.parametrize("state", ["approved", "denied", "expired"])
def test_native_window_cannot_replay_decided_or_expired_request(state):
    invocation, _, _ = ancestry()
    opened = []
    with pytest.raises(PermissionError, match="changed"):
        open_file_tool_approval(
            invocation,
            effect_status={
                "state": "approval_pending",
                "effect_id": "effect",
                "approval_request_id": "approval",
            },
            approval_port=NS(
                get_interactive_approval=lambda _: NS(request_id="approval", state=state)
            ),
            window_port=NS(open_authority_approval_window=lambda command: opened.append(command)),
        )
    assert not opened


@pytest.fixture
def real_create(tmp_path: Path, monkeypatch):
    root = tmp_path / "demo"
    root.mkdir()
    inode = root.stat()
    binding = WorkspaceMutationBinding("profile", "workspace", 1, root, inode.st_dev, inode.st_ino)
    selected = [binding]
    port = HostWorkspaceMutationPort(
        WorkspaceMutationCoordinator(tmp_path / "host-locks"),
        binding_resolver=lambda _profile, _workspace: selected[0],
    )
    capture = NS(profile_id="profile", user_data_root=tmp_path, workspace_mutation_port=port)
    monkeypatch.setattr(create, "_binding", lambda _: selected[0])
    invoke = NS(envelope=envelope((create.CONTRACT, create.EXECUTE)), assert_current=lambda: None)
    request = {
        "workspace_id": "workspace",
        "expected_mount_revision": 1,
        "path": "index.html",
        "content": "<html>Todo 日本語</html>",
    }
    origin, _, _ = ancestry()
    request = register_file_tool_request(request, origin)
    yield root, binding, selected, capture, invoke, request
    close_file_tool_request(request["invocation_key"])
    port.close()


def test_real_host_absent_create_and_replay_cannot_replace(real_create):
    root, _, _, capture, invocation, request = real_create
    plan = create._bind_prepare(capture)(request, invocation)
    assert not (root / "index.html").exists()
    result = create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    assert result["created"] and result["byte_count"] == len(request["content"].encode())
    assert (root / "index.html").read_text() == request["content"]
    with pytest.raises(Exception):
        create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    assert (root / "index.html").read_text() == request["content"]


def test_coordinator_freezes_content_and_shows_root_path_digest_not_source(real_create):
    from core_runtime.interactive_effect_coordinator import (
        INTERACTIVE_EFFECT_SPECS,
        InteractiveEffectUnavailable,
        _execute_payload,
        _presentation_metadata,
    )
    from tobkiri_protocol.canonical import canonical_digest

    _, binding, _, _, _, request = real_create
    plan = create.create_plan(request, binding)
    spec = INTERACTIVE_EFFECT_SPECS["file_create"]
    payload = _execute_payload(spec, request, plan)
    presentation = _presentation_metadata(
        spec,
        NS(request_digest=canonical_digest(payload), normalized_payload=payload),
    )
    assert str(binding.canonical_root) in presentation["detail"]
    assert "index.html" in presentation["detail"]
    assert plan["content_digest"] in presentation["detail"]
    assert request["content"] not in str(presentation)
    with pytest.raises(InteractiveEffectUnavailable):
        _execute_payload(spec, {**request, "content": "changed"}, plan)


def test_external_file_created_during_approval_is_preserved(real_create):
    root, _, _, capture, invocation, request = real_create
    plan = create._bind_prepare(capture)(request, invocation)
    (root / "index.html").write_text("external")
    with pytest.raises(Exception):
        create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    assert (root / "index.html").read_text() == "external"


def test_changed_content_mount_and_selected_workspace_fail_before_mutation(real_create):
    root, binding, selected, capture, invocation, request = real_create
    plan = create.create_plan(request, binding)
    with pytest.raises(PermissionError):
        create._bind_execute(capture)(
            {"request": {**request, "content": "changed"}, "plan": plan}, invocation
        )
    selected[0] = replace(binding, mount_revision=2)
    with pytest.raises(PermissionError):
        create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    selected[0] = replace(binding, workspace_id="foreign-workspace")
    with pytest.raises(PermissionError):
        create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    assert not (root / "index.html").exists()


def test_nofollow_jail_rejects_symlink_parent(real_create, tmp_path: Path):
    root, binding, _, capture, invocation, request = real_create
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    request = {**request, "path": "linked/index.html"}
    plan = create.create_plan(request, binding)
    with pytest.raises(Exception):
        create._bind_execute(capture)({"request": request, "plan": plan}, invocation)
    assert not (outside / "index.html").exists()


@pytest.mark.parametrize(
    "path", ["../escape", "/absolute", "a/../b", "./a", "a//b", "a\\b", "a\x00b"]
)
def test_finite_argument_jail_rejects_ambiguous_paths(path):
    with pytest.raises(ValueError):
        create.file_arguments({"path": path, "content": "test"})


@pytest.mark.parametrize("state", ["cancelled", "failed", "stale", "ambiguous"])
def test_tool_denial_never_invokes_execute(monkeypatch, real_create, state):
    _, binding, _, capture, _, _ = real_create
    calls = []

    def client_invoke(_contract, _operation, payload):
        calls.append(payload["phase"])
        return {
            "effect_id": "effect",
            "approval_request_id": "approval",
            "state": "approval_pending" if payload["phase"] == "prepare" else state,
        }

    capture.interactive_approval_port = object()
    capture.authority_approval_window_port = object()
    monkeypatch.setattr(create, "open_file_tool_approval", lambda *args, **kwargs: None)
    invocation, _, _ = ancestry()
    invocation.contract_client = lambda **kwargs: NS(invoke=client_invoke)
    with pytest.raises(PermissionError):
        create._bind_tool(capture)(
            {
                "tool_id": "coding_file_create",
                "tool_call_id": "call",
                "arguments": {"path": "index.html", "content": "test"},
            },
            invocation,
        )
    assert calls == ["prepare", "status", "cancel"]


def test_tool_resumes_once_after_approval_and_reports_success(monkeypatch, real_create):
    _, _, _, capture, _, _ = real_create
    calls = []

    def invoke(_contract, _operation, payload):
        phase = payload["phase"]
        calls.append(phase)
        return {
            "effect_id": "effect",
            "approval_request_id": "approval",
            "state": {
                "prepare": "approval_pending",
                "status": "approved",
                "resume": "succeeded",
            }[phase],
        }

    capture.interactive_approval_port = object()
    capture.authority_approval_window_port = object()
    monkeypatch.setattr(create, "open_file_tool_approval", lambda *args, **kwargs: None)
    invocation, _, _ = ancestry()
    invocation.contract_client = lambda **_: NS(invoke=invoke)
    result = create._bind_tool(capture)(
        {
            "tool_id": "coding_file_create",
            "tool_call_id": "call",
            "arguments": {"path": "index.html", "content": "test"},
        },
        invocation,
    )
    assert calls == ["prepare", "status", "resume"]
    assert json.loads(result["result"])["created"] and not result["is_error"]


@pytest.mark.parametrize("stage", ["pending", "approved", "queued"])
def test_original_stop_closes_host_lifetime_and_cancels_without_guarded_client(stage):
    invocation, root, _ = ancestry()
    request = register_file_tool_request({"path": "index.html", "content": "test"}, invocation)
    cancelled = []
    bind_file_effect(
        "effect-stop", request, invocation.envelope.context, lambda: cancelled.append(stage)
    )
    guard = file_effect_execution_guard("effect-stop", invocation.envelope.context)
    root.assert_current = lambda: (_ for _ in ()).throw(PermissionError("stopped"))
    with pytest.raises(PermissionError, match="stopped"):
        guard()
    close_file_tool_request(request["invocation_key"])
    assert cancelled == [stage]
    with pytest.raises(PermissionError):
        file_effect_execution_guard("effect-stop", invocation.envelope.context)


def test_private_lifetime_expires_and_missing_recovered_guard_cannot_resume(monkeypatch):
    from core_runtime import owned_file_approval_v4 as owner

    invocation, _, _ = ancestry()
    request = register_file_tool_request({"path": "index.html", "content": "test"}, invocation)
    bind_file_effect("effect-expiry", request, invocation.envelope.context, lambda: None)
    now = owner.time.monotonic()
    monkeypatch.setattr(owner.time, "monotonic", lambda: now + 91)
    with pytest.raises(PermissionError):
        file_effect_execution_guard("effect-expiry", invocation.envelope.context)
    close_file_tool_request(request["invocation_key"])
    with pytest.raises(PermissionError):
        file_effect_execution_guard("persisted-before-host-restart", invocation.envelope.context)


def test_lost_prepare_reply_closes_and_cancels_exact_host_effect(monkeypatch, real_create):
    _, _, _, capture, _, _ = real_create
    cancelled = []
    invocation, _, _ = ancestry()

    def invoke(_contract, _operation, payload):
        assert payload["phase"] == "prepare"
        bind_file_effect(
            "lost-effect",
            payload["request"],
            invocation.envelope.context,
            lambda: cancelled.append("lost-effect"),
        )
        raise TimeoutError("prepare reply lost")

    invocation.contract_client = lambda **_: NS(invoke=invoke)
    capture.interactive_approval_port = object()
    capture.authority_approval_window_port = object()
    with pytest.raises(TimeoutError, match="lost"):
        create._bind_tool(capture)(
            {
                "tool_id": "coding_file_create",
                "tool_call_id": "call",
                "arguments": {"path": "index.html", "content": "test"},
            },
            invocation,
        )
    assert cancelled == ["lost-effect"]
    with pytest.raises(PermissionError):
        file_effect_execution_guard("lost-effect", invocation.envelope.context)


def test_pending_controller_forwards_stop_guard_to_real_broker_before_backend():
    from tests.test_interactive_effects import (
        _MemoryPendingEffects,
        _Approvals,
        _controller,
        _prepare,
    )
    from tests.test_tobkiri_host_execution_integration import make_broker

    fixture = make_broker()
    approvals = _Approvals()
    controller = _controller(_MemoryPendingEffects(), approvals)
    stopped = [False]
    forwarded = []

    def guard():
        if stopped[0]:
            raise PermissionError("saved origin stopped while queued")

    original = fixture.broker.invoke_prepared

    def queued(snapshot, value, scope, **kwargs):
        assert kwargs["execution_guard"] is guard
        forwarded.append(True)
        stopped[0] = True
        return original(snapshot, value, scope, **kwargs)

    fixture.broker.invoke_prepared = queued
    try:
        pending, _ = _prepare(controller, fixture.broker)
        approvals.approve(pending.approval_request_id)
        controller.resume_for_presentation(
            effect_id=pending.effect_id,
            presentation_owner_principal_id="authority:presenter",
            presentation_owner_session_id="presenter-session",
            broker=fixture.broker,
            execution_guard=guard,
            wall_clock=lambda: 100.0,
            monotonic_clock=lambda: 10.0,
        )
        assert forwarded == [True]
        assert fixture.backend.invocations == 0
        assert "provider_invoked" not in fixture.events
    finally:
        fixture.broker.close()


def test_normative_fixture_new_functions_resolve_exact_candidate_bytes(tmp_path: Path):
    from scripts.generate_executable_source_registry_v1 import ECOSYSTEM, build_registry
    import hashlib

    repository = tmp_path / "repository"
    pack = repository / "tobkiri_runtime/ecosystem/rumi_default_tools_pack"
    (pack / "runtime").mkdir(parents=True)
    original = ECOSYSTEM / "rumi_default_tools_pack"
    for relative in ("rumi.pack.v3.json", "artifact-manifest.json", "runtime/calculator.py"):
        shutil.copyfile(original / relative, pack / relative)
    shutil.copyfile(create.__file__, pack / "runtime/file_create.py")
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/legacy_executable_sources.v1.json").read_text()
    )
    value = {
        "schema": fixture["schema"],
        "source_format": fixture["source_format"],
        "packs": {"rumi_default_tools_pack": fixture["packs"]["rumi_default_tools_pack"]},
    }
    fixture_path = repository / "fixture.json"
    fixture_path.write_text(json.dumps(value))
    registry = build_registry(pack.parent, fixture_path=fixture_path)
    expected = "sha256:" + hashlib.sha256(Path(create.__file__).read_bytes()).hexdigest()
    for function in create.HOST_PROVIDER_FACTORY:
        record = registry["packs"][function]
        assert record["implementation_digest"] == expected
        assert record["implementation_path"] == "runtime/file_create.py"
        assert len(record["operations"]) == 1


@pytest.mark.parametrize("source", ["fixture", "catalog"])
def test_integrated_file_and_mount_prepare_schemas_coexist(real_create, source):
    """Shared picker schemas must retain the real saved file prepare request."""
    from jsonschema import Draft202012Validator

    _, _, _, _, _, request = real_create
    runtime_root = Path(create.__file__).resolve().parents[3]
    if source == "fixture":
        fixture = json.loads(
            (runtime_root / "tests/fixtures/legacy_executable_sources.v1.json").read_text()
        )
        schema = next(
            item for item in fixture["packs"]["rumi_host_authority_bridge_pack"]["entries"]
            if item["function_id"]
            == "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
        )["schemas"]["input"]
    else:
        catalog = json.loads((runtime_root / "schemas/pack_v4_catalog.v1.json").read_text())
        pack = next(
            item for item in catalog["packs"]
            if item["pack_id"] == "rumi_host_authority_bridge_pack"
        )
        schema = next(
            item for item in pack["provided_contracts"]
            if item["contract_id"] == "tobkiri.service.interactive-effect.v1"
        )["schemas"]["input"]
    validator = Draft202012Validator(schema)
    for kind, payload in (("file_create", request), ("workspace_mount", {"selection_id": "ticket"})):
        prepare = {
            "phase": "prepare", "effect_kind": kind, "request": payload,
            "correlation_id": "00000000-0000-4000-8000-000000000001",
        }
        assert not list(validator.iter_errors(prepare))
        assert sum(Draft202012Validator(branch).is_valid(prepare) for branch in schema["oneOf"]) == 1
        assert not list(validator.iter_errors({
            "phase": "lookup", "effect_kind": kind,
            "correlation_id": prepare["correlation_id"],
        }))
        assert list(validator.iter_errors({**prepare, "approved": True}))
