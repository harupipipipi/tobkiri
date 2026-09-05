"""Focused coverage for Host-only, side-effect-free Broker preparation."""

from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any, Mapping

import pytest

from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import (
    PreparedInvocation,
    PreparedInvocationSnapshot,
    RequestBroker,
    RequestEnvelope,
)
from tobkiri_host.contracts import (
    AdapterPlanner,
    OperationCatalog,
    OperationRoute,
    StructuralAdapter,
    schema_digest,
)
from tobkiri_host.effects import InMemoryReconciliationStore, ProviderOutcome
from tobkiri_host.errors import AuthorizationError, RequestTimedOutError, ResolutionError
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import InvocationFrame, OpaqueAuthorityRef, PackArtifact

from tests.test_tobkiri_host_execution_integration import (
    INPUT_SCHEMA,
    FakeAdmission,
    FakeAudit,
    FakeAuthority,
    FakeBackend,
    NoAdapters,
    context,
    fixture_artifact,
    frame,
    make_broker,
)


def _digest(value: object) -> str:
    """Return the canonical Broker digest used by the legacy invoke path."""

    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _expected_digest(
    prepared: PreparedInvocation,
    *,
    request_id: str,
    profile_revision: str,
    activation_digest: str,
    plan_digest: str,
) -> str:
    """Compute the digest formula used before the PreparedInvocation split."""

    return _digest(
        {
            "request_id": request_id,
            "profile_revision": profile_revision,
            "activation_digest": activation_digest,
            "plan_digest": plan_digest,
            "target": prepared.binding.principal_ref.value,
            "contract_id": prepared.binding.operation.contract_id,
            "contract_version": prepared.binding.operation.contract_version,
            "operation_id": prepared.binding.operation.operation_id,
            "payload": dict(prepared.normalized_payload),
            "idempotency_key": prepared.idempotency_key,
        }
    )


class _UnusedPort:
    """Fail loudly if side-effectful Broker dependencies are touched by prepare."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"prepare unexpectedly called {name}")


class _UppercaseAdapterExecutor:
    """Capability-free structural adapter used only by preparation coverage."""

    def __init__(self) -> None:
        self.calls = 0

    def execute(
        self,
        adapter: StructuralAdapter,
        payload: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        self.calls += 1
        assert adapter.adapter_id == "uppercase"
        return {"normalized": str(payload["message"]).upper()}


def _artifact(
    *,
    contract_id: str,
    operation_id: str,
    label: str,
    input_schema: Mapping[str, Any] = INPUT_SCHEMA,
) -> PackArtifact:
    """Build one independently routable test artifact from the core fixture."""

    base = fixture_artifact()
    operation = replace(
        base.functions[0].operations[0],
        contract_id=contract_id,
        operation_id=operation_id,
        input_schema=input_schema,
    )
    function = replace(
        base.functions[0],
        function_id=f"notification.{label}",
        operations=(operation,),
    )
    return replace(
        base,
        pack_id=f"notification.{label}",
        digest=_digest({"artifact": label}),
        functions=(function,),
    )


def _route(
    artifact: PackArtifact,
    *,
    adapter_ids: tuple[str, ...] = (),
) -> OperationRoute:
    """Return the exact route corresponding to one test artifact."""

    operation = artifact.functions[0].operations[0]
    return OperationRoute(
        contract_id=operation.contract_id,
        operation_id=operation.operation_id,
        artifact_digest=artifact.digest,
        function_id=artifact.functions[0].function_id,
        variant_id=artifact.variants[0].variant_id,
        execution_domain_profile="wasm.effect.v1",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(
            f"authority:{artifact.pack_id}-{operation.operation_id}"
        ),
        adapter_ids=adapter_ids,
    )


def _prepare_only_broker(
    catalog: OperationCatalog,
    *,
    adapters: AdapterPlanner | None = None,
    adapter_executor: object | None = None,
) -> RequestBroker:
    """Construct a Broker whose non-prepare dependencies must remain untouched."""

    return RequestBroker(
        catalog=catalog,
        adapters=adapters or AdapterPlanner(()),
        adapter_executor=adapter_executor or NoAdapters(),
        backends=BackendRegistry(()),
        materialization=MaterializationCoordinator(),
        admission=_UnusedPort(),
        authority=_UnusedPort(),
        audit=_UnusedPort(),
        reconciliation=InMemoryReconciliationStore(),
    )


def _live_broker(
    catalog: OperationCatalog,
    *,
    adapters: AdapterPlanner,
    adapter_executor: object,
) -> tuple[RequestBroker, Any, Any, Any, Any, list[str]]:
    """Build a single-dispatch Broker for durable prepared invocation tests."""

    events: list[str] = []
    backend = FakeBackend(events)
    authority = FakeAuthority(events)
    audit = FakeAudit(events)
    admission = FakeAdmission(events)
    return (
        RequestBroker(
            catalog=catalog,
            adapters=adapters,
            adapter_executor=adapter_executor,
            backends=BackendRegistry((backend,)),
            materialization=MaterializationCoordinator(),
            admission=admission,
            authority=authority,
            audit=audit,
            reconciliation=InMemoryReconciliationStore(),
        ),
        backend,
        authority,
        audit,
        admission,
        events,
    )


def _frame(
    *,
    contract_id: str,
    operation_id: str,
    payload: Mapping[str, Any] | None = None,
    idempotency_key: str | None = "key-1",
) -> InvocationFrame:
    """Return a caller-controlled frame for one independently pinned route."""

    return InvocationFrame(
        contract_id=contract_id,
        version_range=">=1,<2",
        operation_id=operation_id,
        payload=payload or {"message": "hello"},
        idempotency_key=idempotency_key,
    )


def test_prepare_is_side_effect_free_and_immutable() -> None:
    """Prepare does not touch authority, admission, audit, or a backend."""

    fixture = make_broker()
    try:
        prepared = fixture.broker.prepare(frame(), context())
    finally:
        fixture.broker.close()

    assert prepared.timeout_ms == 100
    assert prepared.deadline_monotonic > 0
    assert dict(prepared.normalized_payload) == {"message": "hello"}
    with pytest.raises(TypeError):
        prepared.normalized_payload["message"] = "changed"  # type: ignore[index]
    assert fixture.events == []
    assert fixture.backend.starts == 0
    assert fixture.backend.invocations == 0


def test_invoke_consumes_prepare_with_the_unchanged_legacy_digest() -> None:
    """Normal execution receives precisely the old request-digest calculation."""

    fixture = make_broker()
    captured: list[RequestEnvelope] = []

    def invoke(envelope: RequestEnvelope) -> ProviderOutcome:
        captured.append(envelope)
        return ProviderOutcome({"delivered": True})

    fixture.backend.invoke = invoke  # type: ignore[method-assign]
    current_context = context()
    try:
        prepared = fixture.broker.prepare(frame(), current_context)
        assert fixture.broker.invoke(frame(), current_context, effect_scope={}) == {
            "delivered": True
        }
    finally:
        fixture.broker.close()

    expected = _expected_digest(
        prepared,
        request_id=current_context.request_id,
        profile_revision=current_context.profile_revision,
        activation_digest=current_context.activation_digest,
        plan_digest=current_context.plan_digest,
    )
    assert prepared.request_digest == expected
    assert captured[0].request_digest == expected


def test_prepare_digest_binds_request_context_route_payload_and_idempotency() -> None:
    """Every identity-relevant digest input changes the prepared request."""

    first = _artifact(
        contract_id="io.tobkiri.first.v1",
        operation_id="send",
        label="first",
    )
    second = _artifact(
        contract_id="io.tobkiri.second.v1",
        operation_id="deliver",
        label="second",
    )
    broker = _prepare_only_broker(
        OperationCatalog((first, second), (_route(first), _route(second)))
    )
    try:
        initial_context = context()
        baseline = broker.prepare(
            _frame(contract_id="io.tobkiri.first.v1", operation_id="send"),
            initial_context,
        )
        changed_request = broker.prepare(
            _frame(contract_id="io.tobkiri.first.v1", operation_id="send"),
            replace(initial_context, request_id="request-2"),
        )
        changed_route = broker.prepare(
            _frame(contract_id="io.tobkiri.second.v1", operation_id="deliver"),
            initial_context,
        )
        changed_payload = broker.prepare(
            _frame(
                contract_id="io.tobkiri.first.v1",
                operation_id="send",
                payload={"message": "changed"},
            ),
            initial_context,
        )
        changed_key = broker.prepare(
            _frame(
                contract_id="io.tobkiri.first.v1",
                operation_id="send",
                idempotency_key="key-2",
            ),
            initial_context,
        )
    finally:
        broker.close()

    assert (
        len(
            {
                baseline.request_digest,
                changed_request.request_digest,
                changed_route.request_digest,
                changed_payload.request_digest,
                changed_key.request_digest,
            }
        )
        == 5
    )


def test_prepare_binds_adapter_normalization_before_request_digest() -> None:
    """The digest covers adapter output, never only the caller's preimage."""

    target_schema = {
        "type": "object",
        "properties": {"normalized": {"type": "string"}},
        "required": ["normalized"],
        "additionalProperties": False,
    }
    adapter = StructuralAdapter(
        adapter_id="uppercase",
        artifact_digest=_digest({"adapter": "uppercase"}),
        source_schema_digest=schema_digest(INPUT_SCHEMA),
        target_schema_digest=schema_digest(target_schema),
        source_schema=INPUT_SCHEMA,
        target_schema=target_schema,
    )
    artifact = _artifact(
        contract_id="io.tobkiri.adapter.v1",
        operation_id="normalize",
        label="adapter",
    )
    executor = _UppercaseAdapterExecutor()
    broker = _prepare_only_broker(
        OperationCatalog((artifact,), (_route(artifact, adapter_ids=("uppercase",)),)),
        adapters=AdapterPlanner((adapter,)),
        adapter_executor=executor,
    )
    try:
        prepared = broker.prepare(
            _frame(
                contract_id="io.tobkiri.adapter.v1",
                operation_id="normalize",
            ),
            context(),
        )
    finally:
        broker.close()

    assert executor.calls == 1
    assert dict(prepared.normalized_payload) == {"normalized": "HELLO"}
    assert prepared.request_digest == _expected_digest(
        prepared,
        request_id="request-1",
        profile_revision="",
        activation_digest=context().activation_digest,
        plan_digest=context().plan_digest,
    )


def test_invalid_or_identity_shaped_payload_never_reaches_authority() -> None:
    """Caller payload cannot supply context identity or bypass input validation."""

    fixture = make_broker()
    attacker_payload = {
        "message": "hello",
        "request_id": "attacker-request",
        "caller_principal": "attacker-principal",
        "target_principal": "attacker-target",
    }
    attacker = replace(frame(), payload=attacker_payload)
    try:
        with pytest.raises(ResolutionError, match="input schema"):
            fixture.broker.prepare(attacker, context())
        with pytest.raises(ResolutionError, match="input schema"):
            fixture.broker.invoke(attacker, context(), effect_scope={})
    finally:
        fixture.broker.close()

    assert fixture.events == []


def test_payload_identity_fields_remain_data_not_host_request_identity() -> None:
    """Accepted payload fields cannot replace the Host context or route target."""

    identity_schema = {
        "type": "object",
        "properties": {
            "message": {"type": "string"},
            "request_id": {"type": "string"},
            "caller_principal": {"type": "string"},
            "target_principal": {"type": "string"},
        },
        "required": ["message"],
        "additionalProperties": False,
    }
    artifact = _artifact(
        contract_id="io.tobkiri.identity.v1",
        operation_id="prepare",
        label="identity",
        input_schema=identity_schema,
    )
    broker = _prepare_only_broker(OperationCatalog((artifact,), (_route(artifact),)))
    trusted_context = context()
    try:
        prepared = broker.prepare(
            _frame(
                contract_id="io.tobkiri.identity.v1",
                operation_id="prepare",
                payload={
                    "message": "hello",
                    "request_id": "attacker-request",
                    "caller_principal": "attacker-caller",
                    "target_principal": "attacker-target",
                },
            ),
            trusted_context,
        )
    finally:
        broker.close()

    assert prepared.binding.principal_ref.value != "attacker-target"
    assert prepared.request_digest == _expected_digest(
        prepared,
        request_id=trusted_context.request_id,
        profile_revision=trusted_context.profile_revision,
        activation_digest=trusted_context.activation_digest,
        plan_digest=trusted_context.plan_digest,
    )
    assert prepared.request_digest != _expected_digest(
        prepared,
        request_id="attacker-request",
        profile_revision=trusted_context.profile_revision,
        activation_digest=trusted_context.activation_digest,
        plan_digest=trusted_context.plan_digest,
    )


def test_prepared_snapshot_round_trips_without_reapplying_adapter() -> None:
    """A restart-style snapshot resumes through exactly one provider dispatch."""

    adapter = StructuralAdapter(
        adapter_id="uppercase-message",
        artifact_digest=_digest({"adapter": "uppercase-message"}),
        source_schema_digest=schema_digest(INPUT_SCHEMA),
        target_schema_digest=schema_digest(INPUT_SCHEMA),
        source_schema=INPUT_SCHEMA,
        target_schema=INPUT_SCHEMA,
    )

    class UppercaseMessageExecutor(_UppercaseAdapterExecutor):
        """Normalize the same input schema while recording one adapter call."""

        def execute(
            self,
            adapter: StructuralAdapter,
            payload: Mapping[str, Any],
        ) -> Mapping[str, Any]:
            self.calls += 1
            assert adapter.adapter_id == "uppercase-message"
            return {"message": str(payload["message"]).upper()}

    artifact = _artifact(
        contract_id="io.tobkiri.pending.v1",
        operation_id="deliver",
        label="pending",
    )
    executor = UppercaseMessageExecutor()
    broker, backend, _authority, _audit, _admission, _events = _live_broker(
        OperationCatalog(
            (artifact,),
            (_route(artifact, adapter_ids=("uppercase-message",)),),
        ),
        adapters=AdapterPlanner((adapter,)),
        adapter_executor=executor,
    )
    current_context = context()
    dispatch_marks: list[int] = []

    def before_dispatch() -> None:
        dispatch_marks.append(backend.invocations)

    try:
        prepared = broker.prepare(
            _frame(
                contract_id="io.tobkiri.pending.v1",
                operation_id="deliver",
            ),
            current_context,
        )
        restored = PreparedInvocationSnapshot.from_dict(
            json.loads(json.dumps(prepared.to_snapshot().to_dict()))
        )
        result = broker.invoke_prepared(
            restored,
            current_context,
            {},
            execute_not_after_wall=1_000.0,
            wall_clock=lambda: 1.0,
            monotonic_clock=lambda: 10.0,
            before_dispatch=before_dispatch,
        )
    finally:
        broker.close()

    assert result == {"delivered": True}
    assert executor.calls == 1
    assert backend.invocations == 1
    assert dispatch_marks == [0]


def test_invoke_prepared_rejects_catalog_binding_drift_before_authority() -> None:
    """A changed resolved artifact cannot consume a durable prepared effect."""

    original = _artifact(
        contract_id="io.tobkiri.binding.v1",
        operation_id="deliver",
        label="binding-original",
    )
    changed = _artifact(
        contract_id="io.tobkiri.binding.v1",
        operation_id="deliver",
        label="binding-changed",
    )
    broker = _prepare_only_broker(OperationCatalog((original,), (_route(original),)))
    current_context = context()
    try:
        snapshot = broker.prepare(
            _frame(
                contract_id="io.tobkiri.binding.v1",
                operation_id="deliver",
            ),
            current_context,
        ).to_snapshot()
        broker._catalog = OperationCatalog((changed,), (_route(changed),))
        with pytest.raises(AuthorizationError, match="binding changed"):
            broker.invoke_prepared(
                snapshot,
                current_context,
                {},
                execute_not_after_wall=1_000.0,
                wall_clock=lambda: 1.0,
                monotonic_clock=lambda: 10.0,
            )
    finally:
        broker.close()


def test_invoke_prepared_rejects_payload_context_digest_and_expiry_drift() -> None:
    """Durable input changes fail before the common execution pipeline begins."""

    fixture = make_broker()
    current_context = context()
    try:
        snapshot = fixture.broker.prepare(frame(), current_context).to_snapshot()
        payload_document = snapshot.to_dict()
        payload_document["normalized_payload"]["message"] = "tampered"
        payload_snapshot = PreparedInvocationSnapshot.from_dict(payload_document)
        with pytest.raises(AuthorizationError, match="request digest changed"):
            fixture.broker.invoke_prepared(
                payload_snapshot,
                current_context,
                {},
                execute_not_after_wall=1_000.0,
                wall_clock=lambda: 1.0,
                monotonic_clock=lambda: 10.0,
            )

        digest_document = snapshot.to_dict()
        digest_document["request_digest"] = "sha256:" + "0" * 64
        digest_snapshot = PreparedInvocationSnapshot.from_dict(digest_document)
        with pytest.raises(AuthorizationError, match="request digest changed"):
            fixture.broker.invoke_prepared(
                digest_snapshot,
                current_context,
                {},
                execute_not_after_wall=1_000.0,
                wall_clock=lambda: 1.0,
                monotonic_clock=lambda: 10.0,
            )

        with pytest.raises(AuthorizationError, match="context changed"):
            fixture.broker.invoke_prepared(
                snapshot,
                replace(current_context, plan_digest=_digest({"plan": "changed"})),
                {},
                execute_not_after_wall=1_000.0,
                wall_clock=lambda: 1.0,
                monotonic_clock=lambda: 10.0,
            )

        with pytest.raises(RequestTimedOutError, match="expired"):
            fixture.broker.invoke_prepared(
                snapshot,
                current_context,
                {},
                execute_not_after_wall=1.0,
                wall_clock=lambda: 1.0,
                monotonic_clock=lambda: 10.0,
            )
    finally:
        fixture.broker.close()

    assert fixture.events == []
