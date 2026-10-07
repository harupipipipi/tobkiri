"""Real native selection, nested parent, policy adapter and finite tool Broker."""

import tobkiri_host.saved_tool_entry_guards as _host_saved_tool_entry_guards
import tobkiri_host.saved_tool_context as _host_saved_tool_context
from dataclasses import replace
import threading
import time
from types import SimpleNamespace
import pytest
from typing import Any
from core_runtime.invocation_scope_v4 import CapturedInvocationScopeV4
from tests import test_saved_tool_consent_broker as ask_fixture
from tobkiri_host import saved_tool_policy_execution as mode_module
from tests.approval_policy_fixture import build_bounded_selection
from tests.fixtures.host_action_reviewer_v4 import build_shared_store_reviewer
from tobkiri_host.policy_exact_authority_adapter import (
    PolicyExactAuthorityV4Adapter,
    CommittedPolicyDerivationRoot,
    rehydrate_policy_root,
)
from core_runtime.authority.policy_exact_grant import PolicyExactGrantKernel
from core_runtime.authority.v4 import AuthorityStore
from core_runtime.authority.v4 import (
    AuthorityScope,
    AuthorityMode,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
    LeaseState,
    AuthorityDenied,
)
from core_runtime.host_contract import bind_host_contract
from tests.test_interactive_approval_v4 import _CONTRACT
from tests.test_authority_v4_lifecycle import _principal, _domain, _Resolver, _digest
from tests.test_tobkiri_host_authority_v4_adapter import (
    _Principals,
    _Admission,
    _NoAdapters,
)
from tobkiri_host.models import OpaqueAuthorityRef, RuntimeEvidence
from tobkiri_host.contracts import OperationRoute, AdapterPlanner
from tobkiri_host.broker import RequestBroker, PreparedInvocationSnapshot
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.effects import InMemoryReconciliationStore, ProviderOutcome
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.errors import ProviderExecutionError


@pytest.mark.parametrize("tool_kind", ["read", "calculator"])
@pytest.mark.parametrize(
    "mode,outcome",
    [
        ("full", "approve"),
        ("full", "revoke"),
        ("full", "cancel"),
        ("full", "changed_args"),
        ("full", "restart"),
        ("agent", "unavailable"),
        ("agent", "approve"),
        ("agent", "review_error"),
        ("agent", "review_deny"),
    ],
)
def test_real_selected_policy_nested_tool(tmp_path, monkeypatch, tool_kind, mode, outcome):
    operation = (
        "rumi_default_tools_pack.files-read-operation"
        if tool_kind == "read"
        else "rumi_default_tools_pack.calculator-evaluate"
    )
    function = (
        "rumi_default_tools_pack.files-read"
        if tool_kind == "read"
        else "rumi_default_tools_pack.calculator"
    )
    read = replace(_principal("c", operation=operation), function_id=function)
    executor = replace(
        _principal("d", operation="rumi_tool_local_executor_pack.tool-local-execute"),
        function_id="rumi_tool_local_executor_pack.tool-executor.local",
    )
    read_domain, executor_domain = (
        _domain("read", read),
        _domain("executor", executor),
    )
    read_scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=read.contract_revision_digest,
        dimensions={
            "contract": (ask_fixture.READ_CONTRACT,),
            "operation": (operation,),
        },
        quotas={"operations": 1},
    )
    parent_scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=executor.contract_revision_digest,
        dimensions={
            "contract": ("tobkiri.service.tool.execute.v1",),
            "operation": (executor.operation_id,),
        },
    )
    read_artifact = ask_fixture.make_artifact(read, ask_fixture.READ_CONTRACT)
    executor_artifact = ask_fixture.make_artifact(executor, "tobkiri.service.tool.execute.v1")
    read_route = OperationRoute(
        contract_id=ask_fixture.READ_CONTRACT,
        operation_id=operation,
        artifact_digest=read_artifact.digest,
        function_id=function,
        variant_id="provider.variant",
        execution_domain_profile="dedicated.provider",
        materialization_mode="on_demand",
        target_principal_ref=OpaqueAuthorityRef(read.principal_id),
    )
    reviewer_capture = {}
    review_fixture = []

    def configure(harness, native_artifact, native_route):
        if mode == "agent" and outcome != "unavailable":
            captured = build_shared_store_reviewer(harness)
            reviewer_capture.update(captured.facts)
            review_fixture.append(captured)
            if outcome == "review_error":
                captured.transport.mode = "unavailable"
            if outcome == "review_deny":
                original_invoke = captured.transport.invoke

                def deny_invoke(contract, operation, payload, **kwargs):
                    value = original_invoke(contract, operation, payload, **kwargs)
                    if contract == "tobkiri.service.ai.provider.generate.v1":
                        import json

                        value["output"] = json.dumps(
                            {
                                "decision": "deny",
                                "reason": "Fixture dangerous operation denied.",
                            }
                        )
                    return value

                captured.transport.invoke = deny_invoke
        for principal, domain, scope, name in (
            (read, read_domain, read_scope, "read"),
            (executor, executor_domain, parent_scope, "executor"),
        ):
            harness.kernel.register_execution_domain(
                domain,
                session_id="session-" + name,
                channel_digest=domain.authenticated_channel_digest,
                principal=principal,
            )
            trust = HostExtensionTrustRecord(
                trust_id=name + "-trust",
                parent_artifact_digest=principal.parent_artifact_digest,
                publisher_lineage="publisher.target",
                provider_principal_ids=(principal.principal_id,),
                trust_provenance_digest=_digest(name + "-trust"),
                security_epoch=1,
                valid_from=harness.clock(),
            )
            provider = ProviderAuthorityRecord(
                record_id=name + "-provider",
                provider=principal,
                execution_domain_id=domain.domain_id,
                execution_domain_identity_digest=domain.identity_digest,
                scope=scope,
                authority_mode=AuthorityMode.LEASE_ONLY,
                security_epoch=1,
                trust_provenance_digest=trust.trust_provenance_digest,
                publisher_lineage="publisher.target",
                host_extension_id=trust.trust_id,
                valid_from=harness.clock(),
                host_broker_binding="broker." + name,
            )
            caller = executor if name == "read" else harness.caller
            approval = replace(
                harness.approval,
                approval_id=name + "-ordinary-approval",
                caller=caller,
                target=principal,
            )
            grant = replace(
                harness.grant,
                grant_id=name + "-ordinary-grant",
                caller=caller,
                target=principal,
                scope=scope,
                approval_id=approval.approval_id,
                delegation_allowed=True,
                max_delegation_depth=4,
            )
            harness.kernel.commit_approval_bundle(
                approval,
                host_extension_trust=trust,
                provider_authorities=(provider,),
                grants=(grant,),
            )
        original = harness.kernel._binding_resolver

        class Resolver:
            def resolve_authority_binding(self, **kwargs):
                scope = (
                    read_scope
                    if kwargs["target"] == read
                    else parent_scope
                    if kwargs["target"] == executor
                    else None
                )
                return (
                    _Resolver(scope).resolve_authority_binding(**kwargs)
                    if scope
                    else original.resolve_authority_binding(**kwargs)
                )

        harness.kernel._binding_resolver = Resolver()
        return (
            (read_artifact, executor_artifact),
            (read_route,),
            (
                {
                    "contract": ask_fixture.READ_CONTRACT,
                    "operation": operation,
                    "caller_principal_id": executor.principal_id,
                    "operation_class": read_artifact.functions[0].operations[0].effect_class.value,
                    "ancestor_chain": [harness.caller.principal_id],
                    "ceiling": read_scope.to_dict(),
                    "caller_publisher_lineage": executor_artifact.publisher_lineage,
                },
            ),
        )

    with bind_host_contract(_CONTRACT):
        fixture = build_bounded_selection(
            tmp_path,
            monkeypatch,
            mode,
            configure=configure,
            reviewer_capture=(
                reviewer_capture if mode == "agent" and outcome != "unavailable" else None
            ),
        )
        harness = fixture.harness
        harness.kernel = PolicyExactGrantKernel(
            harness.store,
            harness.kernel._binding_resolver,
            clock=harness.clock,
            lease_ttl_seconds=300,
        )
        harness.kernel.policy_roots = {}
        authority = PolicyExactAuthorityV4Adapter(
            harness.kernel, _Principals(harness.caller, harness.target, executor, read)
        )
        parent_context = harness.context(
            request_id="actual-executor-parent",
            request_digest=_digest("parent-request"),
            effect_digest=parent_scope.digest,
            target=executor,
            target_domain_id=executor_domain.domain_id,
            activation_digest=fixture.context.activation_digest,
        )
        authorized = harness.kernel.authorize(parent_context, parent_scope)
        parent = harness.kernel.dispatch(
            authorized.lease_token,
            target_domain_id=executor_domain.domain_id,
            target_boot_epoch=executor_domain.boot_epoch,
            request_digest=parent_context.request_digest,
        )
        parent_lease = OpaqueInvocationLease(authorized.lease_token.encode("ascii"))
        payload = {
            "tool_id": "coding_file_read" if tool_kind == "read" else "calculator",
            "tool_call_id": "call-1",
            "arguments": (
                {"path": "fixture.txt"} if tool_kind == "read" else {"expression": "2+3"}
            ),
        }
        file = tmp_path / "fixture.txt"
        file.write_text("finite policy fixture")
        effects = []
        registry = _host_saved_tool_entry_guards.SavedToolEntryGuardRegistry()

        class Backend(ask_fixture.Backend):
            def materialize(self, binding, reservation_id):
                return RuntimeEvidence(
                    domain_ref=OpaqueAuthorityRef(read_domain.domain_id),
                    executable_digest=read.function_implementation_digest,
                    backend_digest=self.status.backend_digest,
                    authenticated_channel=True,
                    nonce_fresh=True,
                )

            def invoke(self, envelope):
                if outcome == "revoke":
                    selected = harness.store.get_interactive_approval_decision(
                        fixture.command.context.request_id
                    )
                    harness.kernel.revoke(
                        target_kind="grant",
                        target_id=selected.grant_id,
                        reason="fixture revoke selected root",
                    )
                    registry.capture(envelope)()
                effects.append(envelope.operation_id)
                from ecosystem.rumi_default_tools_pack.domain.tool.calculator import (
                    calculate,
                )

                return ProviderOutcome(
                    {
                        "result": (
                            file.read_text()
                            if tool_kind == "read"
                            else calculate(envelope.payload["arguments"]["expression"])
                        )
                    }
                )

        backend = Backend(harness, harness.target, harness.target_domain, file, "approve", payload)
        broker = RequestBroker(
            catalog=fixture.catalog,
            adapters=AdapterPlanner(()),
            adapter_executor=_NoAdapters(),
            backends=BackendRegistry((backend,)),
            materialization=MaterializationCoordinator(),
            admission=_Admission(),
            authority=authority,
            audit=authority,
            reconciliation=InMemoryReconciliationStore(),
        )
        root = fixture.context
        saved = SimpleNamespace(
            envelope=SimpleNamespace(
                contract_id="conversation.saved-turn.v1",
                operation_id="saved_complete",
                context=root,
                payload={"request": {"turn_id": "turn-1", "action_approval_mode": mode}},
            ),
            parent=None,
            assert_current=lambda: None,
        )
        tool_broker = SimpleNamespace(
            envelope=SimpleNamespace(
                contract_id="tobkiri.service.tool.invoke.v1",
                operation_id="rumi_tool_broker_pack.tool-invoke",
                context=root,
                payload={},
            ),
            parent=saved,
            assert_current=lambda: None,
        )

        def live():
            current, state = harness.store.inspect_lease_token(authorized.lease_token)
            if state is not LeaseState.DISPATCHED or current != parent:
                raise AuthorityDenied("actual executor parent changed")

        invocation = SimpleNamespace(
            envelope=SimpleNamespace(
                contract_id="tobkiri.service.tool.execute.v1",
                operation_id=executor.operation_id,
                context=root,
                target_principal=OpaqueAuthorityRef(executor.principal_id),
                cancellation_requested=threading.Event(),
                deadline_monotonic=time.monotonic() + 45,
                lease=parent_lease,
            ),
            parent_invocation=tool_broker,
            presentation_owner_principal_id=root.caller_principal.value,
            presentation_owner_session_id=root.caller_session_id,
            assert_current=live,
        )

        def validate_prepared(retained):
            context = replace(
                root,
                request_id=retained.context.request_id,
                caller_principal=OpaqueAuthorityRef(executor.principal_id),
                caller_session_id="session-executor",
                caller_domain_id=executor_domain.domain_id,
                target_domain_id=read_domain.domain_id,
                target_backend_digest=_digest("backend"),
                delegation_chain=(OpaqueAuthorityRef(harness.caller.principal_id),),
            )
            checked = broker.validate_prepared_snapshot(
                PreparedInvocationSnapshot.from_dict(retained.prepared_snapshot),
                context,
            )
            if checked.binding.principal_ref.value != read.principal_id:
                raise AuthorityDenied("retained target changed")

        def saved_root_guard(actual, authenticated, capture):
            live()
            if (
                actual is not invocation
                or authenticated != root
                or capture["turn"] != "turn-1"
                or (capture["workspace"] != str(tmp_path.resolve()))
            ):
                raise AuthorityDenied("saved native root changed")

        policy_root = CommittedPolicyDerivationRoot(
            policy=fixture.policy,
            command=fixture.command,
            expected=fixture.kwargs,
            prepared_validator=validate_prepared,
            current_guard=lambda operation: live(),
            saved_root_guard=saved_root_guard,
            reviewer=review_fixture[0].reviewer if review_fixture else None,
            review_transport_verifier=(review_fixture[0].verifier if review_fixture else None),
        )
        harness.kernel.policy_roots[policy_root.selection_id] = policy_root
        releases = []
        nested_ids = [0]
        retained_scopes: dict[str, CapturedInvocationScopeV4] = {}

        def bind_nested(inv, contract, op, arguments):
            nested_ids[0] += 1
            scope = CapturedInvocationScopeV4(
                inv.envelope, inv.assert_current, inv.parent_invocation
            )
            retained_scopes["retained-read-" + str(nested_ids[0])] = scope
            return _host_saved_tool_context.NestedToolBinding(
                replace(
                    root,
                    request_id="retained-read-" + str(nested_ids[0]),
                    caller_principal=OpaqueAuthorityRef(executor.principal_id),
                    caller_session_id="session-executor",
                    caller_domain_id=executor_domain.domain_id,
                    target_domain_id=read_domain.domain_id,
                    target_backend_digest=_digest("backend"),
                    delegation_chain=(OpaqueAuthorityRef(harness.caller.principal_id),),
                ),
                read_scope.to_dict(),
                "publisher.caller",
                None,
                lambda: releases.append(op),
                inline_parent_scope=scope,
            )

        original_invoke_prepared = broker.invoke_prepared

        def observed_prepared(*args: Any, **kwargs: Any) -> Any:
            """Retain the exact modeled scope while original policy dispatch runs."""
            request_context = args[1]
            assert kwargs["inline_parent_scope"] is retained_scopes[
                request_context.request_id
            ]
            return original_invoke_prepared(*args, **kwargs)

        monkeypatch.setattr(broker, "invoke_prepared", observed_prepared)

        if outcome == "cancel":
            invocation.envelope.cancellation_requested.set()
        if outcome == "changed_args":
            original_settle = authority.settle_policy_derived_operation

            def changed_settle(**kwargs):
                result = original_settle(**kwargs)
                payload["arguments"] = (
                    {"expression": "99"} if tool_kind == "calculator" else {"path": "changed.txt"}
                )
                return result

            authority.settle_policy_derived_operation = changed_settle
        gate = mode_module.SavedToolApprovalExecution(
            ask=None,
            broker=broker,
            authority=authority,
            bind_nested=bind_nested,
            entry_guard_registry=registry,
            policy_selection=policy_root,
        )
        execution = {
            "contract_id": ask_fixture.READ_CONTRACT,
            "operation": operation,
            "provider_instance_id": function,
        }
        if outcome in {"approve", "restart"}:
            assert gate(invocation, execution, payload) == {
                "result": "finite policy fixture" if tool_kind == "read" else 5
            }
            assert effects == [operation]
            assert harness.store.get_grant("read-ordinary-grant") is not None
            reopened = mode_module.SavedToolApprovalExecution(
                ask=None,
                broker=broker,
                authority=authority,
                bind_nested=bind_nested,
                entry_guard_registry=registry,
                policy_selection=policy_root,
            )
            with pytest.raises(PermissionError, match="already claimed"):
                reopened(invocation, execution, payload)
            if outcome == "restart":
                resolver = harness.kernel._binding_resolver
                harness.store = AuthorityStore(tmp_path / "authority.sqlite3", clock=harness.clock)
                harness.kernel = PolicyExactGrantKernel(
                    harness.store, resolver, clock=harness.clock, lease_ttl_seconds=300
                )
                harness.kernel.policy_roots = {}
                authority = PolicyExactAuthorityV4Adapter(
                    harness.kernel,
                    _Principals(harness.caller, harness.target, executor, read),
                )
                broker = RequestBroker(
                    catalog=fixture.catalog,
                    adapters=AdapterPlanner(()),
                    adapter_executor=_NoAdapters(),
                    backends=BackendRegistry((backend,)),
                    materialization=MaterializationCoordinator(),
                    admission=_Admission(),
                    authority=authority,
                    audit=authority,
                    reconciliation=InMemoryReconciliationStore(),
                )
                restored = rehydrate_policy_root(
                    selection_id=policy_root.selection_id,
                    authority=authority,
                    authority_store=harness.store,
                    catalog=fixture.catalog,
                    restore_prepared=fixture.broker.validate_prepared_snapshot,
                    clock=harness.clock,
                    prepared_validator=validate_prepared,
                    current_guard=lambda operation: live(),
                    saved_root_guard=saved_root_guard,
                )
                harness.kernel.policy_roots[restored.selection_id] = restored
                restarted_gate = mode_module.SavedToolApprovalExecution(
                    ask=None,
                    broker=broker,
                    authority=authority,
                    bind_nested=bind_nested,
                    entry_guard_registry=registry,
                    policy_selection=restored,
                )
                with pytest.raises(PermissionError, match="already claimed"):
                    restarted_gate(invocation, execution, payload)
                payload["tool_call_id"] = "call-after-reload"
                assert restarted_gate(invocation, execution, payload) == {
                    "result": "finite policy fixture" if tool_kind == "read" else 5
                }
                assert effects == [operation, operation]
        else:
            with pytest.raises(
                (AuthorityDenied, PermissionError, ValueError, ProviderExecutionError)
            ):
                gate(invocation, execution, payload)
            assert effects == []
        assert releases == ([operation, operation] if outcome == "restart" else [operation])
