"""Workspace task approval bindings through the Host coordinator and Broker."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
import time
from pathlib import Path
from types import MappingProxyType, SimpleNamespace
from typing import Any, Iterator, Mapping

import pytest
from jsonschema import Draft202012Validator, ValidationError

from core_runtime.authority.v4 import AuthorityScope
from core_runtime.host_contract import bind_host_contract
from core_runtime.interactive_effect_coordinator import (
    CapturedInteractiveEffectRoute,
    HostInteractiveEffectService,
    INTERACTIVE_EFFECT_SPECS,
    InteractiveEffectUnavailable,
    _execute_payload,
    _presentation_metadata,
)
from tests import test_interactive_approval_v4 as approval_tests
from tests.test_authority_v4_lifecycle import _Harness
from tests.test_interactive_effect_coordinator import _coordinator_context
from tests.test_tobkiri_host_authority_v4_adapter import _Admission, _NoAdapters
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import RequestBroker
from tobkiri_host.contracts import AdapterPlanner, OperationCatalog, OperationRoute
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.interactive_effects import PendingEffectController
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef, RequestContext
from tobkiri_host.ports import (
    InteractiveEffectOwnerQuery,
    InteractiveEffectPrepareCommand,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.workspace_task_v1 import EXECUTE, PREPARE, TASK_CONTRACT


def _request(context: RequestContext) -> dict[str, Any]:
    return {
        "task_request_id": "request.portable",
        "profile_id": context.profile_id,
        "workspace_id": "workspace.portable",
        "expected_revision": 4,
        "expected_writer_epoch": 2,
        "argv": ["python", "-m", "pytest"],
        "timeout_seconds": 30,
    }


def _result(
    context: RequestContext,
    request: Mapping[str, Any],
    *,
    expires_at_ms: int = 160_000,
) -> dict[str, Any]:
    plan = {
        **request,
        "version": "tobkiri.workspace-task-plan.v1",
        "task_id": "task.portable",
        "plan_digest": context.plan_digest,
        "security_epoch": context.security_epoch,
        "checkpoint_digest": canonical_digest({"checkpoint": 4}),
        "recipe_digest": canonical_digest({"recipe": "python"}),
        "image_reference": "python@sha256:" + "a" * 64,
        "expires_at_ms": expires_at_ms,
        "request_digest": canonical_digest(dict(request)),
    }
    return {
        "executed": False,
        "task_plan": plan,
        "task_plan_digest": canonical_digest(plan),
    }


def test_workspace_task_has_only_the_finite_prepare_execute_pair() -> None:
    spec = INTERACTIVE_EFFECT_SPECS["workspace_task"]
    assert spec.prepare_contract_id == spec.execute_contract_id == TASK_CONTRACT
    assert spec.prepare_operation_id == PREPARE
    assert spec.execute_operation_id == EXECUTE
    context = _coordinator_context()
    request = _request(context)
    result = _result(context, request)
    payload = _execute_payload(spec, request, result, context=context, now=100.0)
    assert payload == {
        "task_plan": result["task_plan"],
        "task_plan_digest": result["task_plan_digest"],
    }
    assert payload["task_plan"] is not result["task_plan"]


@pytest.mark.parametrize(
    "patch",
    [
        {"approved": True},
        {"presentation": {"action": "trusted"}},
        {"timeout_seconds": True},
        {"timeout_seconds": 0},
        {"timeout_seconds": 121},
        {"expected_revision": True},
        {"expected_revision": 0},
        {"expected_writer_epoch": 1.5},
        {"profile_id": "../foreign"},
        {"task_request_id": "../foreign"},
        {"task_request_id": "n" * 129},
        {"argv": "python -m pytest"},
        {"argv": []},
        {"argv": ["-c", "command"]},
        {"argv": ["python", "\x00"]},
        {"argv": ["python"] * 65},
        {"argv": ["python", "a" * 8192, "b" * 8192]},
    ],
)
def test_workspace_task_rejects_malformed_and_authority_bearing_request(
    patch: dict[str, Any],
) -> None:
    context = _coordinator_context()
    request = {**_request(context), **patch}
    with pytest.raises(InteractiveEffectUnavailable, match="unavailable"):
        _execute_payload(
            INTERACTIVE_EFFECT_SPECS["workspace_task"],
            request,
            _result(context, _request(context)),
            context=context,
            now=100.0,
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"workspace_id": "workspace.foreign"},
        {"task_request_id": "request.foreign"},
        {"expected_revision": 5},
        {"expected_writer_epoch": 3},
        {"argv": ["python", "different"]},
        {"timeout_seconds": 31},
        {"profile_id": "profile.foreign"},
        {"plan_digest": canonical_digest({"plan": "foreign"})},
        {"security_epoch": 8},
        {"security_epoch": True},
        {"version": "tobkiri.workspace-task-plan.v2"},
        {"image_reference": "python:latest"},
        {"expires_at_ms": 100_000},
        {"request_digest": canonical_digest({"request": "foreign"})},
        {"owner_id": "private-owner"},
        {"session_id": "private-session"},
        {"lease": "private-lease"},
        {"host_path": "/private/workspace"},
    ],
)
def test_workspace_task_rejects_resealed_plan_outside_the_request_or_capture(
    patch: dict[str, Any],
) -> None:
    context = _coordinator_context()
    request = _request(context)
    result = _result(context, request)
    result["task_plan"].update(patch)
    result["task_plan_digest"] = canonical_digest(result["task_plan"])
    with pytest.raises(InteractiveEffectUnavailable, match="unavailable"):
        _execute_payload(
            INTERACTIVE_EFFECT_SPECS["workspace_task"],
            request,
            result,
            context=context,
            now=100.0,
        )


@pytest.mark.parametrize(
    "patch",
    [
        {"executed": True},
        {"executed": 0},
        {"approved": True},
        {"task_plan_digest": canonical_digest({"plan": "other"})},
        {"task_plan": None},
    ],
)
def test_workspace_task_rejects_invalid_provider_prepare_result(
    patch: dict[str, Any],
) -> None:
    context = _coordinator_context()
    request = _request(context)
    result = {**_result(context, request), **patch}
    with pytest.raises(InteractiveEffectUnavailable, match="unavailable"):
        _execute_payload(
            INTERACTIVE_EFFECT_SPECS["workspace_task"],
            request,
            result,
            context=context,
            now=100.0,
        )


def test_workspace_task_requires_a_host_context_even_for_a_valid_plan() -> None:
    context = _coordinator_context()
    request = _request(context)
    with pytest.raises(InteractiveEffectUnavailable):
        _execute_payload(
            INTERACTIVE_EFFECT_SPECS["workspace_task"],
            request,
            _result(context, request),
            now=100.0,
        )


def test_workspace_task_presentation_uses_only_a_frozen_redacted_plan() -> None:
    context = _coordinator_context()
    request = {
        **_request(context),
        "argv": [
            "python",
            "--token",
            "private-token",
            "password=private-password",
            "a" * 8192,
            *["test"] * 40,
        ],
    }
    result = _result(context, request)
    plan = {**result["task_plan"], "argv": tuple(request["argv"])}
    frozen = MappingProxyType(
        {
            "task_plan": MappingProxyType(plan),
            "task_plan_digest": result["task_plan_digest"],
        }
    )
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["workspace_task"],
        SimpleNamespace(
            normalized_payload=frozen,
            request_digest=canonical_digest(result["task_plan"]),
        ),
    )
    assert metadata["action"] == "Run workspace container task"
    assert "workspace.portable" in metadata["detail"]
    assert "python@sha256:" in metadata["detail"]
    assert "Timeout: 30 seconds" in metadata["detail"]
    assert "private-token" not in str(metadata)
    assert "private-password" not in str(metadata)
    assert all(len(value) <= 2048 for value in metadata.values())
    assert metadata["confirmation_phrase"] == "EXECUTE"


@pytest.mark.parametrize("mutate", ["digest", "request", "extra"])
def test_workspace_task_presentation_revalidates_the_broker_snapshot(
    mutate: str,
) -> None:
    context = _coordinator_context()
    request = _request(context)
    result = _result(context, request)
    payload = {
        "task_plan": result["task_plan"],
        "task_plan_digest": result["task_plan_digest"],
    }
    if mutate == "digest":
        payload["task_plan"]["argv"].append("different")
    elif mutate == "request":
        payload["task_plan"]["request_digest"] = canonical_digest({})
        payload["task_plan_digest"] = canonical_digest(payload["task_plan"])
    else:
        payload["presentation"] = {"action": "client approval"}
    with pytest.raises(InteractiveEffectUnavailable):
        _presentation_metadata(
            INTERACTIVE_EFFECT_SPECS["workspace_task"],
            SimpleNamespace(
                normalized_payload=payload, request_digest=canonical_digest(payload)
            ),
        )


@pytest.fixture
def host_workspace_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[SimpleNamespace]:
    """Use the real Authority, approval, pending store, and RequestBroker."""

    principal = approval_tests._principal
    monkeypatch.setattr(
        approval_tests,
        "_principal",
        lambda seed: principal(
            seed, operation=EXECUTE if seed == "interactive-execute" else "invoke"
        ),
    )
    scope = AuthorityScope(
        capability="workspace.task.execute",
        semantics_digest=canonical_digest({"workspace-task": 1}),
    )
    harness = _Harness(tmp_path, scope=scope)
    harness.clock.value = time.time()
    edge = approval_tests._InteractiveEdge(harness)
    adapter = AuthorityV4Adapter(
        harness.kernel,
        approval_tests._Principals(
            harness.caller,
            harness.target,
            edge.coordinator,
            edge.execute_target,
        ),
    )
    backend = approval_tests._ExecuteBackend(edge)
    artifact = approval_tests._execute_artifact(edge)
    function = artifact.functions[0]
    operation = replace(
        function.operations[0],
        contract_id=TASK_CONTRACT,
        operation_id=EXECUTE,
        idempotency="none",
        reconcile_operation=None,
    )
    artifact = replace(
        artifact, functions=(replace(function, operations=(operation,)),)
    )
    catalog = OperationCatalog(
        (artifact,),
        (
            OperationRoute(
                contract_id=TASK_CONTRACT,
                operation_id=EXECUTE,
                artifact_digest=artifact.digest,
                function_id=edge.execute_target.function_id,
                variant_id="execute.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(
                    edge.execute_target.principal_id
                ),
            ),
        ),
    )
    broker = RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=_Admission(),
        authority=adapter,
        audit=adapter,
        reconciliation=InMemoryReconciliationStore(),
    )
    controller = PendingEffectController(
        persistence=adapter,
        approvals=adapter,
        coordinator_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        coordinator_publisher_lineage="publisher.coordinator",
        clock=harness.clock,
    )
    outer = approval_tests._context(harness)
    inner = approval_tests._execute_context(harness, edge, request_id="task.execute")
    route = CapturedInteractiveEffectRoute(
        spec=INTERACTIVE_EFFECT_SPECS["workspace_task"],
        coordinator_principal=OpaqueAuthorityRef(edge.coordinator.principal_id),
        execute_target_principal=OpaqueAuthorityRef(edge.execute_target.principal_id),
        execute_ceiling=scope,
    )
    service = HostInteractiveEffectService(
        broker=broker,
        controller=controller,
        routes=(route,),
        context_for_execute=lambda _route, _outer: inner,
        assert_current_capture=lambda: None,
        profile_id=outer.profile_id,
        activation_id=outer.activation_id,
        plan_digest=outer.plan_digest,
        security_epoch=outer.security_epoch,
        clock=harness.clock,
    )
    fixture = SimpleNamespace(
        harness=harness,
        adapter=adapter,
        edge=edge,
        backend=backend,
        broker=broker,
        controller=controller,
        service=service,
        outer=outer,
        inner=inner,
    )
    try:
        yield fixture
    finally:
        broker.close()


def _prepare(fixture: SimpleNamespace) -> Any:
    request = _request(fixture.outer)
    return fixture.service.prepare_interactive_effect(
        InteractiveEffectPrepareCommand(
            context=fixture.outer,
            coordinator_principal=OpaqueAuthorityRef(
                fixture.edge.coordinator.principal_id
            ),
            presentation_owner_principal_id=fixture.harness.caller.principal_id,
            presentation_owner_session_id="session-caller",
            effect_kind="workspace_task",
            payload=request,
            prepared_result=_result(
                fixture.outer,
                request,
                expires_at_ms=int(fixture.harness.clock() * 1000) + 60_000,
            ),
        )
    )


def _owner(fixture: SimpleNamespace, effect_id: str) -> InteractiveEffectOwnerQuery:
    return InteractiveEffectOwnerQuery(
        context=fixture.outer,
        effect_id=effect_id,
        coordinator_principal=OpaqueAuthorityRef(fixture.edge.coordinator.principal_id),
        presentation_owner_principal_id=fixture.harness.caller.principal_id,
        presentation_owner_session_id="session-caller",
    )


def _decide(fixture: SimpleNamespace, request_id: str, action: str) -> None:
    with bind_host_contract(approval_tests._CONTRACT):
        command = approval_tests._decision_command(
            fixture.harness,
            request_id,
            phrase="EXECUTE",
            action=action,
        )
        if action == "approve":
            fixture.adapter.approve_interactive_approval(command)
        else:
            fixture.adapter.deny_interactive_approval(command)


def test_workspace_task_approval_resumes_the_exact_snapshot_once(
    host_workspace_task: SimpleNamespace,
) -> None:
    fixture = host_workspace_task
    pending = _prepare(fixture)
    assert pending.expires_at == (int(fixture.harness.clock() * 1000) + 60_000) / 1000
    assert fixture.backend.invocations == 0
    stored = fixture.harness.store.get_host_pending_effect(pending.effect_id)
    assert stored is not None
    payload = stored[1]["prepared"]["normalized_payload"]
    assert set(payload) == {"task_plan", "task_plan_digest"}
    assert payload["task_plan"]["profile_id"] == fixture.outer.profile_id
    assert (
        fixture.service.resume_interactive_effect(
            _owner(fixture, pending.effect_id)
        ).state
        == "approval_pending"
    )
    _decide(fixture, pending.approval_request_id, "approve")
    assert (
        fixture.service.resume_interactive_effect(
            _owner(fixture, pending.effect_id)
        ).state
        == "succeeded"
    )
    assert (
        fixture.service.resume_interactive_effect(
            _owner(fixture, pending.effect_id)
        ).state
        == "succeeded"
    )
    assert fixture.backend.invocations == 1
    execution_states = [
        event["event_state"]
        for event in fixture.harness.store.audit_events()
        if event["event_state"] in {"reserved", "dispatched", "committed"}
    ]
    assert execution_states == [
        "reserved",
        "dispatched",
        "committed",
    ]


@pytest.mark.parametrize("action", ["cancel", "deny", "expire"])
def test_workspace_task_cancel_denied_or_expired_cannot_dispatch(
    host_workspace_task: SimpleNamespace,
    action: str,
) -> None:
    fixture = host_workspace_task
    pending = _prepare(fixture)
    if action == "cancel":
        assert (
            fixture.service.cancel_interactive_effect(
                _owner(fixture, pending.effect_id)
            ).state
            == "cancelled"
        )
    elif action == "deny":
        _decide(fixture, pending.approval_request_id, "deny")
    else:
        _decide(fixture, pending.approval_request_id, "approve")
        fixture.harness.clock.value += 60
    state = fixture.service.resume_interactive_effect(
        _owner(fixture, pending.effect_id)
    ).state
    assert state in {"cancelled", "stale"}
    assert fixture.backend.invocations == 0


def test_workspace_task_direct_broker_execute_has_no_profile_grant(
    host_workspace_task: SimpleNamespace,
) -> None:
    fixture = host_workspace_task
    request = _request(fixture.outer)
    payload = _execute_payload(
        INTERACTIVE_EFFECT_SPECS["workspace_task"],
        request,
        _result(
            fixture.outer,
            request,
            expires_at_ms=int(fixture.harness.clock() * 1000) + 60_000,
        ),
        context=fixture.outer,
        now=fixture.harness.clock(),
    )
    with pytest.raises(AuthorizationError):
        fixture.broker.invoke(
            InvocationFrame(
                contract_id=TASK_CONTRACT,
                version_range=None,
                operation_id=EXECUTE,
                payload=payload,
            ),
            fixture.inner,
            effect_scope=fixture.harness.scope.to_dict(),
        )
    assert fixture.backend.invocations == 0


def test_workspace_task_revoked_approval_grant_cannot_dispatch(
    host_workspace_task: SimpleNamespace,
) -> None:
    fixture = host_workspace_task
    pending = _prepare(fixture)
    _decide(fixture, pending.approval_request_id, "approve")
    decision = fixture.harness.store.get_interactive_approval_decision(
        pending.approval_request_id
    )
    assert decision is not None and decision.grant_id is not None
    fixture.adapter.revoke(
        target_kind="grant", target_id=decision.grant_id, reason="test revocation"
    )
    assert (
        fixture.service.resume_interactive_effect(
            _owner(fixture, pending.effect_id)
        ).state
        == "stale"
    )
    assert fixture.backend.invocations == 0


@pytest.mark.parametrize(
    "patch",
    [
        {"profile_id": "profile.foreign"},
        {"plan_digest": canonical_digest({"plan": "foreign"})},
        {"security_epoch": 2},
    ],
)
def test_workspace_task_inner_execute_context_cannot_change_capture(
    host_workspace_task: SimpleNamespace,
    patch: dict[str, Any],
) -> None:
    fixture = host_workspace_task
    fixture.service._context_for_execute = lambda _route, _outer: replace(
        fixture.inner,
        **patch,
    )
    with pytest.raises(InteractiveEffectUnavailable):
        _prepare(fixture)
    assert fixture.backend.invocations == 0
    assert fixture.harness.store.list_host_pending_effects() == []


def test_workspace_task_rejects_broker_normalization_that_changes_frozen_plan(
    host_workspace_task: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = host_workspace_task
    original_prepare = fixture.broker.prepare

    def altered_prepare(frame: InvocationFrame, context: RequestContext) -> Any:
        prepared = original_prepare(frame, context)
        payload = deepcopy(dict(frame.payload))
        payload["task_plan"]["argv"] = ["python", "different"]
        return replace(prepared, normalized_payload=payload)

    monkeypatch.setattr(fixture.broker, "prepare", altered_prepare)
    with pytest.raises(InteractiveEffectUnavailable):
        _prepare(fixture)
    assert fixture.backend.invocations == 0
    assert fixture.harness.store.list_host_pending_effects() == []


@pytest.mark.parametrize("action", ["get", "resume", "cancel"])
def test_workspace_task_foreign_presentation_owner_cannot_manage_effect(
    host_workspace_task: SimpleNamespace,
    action: str,
) -> None:
    fixture = host_workspace_task
    pending = _prepare(fixture)
    query = replace(
        _owner(fixture, pending.effect_id),
        presentation_owner_session_id="foreign-session",
    )
    with pytest.raises(InteractiveEffectUnavailable):
        getattr(fixture.service, f"{action}_interactive_effect")(query)
    assert fixture.backend.invocations == 0


def test_adoption_schema_preserves_each_finite_kind_and_management_phase() -> None:
    """The proposed canonical schema adds tasks without breaking current kinds."""

    adoption = json.loads(
        (
            Path(__file__).parents[1]
            / "docs"
            / "workspace-task-effect-adoption.v1.json"
        ).read_text()
    )
    schema = adoption["canonical_source_updates"][0]["input_schema"]
    Draft202012Validator.check_schema(schema)
    from ecosystem.defaultspack.defaultspack.http_dynamic_targets import (
        _captured_input_schema,
    )

    assert _captured_input_schema(schema)
    validator = Draft202012Validator(schema)
    correlation_id = "a7188ed0-45d5-4b1b-9abc-6de80c41d5a1"
    for kind in INTERACTIVE_EFFECT_SPECS:
        request = _request(_coordinator_context()) if kind == "workspace_task" else {}
        validator.validate(
            {
                "phase": "prepare",
                "effect_kind": kind,
                "request": request,
                "correlation_id": correlation_id,
            }
        )
        validator.validate(
            {"phase": "lookup", "effect_kind": kind, "correlation_id": correlation_id}
        )
    for phase in ("status", "resume", "cancel"):
        validator.validate({"phase": phase, "effect_id": "pending-effect-1"})
    for payload in (
        {"phase": "prepare", "effect_kind": "unknown", "request": {}},
        {"phase": "prepare", "effect_kind": "workspace_task", "request": {}},
        {
            "phase": "prepare",
            "effect_kind": "shell_execute",
            "request": {},
            "approved": True,
        },
        {"phase": "resume", "effect_id": "pending-effect-1", "request": {}},
    ):
        with pytest.raises(ValidationError):
            validator.validate(payload)
