"""Native selection on a finite operation.invoke target through actual Broker."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import hashlib

import pytest

from tobkiri_host.committed_selection_receipt import CommittedSelectionReceiptController
from tobkiri_host.route_bound_policy_selection import (
    CONTRACT,
    OPERATION,
    FUNCTION,
)
from tobkiri_host.selection_native_port import SelectionAwareNativeApprovalPort
from tobkiri_host.retained_policy_selection_port import RetainedPolicySelectionPort
from ecosystem.rumi_host_authority_bridge_pack.runtime import native_policy_bridge
from ecosystem.rumi_host_authority_bridge_pack.runtime.native_policy_bridge import (
    HOST_PROVIDER_FACTORY,
)
from core_runtime.host_provider_backend_v4 import ExactHostProviderBackendV4
from tobkiri_protocol.canonical import canonical_digest
from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope
from core_runtime.host_contract import bind_host_contract
from tests import test_authority_v4_lifecycle as lifecycle
from tests.test_authority_v4_lifecycle import _Harness, _principal
from tests.test_interactive_approval_v4 import (
    _CONTRACT,
    _adapter,
    _decision_command,
    _request_command,
)
from tests.test_tobkiri_host_authority_v4_adapter import (
    _artifact,
    _Admission,
    _NoAdapters,
    _context,
)
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.broker import RequestBroker
from tobkiri_host.contracts import OperationCatalog, OperationRoute, AdapterPlanner
from tobkiri_host.effects import InMemoryReconciliationStore
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import ExecutionKind, EffectClass, InvocationFrame, OpaqueAuthorityRef


@pytest.fixture(autouse=True)
def isolated_native_contract():
    with bind_host_contract(_CONTRACT):
        yield


def build_bounded_selection(
    tmp_path, monkeypatch, mode="full", *, configure=None, ancestor_chain=(), reviewer_capture=None
):
    tamper = None
    # Fixture pins exact new module bytes. No production trust or user proof is created.
    digest = (
        "sha256:" + hashlib.sha256(Path(native_policy_bridge.__file__).read_bytes()).hexdigest()
    )
    principal = replace(
        _principal("b", operation=OPERATION),
        function_id=FUNCTION,
        function_implementation_digest=digest,
    )
    original = lifecycle._principal
    monkeypatch.setattr(
        lifecycle,
        "_principal",
        lambda seed, **kw: principal if seed == "b" else original(seed, **kw),
    )
    ceiling = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=principal.contract_revision_digest,
        dimensions={"contract": (CONTRACT,), "operation": (OPERATION,)},
    )
    harness = _Harness(tmp_path, scope=ceiling)
    artifact = _artifact(harness)
    operation = replace(
        artifact.functions[0].operations[0],
        contract_id=CONTRACT,
        operation_id=OPERATION,
        effect_class=EffectClass.PRIVILEGED,
        idempotency="none",
        reconcile_operation=None,
    )
    artifact = replace(
        artifact,
        functions=(replace(artifact.functions[0], operations=(operation,)),),
        variants=(
            replace(
                artifact.variants[0],
                execution_kind=ExecutionKind.HOST_EXTENSION,
                backend="tobkiri.python-host-v4",
            ),
        ),
    )
    route = OperationRoute(
        contract_id=CONTRACT,
        operation_id=OPERATION,
        artifact_digest=artifact.digest,
        function_id=FUNCTION,
        variant_id="provider.variant",
        execution_domain_profile="dedicated.provider",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
    )
    extras = configure(harness, artifact, route) if configure else ((), (), ())
    extra_artifacts, extra_routes, extra_boundaries = extras
    catalog = OperationCatalog((artifact, *extra_artifacts), (route, *extra_routes))
    adapter = _adapter(harness)
    activation = {"activation_id": "activation-1"}
    activation_digest = canonical_digest(activation)
    original_resolver = harness.kernel._binding_resolver

    class Resolver:
        def resolve_authority_binding(self, **kwargs):
            return replace(
                original_resolver.resolve_authority_binding(**kwargs),
                activation_digest=activation_digest,
            )

    harness.kernel._binding_resolver = Resolver()

    class LatePort:
        target = None

        def select_prepared(self, selection_id, invocation):
            self.target.select_prepared(selection_id, invocation)

    port = LatePort()
    capture = SimpleNamespace(
        profile_id="profile-1",
        plan_digest=_context(harness).plan_digest,
        security_epoch=1,
        activation=activation,
        provider_bindings=(catalog.resolve_pinned(CONTRACT, OPERATION),),
        domain_ids={(CONTRACT, OPERATION, principal.principal_id): harness.target_domain.domain_id},
        action_approval_policy_port=port,
    )
    captured = HOST_PROVIDER_FACTORY.capture(capture)

    def invocation_context(envelope):
        if tamper == "target":
            envelope = replace(envelope, target_principal=OpaqueAuthorityRef("foreign-target"))
        elif tamper == "operation":
            envelope = replace(envelope, operation_id="foreign-operation")
        elif tamper == "payload":
            envelope = replace(envelope, payload={"selection_id": "foreign-selection"})
        elif tamper == "caller":
            envelope = replace(
                envelope,
                context=replace(
                    envelope.context, caller_principal=OpaqueAuthorityRef("foreign-caller")
                ),
            )

        def guard():
            if tamper == "stale":
                raise PermissionError("captured selection is stale")

        return SimpleNamespace(
            envelope=envelope,
            assert_current=guard,
            presentation_owner_principal_id=(
                "foreign-owner" if tamper == "owner" else harness.caller.principal_id
            ),
            presentation_owner_session_id="session-caller",
        )

    backend = ExactHostProviderBackendV4(
        captured.contributions,
        backend_id="tobkiri.python-host-v4",
        profile_id="profile-1",
        plan_digest=capture.plan_digest,
        security_epoch=1,
        invocation_context=invocation_context,
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
    context = replace(
        _context(harness, request_id="native-selection-broker"),
        activation_digest=activation_digest,
        target_backend_digest=backend.status.backend_digest,
    )
    frame = InvocationFrame(
        contract_id=CONTRACT,
        version_range="==1.0.0",
        operation_id=OPERATION,
        payload={"selection_id": "retained-selection-1"},
    )
    prepared = broker.prepare(frame, context)
    from tobkiri_host.policy_delegation_boundary import BoundedPolicySelectionController

    boundary_routes = (
        {
            "contract": CONTRACT,
            "operation": OPERATION,
            "caller_principal_id": harness.caller.principal_id,
            "operation_class": operation.effect_class.value,
            "ancestor_chain": list(ancestor_chain),
            "ceiling": ceiling.to_dict(),
        },
    )
    if extra_boundaries:
        boundary_routes = tuple(extra_boundaries)
    policy = BoundedPolicySelectionController.create(
        authority=adapter,
        authority_store=harness.store,
        catalog=catalog,
        prepared=prepared,
        clock=harness.clock,
        routes=boundary_routes,
    )
    kwargs = dict(
        mode=mode,
        reviewer=(
            dict(reviewer_capture)
            if reviewer_capture is not None
            else {"model": "isolated-reviewer"}
        )
        if mode == "agent"
        else {},
        workspace=str(tmp_path.resolve()),
        conversation="conversation-1",
        turn="turn-1",
    )
    command = policy.request(
        "selection-1", replace(_request_command(harness), context=context), **kwargs
    )
    assert command.request_digest == prepared.request_digest
    assert AuthorityScope.from_dict(command.base_scope).is_subset_of(ceiling)
    SelectionAwareNativeApprovalPort(policy).approve_interactive_approval(
        replace(
            _decision_command(
                harness, context.request_id, phrase=command.typed_confirmation_phrase
            ),
            context=context,
        )
    )
    retained = RetainedPolicySelectionPort(policy, command, guard=lambda: None)
    port.target = retained
    harness.kernel.revoke(
        target_kind="grant",
        target_id=harness.grant.grant_id,
        reason="fixture has no automatic selection commit grant",
    )
    if tamper:
        from tobkiri_host.errors import HostCoreError

        with pytest.raises((HostCoreError, AuthorityDenied, PermissionError, ValueError)):
            broker.invoke_prepared(
                prepared.to_snapshot(),
                context,
                command.base_scope,
                execute_not_after_wall=command.expires_at,
                wall_clock=harness.clock,
            )
        assert policy.store.read(retained.capture_key)[1].get("staged_lease_id") is None
        with pytest.raises(AuthorityDenied):
            retained.complete_after_broker()
        return
    broker.invoke_prepared(
        prepared.to_snapshot(),
        context,
        command.base_scope,
        execute_not_after_wall=command.expires_at,
        wall_clock=harness.clock,
    )
    lease_id = policy.store.read(retained.capture_key)[1]["staged_lease_id"]
    receipt = CommittedSelectionReceiptController(policy)
    retained.complete_after_broker()
    reopened = CommittedSelectionReceiptController(policy)
    assert reopened.resolve("selection-1", command, **kwargs) == mode
    decision = harness.store.get_interactive_approval_decision(context.request_id)
    assert harness.store.grant_usage(decision.grant_id) == (0, 1)
    with pytest.raises(AuthorityDenied):
        receipt.commit_after_execution("selection-1", command, lease_id)
    return SimpleNamespace(
        harness=harness,
        policy=policy,
        command=command,
        kwargs=kwargs,
        receipt=receipt,
        broker=broker,
        context=context,
        ceiling=ceiling,
        principal=principal,
        frame=frame,
        catalog=catalog,
    )
