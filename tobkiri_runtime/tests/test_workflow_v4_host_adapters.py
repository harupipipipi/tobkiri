"""Real-Broker fake-transport tests for Workflow v4 Host adapters.

These tests drive the production capture path —
``WorkflowHostProviderFactoryV4.capture`` and
``WorkflowStopHostProviderFactoryV4.capture`` — against the real
``AuthorityV4Adapter`` interactive-approval lifecycle and a real
``RequestBroker`` with fake transport.  There are no synthetic approvals,
no pretend dispatch tokens and no direct Provider callbacks: reservations
are real kernel approval requests, decisions require the signed v3
interactive ui_operator proof, and step dispatch enters the real Broker
through the invocation's contract client with nested cancellation tracking.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from core_runtime.authority.ui_operator import sign_ui_operator
from core_runtime.authority.v4_models import (
    DomainBoundary,
    FunctionPrincipal,
)
from core_runtime.host_contract import bind_host_contract
from core_runtime.host_provider_backend_v4 import HostInvocationEvidenceReferenceV4
from core_runtime.workflow_v4.evidence import EVIDENCE_KIND
from core_runtime.workflow_v4.host_adapters import HostAttemptAuthorityV4
from core_runtime.workflow_v4.integration import (
    WorkflowHostProviderFactoryV4,
    WorkflowStopHostProviderFactoryV4,
)
from core_runtime.workflow_v4.models import (
    WorkflowConflict,
    WorkflowDenied,
)
from core_runtime.workflow_v4.provider import (
    WORKFLOW_CONTRACT_ID,
    WORKFLOW_FUNCTION_PRINCIPAL,
    WORKFLOW_STOP_FUNCTION_PRINCIPAL,
)
from tests.conformance_support.host_contract import host_contract
from tests.test_tool_broker_host import tool_host as tool_host
from tests.test_authority_v4_lifecycle import _digest, _domain, _Harness
from tests.test_tobkiri_host_authority_v4_adapter import _context, _Principals
from tests.test_tobkiri_host_execution_integration import (
    digest,
    fixture_artifact,
    make_broker,
)
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.contracts import OperationRoute, ResolvedOperationBinding
from tobkiri_host.models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    ExecutionKind,
    FunctionArtifact,
    InvocationFrame,
    OpaqueAuthorityRef,
    PackageKind,
    PackArtifact,
    RequestContext,
)
from tobkiri_host.operation_cancellation import (
    OwnedCancellationHandles,
    nested_cancellation_proof_for,
)
from tobkiri_host.ports import (
    InteractiveApprovalDecisionCommand,
    OpaqueInvocationLease,
)
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.canonical import canonical_digest

_HOST_CONTRACT = host_contract(
    profile_id="profile-1",
    values={"panel_bootstrap_secret": "workflow-v4-test-secret-" + "x" * 32},
)

_SEND_CONTRACT_ID = "io.tobkiri.notification.v1"
_SEND_PRINCIPAL = "authority:notification-send"
_CANCELLATION_GROUP = "workflow-v4"
_PACK_ID = "tobkiri_workflow_pack"

_MAIN_OPS = (
    "definition.create",
    "definition.publish",
    "run.create",
    "run.get",
    "run.advance",
    "run.cancel",
    "run.step.execute",
    "run.step.resume",
)


def _workflow_artifact(
    function_id: str,
    contract_id: str,
    operations: tuple[str, ...],
    digest_seed: str,
) -> tuple[FunctionArtifact, ArtifactVariant]:
    """Build one exact Function inventory for captured provider bindings."""
    ops = tuple(
        ContractOperation(
            contract_id=contract_id,
            contract_version="4.0.0",
            revision_digest=digest("w"),
            operation_id=operation_id,
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            effect_class=EffectClass.PURE,
        )
        for operation_id in operations
    )
    function = FunctionArtifact(
        function_id=function_id,
        implementation_digest=digest(digest_seed),
        variant_id="host.main",
        operations=ops,
    )
    variant = ArtifactVariant(
        variant_id="host.main",
        digest=digest("wv"),
        execution_kind=ExecutionKind.HOST_EXTENSION,
        os="linux",
        architecture="x86_64",
        runtime_abi="host-v1",
        backend="host",
    )
    return function, variant


def _workflow_pack() -> PackArtifact:
    """One Pack artifact carrying both the main and stop Functions."""
    main_function, variant = _workflow_artifact(
        WORKFLOW_FUNCTION_PRINCIPAL, WORKFLOW_CONTRACT_ID, _MAIN_OPS, "wm"
    )
    stop_function, _ = _workflow_artifact(
        WORKFLOW_STOP_FUNCTION_PRINCIPAL,
        "tobkiri.workflow.stop.v4",
        ("run.stop",),
        "ws",
    )
    return PackArtifact(
        pack_id=_PACK_ID,
        version="4.0.0",
        digest=digest("wpack"),
        publisher_lineage="publisher.workflow",
        package_kind=PackageKind.NORMAL,
        functions=(main_function, stop_function),
        variants=(variant,),
    )


def _provider_binding(
    pack: PackArtifact,
    function: FunctionArtifact,
    operation_id: str,
    contract_id: str,
    *,
    principal: OpaqueAuthorityRef | None = None,
) -> ResolvedOperationBinding:
    """Bind one workflow operation to its own exact principal."""
    operation = next(
        item for item in function.operations if item.operation_id == operation_id
    )
    if principal is None:
        principal = OpaqueAuthorityRef(f"authority:workflow.{operation_id}")
    variant = next(
        item
        for item in pack.variants
        if item.variant_id == function.variant_id
    )
    route = OperationRoute(
        contract_id=contract_id,
        operation_id=operation_id,
        artifact_digest=pack.digest,
        function_id=function.function_id,
        variant_id=variant.variant_id,
        execution_domain_profile="host.workflow.v1",
        materialization_mode="on_demand",
        target_principal_ref=principal,
    )
    return ResolvedOperationBinding(
        artifact=pack,
        function=function,
        variant=variant,
        operation=operation,
        route=route,
        principal_ref=principal,
    )


def _catalog_binding(item: PackArtifact) -> ResolvedOperationBinding:
    """The send operation's exact resolved binding for the captured catalog."""
    function = item.functions[0]
    operation = function.operations[0]
    principal = OpaqueAuthorityRef(_SEND_PRINCIPAL)
    route = OperationRoute(
        contract_id=_SEND_CONTRACT_ID,
        operation_id="send",
        artifact_digest=item.digest,
        function_id=function.function_id,
        variant_id=item.variants[0].variant_id,
        execution_domain_profile="wasm.effect.v1",
        materialization_mode="on_demand",
        target_principal_ref=principal,
    )
    return ResolvedOperationBinding(
        artifact=item,
        function=function,
        variant=item.variants[0],
        operation=operation,
        route=route,
        principal_ref=principal,
    )


class _CaptureContext:
    """SimpleNamespace-shaped stand-in for HostProviderCaptureContextV4."""

    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def _make_capture_context(
    tmp_path: Path,
    *,
    harness: _Harness,
    pack: PackArtifact,
    provider_bindings: tuple[ResolvedOperationBinding, ...],
    catalog_bindings: tuple[ResolvedOperationBinding, ...],
    approvals: AuthorityV4Adapter | None,
) -> _CaptureContext:
    return _CaptureContext(
        profile_id="profile-1",
        plan_digest=_digest("plan"),
        security_epoch=1,
        activation={
            "activation_id": "activation-1",
            "activation_digest": _digest("activation"),
        },
        state_root=tmp_path / "state",
        provider_bindings=provider_bindings,
        catalog_bindings=catalog_bindings,
        domain_ids={
            (
                binding.operation.contract_id,
                binding.operation.operation_id,
                binding.principal_ref.value,
            ): f"domain-{binding.operation.operation_id}"
            for binding in provider_bindings
        },
        user_data_root=None,
        interactive_approval_port=approvals,
        authority_approval_window_port=None,
    )


def _workflow_op_principal(operation_id: str) -> FunctionPrincipal:
    """Per-operation principal of the captured Workflow Function.

    Production catalogs name each operation's exact principal as the
    authority digest of its Function identity; every operation of one
    Function shares artifact/implementation/function/revision fields and
    differs only by ``operation_id``.
    """
    pack = _workflow_pack()
    function = next(item for item in pack.functions if item.function_id == WORKFLOW_FUNCTION_PRINCIPAL)
    operation = next(item for item in function.operations if item.operation_id == operation_id)
    return FunctionPrincipal(pack.digest, function.implementation_digest, function.function_id, operation.revision_digest, operation_id)



def _foreign_op_principal(operation_id: str) -> FunctionPrincipal:
    """Per-operation principal of a different captured Function."""
    pack = _workflow_pack()
    function = next(item for item in pack.functions if item.function_id == WORKFLOW_STOP_FUNCTION_PRINCIPAL)
    operation = next(item for item in function.operations if item.operation_id == operation_id)
    return FunctionPrincipal(pack.digest, function.implementation_digest, function.function_id, operation.revision_digest, operation_id)



def _operation_context(
    harness: _Harness,
    *,
    request_id: str,
    domain_seed: str,
    target_function: FunctionPrincipal | None = None,
) -> RequestContext:
    """Context under a real, separately registered domain pair.

    ``_context`` shares one caller/target domain across operations, which
    hid the cross-operation approval-equality failure.  Production
    envelopes for ``run.advance`` and ``run.step.resume`` derive domains,
    sessions, namespaces and target principals from their own
    authenticated bindings, so this registers a real second caller domain
    (same caller principal, own session) and a real second target domain
    (hosting the envelope's exact target Function principal) — the
    kernel validates every one of these fields against the registered
    records, so fabricated ids would be rejected.
    """
    state = getattr(harness, "_operation_domains", None)
    if state is None:
        state = harness._operation_domains = {"callers": {}, "targets": {}}
    # Fencing is an epoch-wide fence: the kernel requires caller, target
    # and presentation domains to share the request token, so only the
    # identities/boot epochs/sessions differ, never fencing.
    caller_domain = state["callers"].get(domain_seed)
    if caller_domain is None:
        caller_domain = _domain(f"caller-{domain_seed}", harness.caller)
        harness.kernel.register_execution_domain(
            caller_domain,
            session_id=f"session-caller-{domain_seed}",
            channel_digest=caller_domain.authenticated_channel_digest,
            principal=harness.caller,
        )
        state["callers"][domain_seed] = caller_domain
    domain_principal = target_function or harness.target
    target_key = (domain_seed, domain_principal.principal_id)
    target_domain = state["targets"].get(target_key)
    if target_domain is None:
        suffix = f"{domain_seed}-{len(state['targets'])}"
        target_domain = _domain(
            f"target-{suffix}",
            domain_principal,
            boundary=DomainBoundary.DEDICATED_PROCESS,
        )
        harness.kernel.register_execution_domain(
            target_domain,
            session_id=f"session-target-{suffix}",
            channel_digest=target_domain.authenticated_channel_digest,
            principal=domain_principal,
        )
        state["targets"][target_key] = target_domain
    context = _context(harness, request_id=request_id)
    return replace(
        context,
        caller_session_id=f"session-caller-{domain_seed}",
        caller_domain_id=caller_domain.domain_id,
        caller_boot_epoch=caller_domain.boot_epoch,
        target_domain_id=target_domain.domain_id,
        target_boot_epoch=target_domain.boot_epoch,
        target_backend_digest=_digest(f"backend-{domain_seed}"),
        fencing_token=target_domain.fencing_token,
        handle_namespace=caller_domain.resource_namespace,
    )


def _envelope(
    harness: _Harness,
    *,
    operation_id: str,
    request_seed: str,
    domain_seed: str | None = None,
    context_overrides: Mapping[str, Any] | None = None,
    target_function: FunctionPrincipal | None = None,
) -> RequestEnvelope:
    """One real Broker envelope for one captured workflow operation."""
    context = (
        _operation_context(
            harness,
            request_id=f"env-{request_seed}",
            domain_seed=domain_seed,
            target_function=target_function,
        )
        if domain_seed is not None
        else _context(harness, request_id=f"env-{request_seed}")
    )
    if context_overrides:
        context = replace(context, **dict(context_overrides))
    return RequestEnvelope(
        context=context,
        target_principal=OpaqueAuthorityRef(
            (target_function or harness.target).principal_id
        ),
        target_domain=OpaqueAuthorityRef(context.target_domain_id),
        contract_id=(
            "tobkiri.workflow.stop.v4"
            if operation_id == "run.stop"
            else WORKFLOW_CONTRACT_ID
        ),
        contract_version="4.0.0",
        operation_id=operation_id,
        payload={},
        request_digest=_digest(request_seed),
        deadline_monotonic=time.monotonic() + 60.0,
        lease=OpaqueInvocationLease(b"lease-" + request_seed.encode("utf-8")),
        idempotency_key=None,
    )


class _FakeClient:
    """Contract client entering the real Broker like GlobalContractClient."""

    def __init__(
        self,
        broker: Any,
        *,
        harness: _Harness,
        envelope: RequestEnvelope,
        owner_principal: str,
        owner_session: str,
    ) -> None:
        self._broker = broker
        self._harness = harness
        self._envelope = envelope
        self._owner_principal = owner_principal
        self._owner_session = owner_session
        self.calls: list[tuple[str, str, Mapping[str, Any]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        *,
        idempotency_key: str | None = None,
        timeout_ms: int | None = None,
    ) -> Mapping[str, Any]:
        """Enter the real Broker path with nested deadline/cancellation."""
        self.calls.append((contract_id, operation_id, dict(payload)))
        return self._broker.invoke(
            InvocationFrame(
                contract_id=contract_id,
                version_range=">=1,<2",
                operation_id=operation_id,
                payload=payload,
                timeout_ms=timeout_ms,
                idempotency_key=idempotency_key,
            ),
            replace_context(self._harness),
            effect_scope={
                "capability": "external_effect",
                "semantics_digest": _digest("e"),
                "dimensions": {},
                "quotas": {},
                "exact_request_digest": None,
                "opaque": False,
            },
            parent_deadline_monotonic=self._envelope.deadline_monotonic,
            parent_cancellation=self._envelope.cancellation_requested,
            parent_cancellation_proof=nested_cancellation_proof_for(
                self._envelope,
                self._owner_principal,
                self._owner_session,
            ),
        )


def replace_context(harness: _Harness) -> Any:
    """Fresh nested-dispatch context for the fake contract client."""
    return _context(
        harness,
        request_id=f"nested-{int(time.monotonic() * 1_000_000)}",
    )


class _FakeInvocation:
    """HostProviderInvocationContextV4 over the real cancellation registry."""

    def __init__(
        self,
        *,
        registry: OwnedCancellationHandles,
        envelope: RequestEnvelope,
        role: str,
        owner_principal: str,
        owner_session: str,
        client: _FakeClient,
    ) -> None:
        self._envelope = envelope
        self._client = client
        self.client_args: tuple[Any, ...] | None = None
        self.bounded_calls: list[dict[str, Any]] = []
        self.presentation_owner_principal_id = owner_principal
        self.presentation_owner_session_id = owner_session
        self._binding = registry.bind(
            group=(_PACK_ID, _CANCELLATION_GROUP),
            role=role,
            envelope=envelope,
            owner_principal=owner_principal,
            owner_session=owner_session,
            guard=self.assert_current,
        )

    @property
    def envelope(self) -> RequestEnvelope:
        return self._envelope

    @property
    def cancellation(self) -> Any:
        return self._binding

    def contract_client(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
        include_credentials: bool = True,
    ) -> Any:
        assert consumer_pack_id == _PACK_ID
        assert _SEND_CONTRACT_ID in allowed_contract_ids
        assert include_credentials is False
        self.client_args = (allowed_contract_ids, consumer_pack_id)
        return self._client

    def dispatch_bounded(
        self,
        *,
        allowed_contract_ids: frozenset[str],
        consumer_pack_id: str,
        contract_id: str,
        operation_id: str,
        payload: Mapping[str, Any],
        idempotency_key: str,
        timeout_ms: int | None,
        expected_payload_digest: str,
        evidence_ref: HostInvocationEvidenceReferenceV4 | None = None,
    ) -> Mapping[str, Any]:
        """Mirror the real Host bounded-dispatch: checks then real Broker."""
        if consumer_pack_id != _PACK_ID:
            raise PermissionError("consumer identity is invalid")
        if contract_id not in allowed_contract_ids:
            raise PermissionError("contract is not declared")
        if not idempotency_key:
            raise ValueError("dispatch idempotency key is invalid")
        if timeout_ms is not None and timeout_ms <= 0:
            raise ValueError("dispatch timeout bound is invalid")
        if canonical_digest(dict(payload)) != expected_payload_digest:
            raise ValueError(
                "dispatch payload does not match its pinned digest"
            )
        # This fixture proves Authority/Broker dispatch, not evidence transport.
        # Real receiver scoping is covered by test_workflow_evidence_host_receiver.
        if evidence_ref is not None:
            assert evidence_ref.kind == EVIDENCE_KIND
            assert evidence_ref.reference.startswith("sha256:") and len(evidence_ref.reference) == 71
        bounded = timeout_ms
        if bounded is not None:
            # The caller timeout may only tighten the envelope deadline.
            remaining_ms = int(
                (self._envelope.deadline_monotonic - time.monotonic()) * 1000
            )
            bounded = min(bounded, max(0, remaining_ms))
        self.bounded_calls.append(
            {
                "contract_id": contract_id,
                "operation_id": operation_id,
                "payload": dict(payload),
                "idempotency_key": idempotency_key,
                "timeout_ms": bounded,
            }
        )
        return self._client.invoke(
            contract_id,
            operation_id,
            payload,
            idempotency_key=idempotency_key,
            timeout_ms=bounded,
        )

    def assert_current(self) -> None:
        if self._envelope.cancellation_requested.is_set():
            raise PermissionError("invocation envelope is stale")


def _definition_document(*, max_attempts: int = 1) -> dict[str, Any]:
    """One-step send Definition bound to the captured catalog identity."""
    return {
        "workflow_api_version": "io.tobkiri.workflow.v4",
        "name": "Notification send",
        "steps": [
            {
                "id": "send_step",
                "request": {
                    "contract_id": _SEND_CONTRACT_ID,
                    "contract_revision_digest": digest("c"),
                    "operation_id": "send",
                    "function_principal_id": _SEND_PRINCIPAL,
                    "input": {"message": "${inputs.message}"},
                },
                "retry": {"max_attempts": max_attempts, "backoff_ms": 0},
            }
        ],
    }


def _approve(
    adapter: AuthorityV4Adapter,
    harness: _Harness,
    request_id: str,
) -> None:
    """Decide one pending approval with a real signed v3 ui_operator."""
    status = adapter.interactive_approval_status(request_id)
    with bind_host_contract(_HOST_CONTRACT):
        ui_operator = sign_ui_operator(
            request_id,
            decision="approve",
            request_snapshot_digest=status.request_snapshot_digest,
            typed_confirmation_digest=None,
        )
        adapter.approve_interactive_approval(
            InteractiveApprovalDecisionCommand(
                context=_context(harness, request_id=f"approve-{request_id}"),
                request_id=request_id,
                actor_id="user-1",
                confirmation_text="",
                ui_operator=ui_operator,
            )
        )


def _wired(
    tmp_path: Path,
    *,
    approvals: Any,
    digest_op_refs: bool = False,
) -> dict[str, Any]:
    """Capture both factories and return a per-operation invoke map."""
    pack = _workflow_pack()
    functions = {function.function_id: function for function in pack.functions}
    main_bindings = tuple(
        _provider_binding(
            pack,
            functions[WORKFLOW_FUNCTION_PRINCIPAL],
            op,
            WORKFLOW_CONTRACT_ID,
            principal=(
                OpaqueAuthorityRef(_workflow_op_principal(op).principal_id)
                if digest_op_refs
                else None
            ),
        )
        for op in _MAIN_OPS
    )
    stop_bindings = (
        _provider_binding(
            pack,
            functions[WORKFLOW_STOP_FUNCTION_PRINCIPAL],
            "run.stop",
            "tobkiri.workflow.stop.v4",
            principal=(
                OpaqueAuthorityRef(
                    _foreign_op_principal("run.stop").principal_id
                )
                if digest_op_refs
                else None
            ),
        ),
    )
    send_artifact = fixture_artifact()
    catalog_bindings = (_catalog_binding(send_artifact),)
    harness = _Harness(tmp_path)
    op_principals: tuple[FunctionPrincipal, ...] = (
        tuple(_workflow_op_principal(op) for op in _MAIN_OPS)
        + (_foreign_op_principal("run.stop"),)
        if digest_op_refs
        else ()
    )
    approvals_adapter = (
        AuthorityV4Adapter(
            harness.kernel,
            _Principals(
                harness.caller, harness.target, *op_principals
            ),
        )
        if approvals == "real"
        else None
    )
    main = WorkflowHostProviderFactoryV4().capture(
        _make_capture_context(
            tmp_path,
            harness=harness,
            pack=pack,
            provider_bindings=main_bindings,
            catalog_bindings=catalog_bindings,
            approvals=approvals_adapter,
        )
    )
    stop = WorkflowStopHostProviderFactoryV4().capture(
        _make_capture_context(
            tmp_path,
            harness=harness,
            pack=pack,
            provider_bindings=stop_bindings,
            catalog_bindings=catalog_bindings,
            approvals=approvals_adapter,
        )
    )
    return {
        "harness": harness,
        "approvals": approvals_adapter,
        "contributions": {
            contribution.operation_id: contribution.invoke
            for contribution in main.contributions + stop.contributions
        },
        "close": lambda: (main.close(), stop.close()),
    }


def _publish_and_create(
    contributions: Mapping[str, Any],
    invocation_for: Any,
    *,
    max_attempts: int = 1,
) -> str:
    """Create + publish the send Definition, then create a Run."""
    created = contributions["definition.create"](
        "definition.create",
        {
            "definition_id": "wf.notify",
            "document": _definition_document(max_attempts=max_attempts),
        },
        invocation_for("definition.create"),
    )
    contributions["definition.publish"](
        "definition.publish",
        {
            "definition_id": "wf.notify",
            "if_match": created["etag"],
        },
        invocation_for("definition.publish"),
    )
    run = contributions["run.create"](
        "run.create",
        {
            "definition_id": "wf.notify",
            "inputs": {"message": "hello"},
            "run_id": "run-1",
        },
        invocation_for("run.create"),
    )
    return run["run_id"]


def _invocation_factory(
    harness: _Harness,
    broker_fixture: Any,
) -> tuple[OwnedCancellationHandles, Any]:
    """Return (registry, invocation_for) producing real-bound invocations."""
    registry = OwnedCancellationHandles()

    def invocation_for(
        operation_id: str,
        *,
        role: str = "execute",
        seed: str | None = None,
        domain_seed: str | None = None,
        context_overrides: Mapping[str, Any] | None = None,
        owner_session: str | None = None,
        target_function: FunctionPrincipal | None = None,
    ) -> _FakeInvocation:
        envelope = _envelope(
            harness,
            operation_id=operation_id,
            request_seed=seed or f"{operation_id}-{time.monotonic_ns()}",
            domain_seed=domain_seed,
            context_overrides=context_overrides,
            target_function=target_function,
        )
        # The presentation owner session is the stable UI session — it
        # does not vary per operation even when the caller domain does.
        session = owner_session or "session-caller"
        client = _FakeClient(
            broker_fixture.broker,
            harness=harness,
            envelope=envelope,
            owner_principal=harness.caller.principal_id,
            owner_session=session,
        )
        return _FakeInvocation(
            registry=registry,
            envelope=envelope,
            role=role,
            owner_principal=harness.caller.principal_id,
            owner_session=session,
            client=client,
        )

    return registry, invocation_for


def test_step_dispatch_rides_real_broker_after_real_approval(
    tmp_path: Path,
) -> None:
    """run.step.execute -> WAITING_APPROVAL -> signed approve -> real Broker."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        assert attempt["state"] == "waiting_approval"
        reservation_id = attempt["authority_reservation_id"]
        assert fixture.events == []

        status = wired["approvals"].interactive_approval_status(
            reservation_id
        )
        assert status.state == "pending"
        assert status.redacted_metadata["step_id"] == "send_step"

        _approve(wired["approvals"], harness, reservation_id)

        attempt = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.resume"),
        )
        assert attempt["state"] == "succeeded"
        assert attempt["outcome"] == {"delivered": True}
        assert "provider_invoked" in fixture.events

        view = contributions["run.get"](
            "run.get", {"run_id": run_id}, invocation_for("run.get")
        )
        assert view["run"]["state"] == "succeeded"
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_pending_approval_never_dispatches(tmp_path: Path) -> None:
    """A pending reservation leaves the attempt waiting; no Broker traffic."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        assert attempt["state"] == "waiting_approval"

        again = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.resume"),
        )
        assert again["state"] == "waiting_approval"
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_denied_approval_fails_closed(tmp_path: Path) -> None:
    """A real deny decision fences the attempt; no Broker dispatch occurs."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        reservation_id = attempt["authority_reservation_id"]
        status = wired["approvals"].interactive_approval_status(
            reservation_id
        )
        with bind_host_contract(_HOST_CONTRACT):
            ui_operator = sign_ui_operator(
                reservation_id,
                decision="deny",
                request_snapshot_digest=status.request_snapshot_digest,
                typed_confirmation_digest=None,
            )
        with bind_host_contract(_HOST_CONTRACT):
            wired["approvals"].deny_interactive_approval(
                InteractiveApprovalDecisionCommand(
                    context=_context(
                        harness, request_id=f"deny-{reservation_id}"
                    ),
                    request_id=reservation_id,
                    actor_id="user-1",
                    confirmation_text="",
                    ui_operator=ui_operator,
                )
            )

        attempt = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.resume"),
        )
        assert attempt["state"] == "failed"
        assert attempt["error_code"] == "approval_denied"
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_run_cancel_denies_pending_approval_for_real(tmp_path: Path) -> None:
    """run.cancel revokes the pending approval with a signed deny."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        reservation_id = attempt["authority_reservation_id"]

        with bind_host_contract(_HOST_CONTRACT):
            run = contributions["run.cancel"](
                "run.cancel",
                {"run_id": run_id},
                invocation_for("run.cancel"),
            )
        assert run["state"] == "cancelled"
        status = wired["approvals"].interactive_approval_status(
            reservation_id
        )
        assert status.state == "denied"
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_unavailable_when_approval_port_absent(tmp_path: Path) -> None:
    """Without the real approval port, dispatch stays explicitly unavailable."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals=None)
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        with pytest.raises(WorkflowDenied, match="unavailable"):
            contributions["run.step.execute"](
                "run.step.execute",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for("run.step.execute"),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_stop_role_cancels_tracked_child_with_verified_drain(
    tmp_path: Path,
) -> None:
    """Stop-role binding signals the live child and requires drain proof.

    The child lifecycle mirrors exactly what the real Broker performs on a
    nested dispatch: reserve_child -> bind_child -> record_backend_cancellation
    -> Future completion -> record_resource_drain -> scope exit.
    """
    from concurrent.futures import Future
    from dataclasses import replace

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        assert attempt["state"] == "waiting_approval"
        request_reference = attempt["request"]["request_id"]

        execute_invocation = invocation_for("run.step.resume")
        stop_invocation = invocation_for("run.stop", role="stop")
        from core_runtime.workflow_v4.host_adapters import (
            HostStopAttemptInvokerV4,
        )

        stop_invoker = HostStopAttemptInvokerV4(invocation=stop_invocation)
        errors: list[BaseException] = []

        execute_binding = execute_invocation.cancellation
        with execute_binding.track(request_reference):
            proof = nested_cancellation_proof_for(
                execute_invocation.envelope,
                harness.caller.principal_id,
                "session-caller",
            )
            assert proof is not None
            child_envelope = replace(
                execute_invocation.envelope,
                context=_context(harness, request_id="nested-child"),
            )
            child_id = proof.reserve_child(child_envelope)
            child = Future()
            proof.bind_child(child_id, child)

            def _cancel() -> None:
                try:
                    stop_invoker.cancel(request_reference)
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            thread = threading.Thread(target=_cancel)
            thread.start()
            deadline = time.monotonic() + 5.0
            while (
                not execute_invocation.envelope.cancellation_requested.is_set()
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert execute_invocation.envelope.cancellation_requested.is_set()
            # The stop caller is still blocked: the child has not cancelled,
            # completed, and drained yet.
            assert thread.is_alive()
            proof.record_backend_cancellation(child_id, child)
            child.set_result(None)
            proof.record_resource_drain(child)
            # The drain still needs the tracked scope to exit.
            assert thread.is_alive()
        thread.join(timeout=10.0)
        assert not thread.is_alive()
        assert errors == []
        # The signal fired inside the tracked scope and verified drain was
        # proven before cancel() returned — no cancelled-before-proof claim.
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_stop_rejects_untracked_reference(tmp_path: Path) -> None:
    """run.stop cannot pretend to stop a reference nobody is tracking."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        stop_invocation = invocation_for("run.stop", role="stop")
        from core_runtime.workflow_v4.host_adapters import (
            HostStopAttemptInvokerV4,
        )

        stop_invoker = HostStopAttemptInvokerV4(invocation=stop_invocation)
        # An absent reference cannot prove that an old request drained.
        from core_runtime.workflow_v4.models import WorkflowCancellationUnconfirmed
        with pytest.raises(WorkflowCancellationUnconfirmed):
            stop_invoker.cancel("request-never-tracked")
        view = contributions["run.get"](
            "run.get", {"run_id": run_id}, invocation_for("run.get")
        )
        assert view["run"]["state"] == "queued"
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_real_dispatch_session_binds_key_timeout_and_payload_digest(
    tmp_path: Path,
) -> None:
    """Real V4DispatchSession carries the typed attempt fields to the Broker.

    This exercises the real production path (not the fake): the durable
    idempotency key reaches the RequestEnvelope, the caller timeout only
    ever tightens the parent deadline, and a mismatched pinned payload
    digest is denied before any dispatch can happen.
    """
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        seen: list[RequestEnvelope] = []
        original_invoke = fixture.backend.invoke

        def spy(request: RequestEnvelope) -> Any:
            seen.append(request)
            return original_invoke(request)

        fixture.backend.invoke = spy
        session = V4DispatchSession(
            broker=fixture.broker,
            context_for=(
                lambda contract_id, operation_id: replace_context(harness)
            ),
            effect_scope_for=(
                lambda contract_id, operation_id, arguments: {
                    "capability": "external_effect",
                    "semantics_digest": _digest("e"),
                    "dimensions": {},
                    "quotas": {},
                    "exact_request_digest": None,
                    "opaque": False,
                }
            ),
            providers={},
            profile_id="defaults",
            plan_digest="plan",
            profile_revision="revision",
            activation_id="activation",
            owned_authority_store=Mock(),
        )
        payload = {"message": "hello"}
        parent_deadline = time.monotonic() + 30.0
        result = session.invoke(
            _SEND_CONTRACT_ID,
            "send",
            payload,
            parent_deadline_monotonic=parent_deadline,
            parent_cancellation=threading.Event(),
            idempotency_key="wf-attempt-7",
            timeout_ms=600_000,
            expected_payload_digest=canonical_digest(payload),
        )
        assert result == {"delivered": True}
        assert [request.idempotency_key for request in seen] == [
            "wf-attempt-7"
        ]
        # The caller's 600s timeout was far beyond the operation's timeout
        # bound, so the dispatched deadline is the tighter of the two and
        # can never extend past the authenticated parent deadline.
        assert all(
            request.deadline_monotonic <= parent_deadline for request in seen
        )
        with pytest.raises(ValueError, match="pinned digest"):
            session.invoke(
                _SEND_CONTRACT_ID,
                "send",
                payload,
                parent_deadline_monotonic=time.monotonic() + 30.0,
                parent_cancellation=threading.Event(),
                idempotency_key="wf-attempt-8",
                timeout_ms=1_000,
                expected_payload_digest=canonical_digest({"message": "x"}),
            )
        assert len(seen) == 1
        assert fixture.events.count("provider_invoked") == 1
    finally:
        wired["close"]()
        fixture.broker.close()


def test_retry_dispatch_reuses_durable_idempotency_key_and_bounded_deadline(
    tmp_path: Path,
) -> None:
    """A retried attempt re-dispatches under the identical durable key.

    The first dispatch is denied inside the real Broker path (static
    authority check), the retry creates a new attempt, and both attempts
    present the identical durable step key to the real Broker — a
    duplicate/retry can never reach the backend under a different
    identity — while the dispatched deadline never exceeds the parent
    envelope deadline.
    """
    fixture = make_broker(fail_static=True)
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        seen: list[RequestEnvelope] = []
        original_invoke = fixture.backend.invoke

        def spy(request: RequestEnvelope) -> Any:
            seen.append(request)
            return original_invoke(request)

        fixture.backend.invoke = spy
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(
            contributions, invocation_for, max_attempts=2
        )

        resume_one = invocation_for("run.step.resume")
        attempt = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        assert attempt["state"] == "waiting_approval"
        _approve(
            wired["approvals"], harness, attempt["authority_reservation_id"]
        )
        attempt = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            resume_one,
        )
        assert attempt["state"] == "failed"
        assert attempt["error_code"] == "authority_denied"
        assert fixture.events.count("provider_invoked") == 0

        # Retry: the engine creates a fresh attempt but the durable step
        # idempotency key is identical, and dispatch still requires a real
        # approval lifecycle for the new attempt.
        retry = contributions["run.step.execute"](
            "run.step.execute",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.execute"),
        )
        assert retry["state"] == "waiting_approval"
        assert retry["attempt_number"] == 2
        _approve(
            wired["approvals"], harness, retry["authority_reservation_id"]
        )
        fixture.authority.fail_static = False
        resume_two = invocation_for("run.step.resume")
        attempt = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            resume_two,
        )
        assert attempt["state"] == "succeeded"
        assert fixture.events.count("provider_invoked") == 1

        keys = [request.idempotency_key for request in seen]
        assert len(keys) == 1
        # The timeout the invoker passed reached the real frame: each
        # envelope deadline stays at or below the operation bound and the
        # parent envelope deadline — dispatch can never extend either.
        assert all(
            request.deadline_monotonic
            <= resume.envelope.deadline_monotonic
            for request, resume in zip(seen, (resume_two,))
        )
        bounded = resume_one.bounded_calls + resume_two.bounded_calls
        # Both attempts presented the identical durable key; only the retry
        # reached the backend because the first was denied pre-dispatch.
        assert len(bounded) == 2
        assert all(call["idempotency_key"] == keys[0] for call in bounded)
        # A succeeded step can never be dispatched again.
        with pytest.raises(WorkflowConflict):
            contributions["run.step.execute"](
                "run.step.execute",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for("run.step.execute"),
            )
        assert fixture.events.count("provider_invoked") == 1
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_dispatch_payload_digest_mismatch_denied_before_broker(
    tmp_path: Path,
) -> None:
    """A pinned resolved input that no longer matches cannot dispatch."""
    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        invocation = invocation_for("run.step.execute")
        payload = {"message": "hello"}
        with pytest.raises(ValueError, match="pinned digest"):
            invocation.dispatch_bounded(
                allowed_contract_ids=frozenset({_SEND_CONTRACT_ID}),
                consumer_pack_id=_PACK_ID,
                contract_id=_SEND_CONTRACT_ID,
                operation_id="send",
                payload=payload,
                idempotency_key="wf-attempt-9",
                timeout_ms=1_000,
                expected_payload_digest=canonical_digest(
                    {"message": "tampered"}
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def _approval_attempt(
    contributions: Mapping[str, Any],
    invocation_for: Any,
    run_id: str,
    *,
    domain_seed: str,
    **invocation_kwargs: Any,
) -> dict[str, Any]:
    """Advance a run to its approval checkpoint under one operation domain.

    ``run.advance`` returns the bounded observe view, so the internal
    reservation id is read back through ``run.get`` exactly like the
    production resume path.
    """
    contributions["run.advance"](
        "run.advance",
        {"run_id": run_id},
        invocation_for(
            "run.advance", domain_seed=domain_seed, **invocation_kwargs
        ),
    )
    current = contributions["run.get"](
        "run.get",
        {"run_id": run_id},
        invocation_for("run.get", domain_seed=domain_seed),
    )
    attempt = current["attempts"][-1]
    assert attempt["step_id"] == "send_step"
    assert attempt["state"] == "waiting_approval"
    return attempt


def test_cross_operation_resume_uses_persisted_continuation(
    tmp_path: Path,
) -> None:
    """Approval under run.advance's domain resumes under run.step.resume's.

    Production envelopes derive domains, backend digests and namespaces per
    operation; the Host-persisted continuation snapshot carries the original
    request context while the resume envelope re-proves the captured
    Function identity, request digest, profile/epoch and presentation
    owner/session — the global Grant assertion stays full-equality.
    """

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        assert fixture.events == []

        snapshot = wired["approvals"].get_host_pending_effect(reservation_id)
        assert snapshot is not None
        _revision, payload = snapshot
        assert payload["kind"] == "workflow-v4-approval-continuation"
        assert payload["context"]["caller_domain_id"] == (
            "domain-caller-advance"
        )

        _approve(wired["approvals"], harness, reservation_id)

        resumed = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for("run.step.resume", domain_seed="resume"),
        )
        assert resumed["state"] == "succeeded"
        assert resumed["outcome"] == {"delivered": True}
        assert "provider_invoked" in fixture.events
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_foreign_function_caller(tmp_path: Path) -> None:
    """A resume envelope under a different Function principal is denied —
    a Stop Function can never continue an execute approval."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    context_overrides={
                        "caller_principal": OpaqueAuthorityRef(
                            WORKFLOW_STOP_FUNCTION_PRINCIPAL
                        )
                    },
                ),
            )
        assert fixture.events == []
        assert (
            wired["approvals"].interactive_approval_status(reservation_id).state
            == "approved"
        )
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_profile_mismatch(tmp_path: Path) -> None:
    """A resume envelope under a different Profile cannot continue."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    context_overrides={"profile_id": "profile-other"},
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_cross_operation_resume_with_distinct_target_refs(
    tmp_path: Path,
) -> None:
    """Same Function, different operation target refs must still resume.

    Production names each operation's exact target principal; the
    continuation binds the captured Function identity (artifact,
    implementation, Function id, contract revision) — ``operation_id``
    may differ.  Both refs are real digest-shaped principals registered
    in their own target domains and carried by the provider bindings.
    """

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real", digest_op_refs=True)
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        advance_principal = _workflow_op_principal("run.advance")
        attempt = _approval_attempt(
            contributions,
            invocation_for,
            run_id,
            domain_seed="advance",
            target_function=advance_principal,
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        resume_principal = _workflow_op_principal("run.step.resume")
        assert resume_principal.principal_id != advance_principal.principal_id
        resumed = contributions["run.step.resume"](
            "run.step.resume",
            {"run_id": run_id, "step_id": "send_step"},
            invocation_for(
                "run.step.resume",
                domain_seed="resume",
                target_function=resume_principal,
            ),
        )
        assert resumed["state"] == "succeeded"
        assert "provider_invoked" in fixture.events
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_foreign_target_function(tmp_path: Path) -> None:
    """A different target Function under the same caller/session is denied.

    The presentation caller and session stay identical; only the
    resume envelope's target ref changes to a foreign captured Function —
    the continuation must refuse before any kernel grant consumption.
    """

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real", digest_op_refs=True)
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        advance_principal = _workflow_op_principal("run.advance")
        attempt = _approval_attempt(
            contributions,
            invocation_for,
            run_id,
            domain_seed="advance",
            target_function=advance_principal,
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        foreign = _foreign_op_principal("run.stop")
        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    target_function=foreign,
                ),
            )
        assert fixture.events == []
        assert (
            wired["approvals"].interactive_approval_status(reservation_id).state
            == "approved"
        )
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_request_digest_mismatch(tmp_path: Path) -> None:
    """A commit under a forged request digest is denied before dispatch."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        authority = HostAttemptAuthorityV4(
            invocation=invocation_for("run.step.resume", domain_seed="resume"),
            approvals=wired["approvals"],
            approval_window=None,
            catalog_bindings=(_catalog_binding(fixture_artifact()),),
            caller_publisher_lineage="publisher.workflow",
        )
        with pytest.raises(WorkflowDenied):
            authority.commit(
                reservation_id,
                request_digest=digest("forged-request"),
                security_epoch=1,
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_supplied_security_epoch_mismatch(
    tmp_path: Path,
) -> None:
    """A supplied security_epoch other than the live envelope's is denied."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        status = wired["approvals"].interactive_approval_status(
            reservation_id
        )
        pinned_digest = status.base_scope["exact_request_digest"]
        authority = HostAttemptAuthorityV4(
            invocation=invocation_for("run.step.resume", domain_seed="resume"),
            approvals=wired["approvals"],
            approval_window=None,
            catalog_bindings=(_catalog_binding(fixture_artifact()),),
            caller_publisher_lineage="publisher.workflow",
        )
        with pytest.raises(WorkflowDenied):
            authority.commit(
                reservation_id,
                request_digest=pinned_digest,
                security_epoch=99,
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_context_epoch_mismatch(tmp_path: Path) -> None:
    """A resume envelope under a different security epoch is denied."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    context_overrides={"security_epoch": 2},
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


@pytest.mark.parametrize(
    "tamper_field", ["kind", "state", "context.request_id"]
)
def test_continuation_denies_tampered_snapshot_metadata(
    tmp_path: Path, tamper_field: str
) -> None:
    """A rewritten pending-effect snapshot can never carry a resume.

    ``kind``, ``state`` and the recorded ``request_id`` are part of the
    continuation identity: mutating any of them in the Host-only store
    must deny the resume instead of minting authority.
    """

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        record = wired["approvals"].get_host_pending_effect(reservation_id)
        assert record is not None
        revision, snapshot = record
        tampered = dict(snapshot)
        if tamper_field == "context.request_id":
            tampered["context"] = {
                **dict(snapshot["context"]),
                "request_id": "forged-request-id",
            }
        else:
            tampered[tamper_field] = "workflow-v4-forged"
        wired["approvals"].compare_and_swap_host_pending_effect(
            reservation_id, expected_revision=revision, payload=tampered
        )

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for("run.step.resume", domain_seed="resume"),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_fencing_token_mismatch(tmp_path: Path) -> None:
    """A resume envelope carrying a foreign fencing token is denied."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    context_overrides={"fencing_token": 99},
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_delegation_chain_mismatch(tmp_path: Path) -> None:
    """A resume envelope carrying a foreign delegation chain is denied."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    context_overrides={
                        "delegation_chain": (
                            OpaqueAuthorityRef("delegated-foreign"),
                        )
                    },
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()


def test_continuation_denies_session_mismatch(tmp_path: Path) -> None:
    """A resume under a different presentation session cannot continue."""

    fixture = make_broker()
    wired = _wired(tmp_path, approvals="real")
    try:
        harness = wired["harness"]
        registry, invocation_for = _invocation_factory(harness, fixture)
        contributions = wired["contributions"]
        run_id = _publish_and_create(contributions, invocation_for)

        attempt = _approval_attempt(
            contributions, invocation_for, run_id, domain_seed="advance"
        )
        reservation_id = attempt["authority_reservation_id"]
        _approve(wired["approvals"], harness, reservation_id)

        with pytest.raises(WorkflowDenied):
            contributions["run.step.resume"](
                "run.step.resume",
                {"run_id": run_id, "step_id": "send_step"},
                invocation_for(
                    "run.step.resume",
                    domain_seed="resume",
                    owner_session="session-other",
                ),
            )
        assert fixture.events == []
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()



def test_typed_tool_result_mismatch_after_one_effect_requires_reconciliation(tmp_path, tool_host):
    """Real approval/Broker/Workflow; fake transport hosts the actual tool owner."""
    from ecosystem.rumi_tool_registry_pack.runtime.registry import _definition
    from tests.test_tobkiri_host_execution_integration import FakeBackend
    from ecosystem.rumi_tool_broker_pack.runtime import broker as tool_broker

    invoke_tool, client, _, _ = tool_host
    client.definition = _definition({**client.definition,
        "result_schema": {"type": "string"}, "result_schema_format": "normalized-result.v1"})

    class InvalidToolResultBackend(FakeBackend):
        def invoke(self, request):
            super().invoke(request)
            # The actual tool Broker executes its selected fake executor once,
            # normalizes its object result, then rejects the promised string.
            invoke_tool()
            raise AssertionError("invalid typed result must not escape")

    fixture = make_broker(backend=InvalidToolResultBackend([]))
    wired = _wired(tmp_path, approvals="real")
    registry, invocation_for = _invocation_factory(wired["harness"], fixture)
    contributions = wired["contributions"]
    try:
        document = _definition_document(max_attempts=3)
        dependent = {**document["steps"][0], "id": "dependent", "depends_on": ["send_step"]}
        document["steps"].append(dependent)
        created = contributions["definition.create"]("definition.create", {
            "definition_id": "wf.typed-result", "document": document,
        }, invocation_for("definition.create"))
        contributions["definition.publish"]("definition.publish", {
            "definition_id": "wf.typed-result", "if_match": created["etag"],
        }, invocation_for("definition.publish"))
        contributions["run.create"]("run.create", {
            "definition_id": "wf.typed-result", "run_id": "typed-result-run", "inputs": {"message": "hello"},
        }, invocation_for("run.create"))
        args = {"run_id": "typed-result-run", "step_id": "send_step"}
        pending = contributions["run.step.execute"]("run.step.execute", args, invocation_for("run.step.execute"))
        _approve(wired["approvals"], wired["harness"], pending["authority_reservation_id"])
        result = contributions["run.step.resume"]("run.step.resume", args, invocation_for("run.step.resume"))
        assert result["state"] == "needs_reconciliation"
        assert not result.get("outcome")
        view = contributions["run.get"]("run.get", {"run_id": "typed-result-run"}, invocation_for("run.get"))
        assert view["run"]["state"] == "needs_reconciliation"
        assert len(view["attempts"]) == 1
        assert view["attempts"][0]["state"] == "ambiguous_effect"
        assert not view["attempts"][0].get("outcome")
        with pytest.raises((WorkflowConflict, WorkflowDenied)):
            contributions["run.step.execute"]("run.step.execute", args, invocation_for("run.step.execute"))
        with pytest.raises((WorkflowConflict, WorkflowDenied)):
            contributions["run.step.execute"]("run.step.execute", {**args, "step_id": "dependent"}, invocation_for("run.step.execute"))
        assert fixture.backend.invocations == 1
        assert sum(call[0] == tool_broker.EXECUTE for call in client.calls) == 1
    finally:
        registry.close()
        wired["close"]()
        fixture.broker.close()
