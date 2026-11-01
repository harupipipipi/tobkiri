"""Shared signed provider/executor parent fixture; no tool effects invoked."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace as NS
import threading
import time
import pytest
from tests.approval_policy_fixture import build_bounded_selection
from tests.fixtures.host_action_reviewer_v4 import build_shared_store_reviewer
from tests.test_saved_tool_consent_broker import make_artifact, Backend
from tests.test_authority_v4_lifecycle import _principal, _domain, _Resolver, _digest
from tests.test_tobkiri_host_authority_v4_adapter import _Principals, _Admission, _NoAdapters
from tests.test_interactive_approval_v4 import _CONTRACT
from core_runtime.host_contract import bind_host_contract
from core_runtime.authority.v4 import (
    AuthorityScope,
    AuthorityMode,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
    LeaseState,
    AuthorityDenied,
)
from core_runtime.authority.policy_exact_grant import PolicyExactGrantKernel
from core_runtime.policy_invocation_v4 import PolicyInvocationRegistry
from core_runtime.interactive_effect_coordinator import (
    HostInteractiveEffectService,
    CapturedInteractiveEffectRoute,
    INTERACTIVE_EFFECT_SPECS,
)
from core_runtime.owned_file_approval_v4 import assert_file_request_live
from ecosystem.rumi_default_tools_pack.runtime import file_create as create
from ecosystem.rumi_host_authority_bridge_pack.runtime.bridge import (
    InteractiveEffectCoordinatorBridgeV4,
)
from tobkiri_host.policy_exact_authority_adapter import (
    PolicyExactAuthorityV4Adapter,
    CommittedPolicyDerivationRoot,
)
from tobkiri_host.policy_effect_authorization import PolicyEffectAuthorizationAdapter
from tobkiri_host.saved_tool_entry_guards import SavedToolEntryGuardRegistry
from tobkiri_host.typed_pending_effect_persistence import TypedPendingEffectPersistence
from tobkiri_host.interactive_effects import PendingEffectController
from tobkiri_host.workspace_mutation import (
    HostWorkspaceMutationPort,
    WorkspaceMutationCoordinator,
    WorkspaceMutationBinding,
)
from tobkiri_host.models import OpaqueAuthorityRef, InvocationFrame, RuntimeEvidence, EffectClass
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_host.broker import RequestBroker, PreparedInvocationSnapshot
from tobkiri_host.backends import BackendRegistry
from tobkiri_host.contracts import AdapterPlanner, OperationRoute
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.effects import ProviderOutcome, InMemoryReconciliationStore


def provision_signed_executor_policy_parent(tmp_path, monkeypatch):
    """Provision signed source providers and live executor parent without write."""
    mode = "full"
    outcome = "cancel"
    if mode == "full" and outcome == "review_deny":
        pytest.skip("full does not review")
    live_wall_clock = outcome == "approve"
    saved_deadline_monotonic = time.monotonic() + 180
    if live_wall_clock:
        from tests import test_authority_v4_lifecycle as lifecycle
        from tests import approval_policy_fixture

        class WallClock:
            def __call__(self):
                return time.time()

        monkeypatch.setattr(lifecycle, "_MutableClock", WallClock)
        original_request = approval_policy_fixture._request_command

        def bounded_native_request(harness):
            command = original_request(harness)
            expiry = min(
                harness.clock() + 300,
                harness.clock() + max(0, saved_deadline_monotonic - time.monotonic()),
            )
            return replace(command, expires_at=expiry)

        monkeypatch.setattr(approval_policy_fixture, "_request_command", bounded_native_request)
    specs = {
        "executor": (
            "tobkiri.service.tool.execute.v1",
            "rumi_tool_local_executor_pack.tool-local-execute",
            "rumi_tool_local_executor_pack.tool-executor.local",
        ),
        "tool": (
            "tobkiri.service.tool.local.operation.v1",
            "rumi_default_tools_pack.file-create-operation",
            "rumi_default_tools_pack.file-create-tool",
        ),
        "coordinator": (
            "tobkiri.service.interactive-effect.v1",
            "interactive_effect.manage",
            "rumi_host_authority_bridge_pack.host-authority.interactive-effect",
        ),
        "prepare": (
            create.CONTRACT,
            create.PREPARE,
            "rumi_default_tools_pack.file-create-prepare.service",
        ),
        "execute": (create.CONTRACT, create.EXECUTE, "rumi_default_tools_pack.file-create.service"),
    }
    principals = {
        name: replace(_principal(seed, operation=op), function_id=function)
        for seed, (name, (_, op, function)) in zip("cdefg", specs.items())
    }
    domains = {name: _domain(name, p) for name, p in principals.items()}
    scopes = {
        name: AuthorityScope(
            capability="operation.invoke",
            semantics_digest=p.contract_revision_digest,
            dimensions={"contract": (specs[name][0],), "operation": (p.operation_id,)},
        )
        for name, p in principals.items()
    }
    artifacts = {name: make_artifact(p, specs[name][0]) for name, p in principals.items()}
    import json
    from scripts.generate_executable_catalogs_v4 import _operation_timeout_ms

    for name, (_, operation, function_id) in specs.items():
        pack_id = function_id.split(".", 1)[0]
        producer_bytes = (
            Path(create.__file__).parents[2] / pack_id / "executables.v4.json"
        ).read_bytes()
        catalog = json.loads(producer_bytes)
        declared = [
            op
            for variant in catalog["variants"]
            if variant["function_id"] == function_id
            for op in variant["operations"]
            if op["operation_id"] == operation
        ]
        assert len(declared) == 1
        selected = declared[0]
        artifacts[name] = replace(
            artifacts[name],
            functions=(
                replace(
                    artifacts[name].functions[0],
                    operations=(
                        replace(
                            artifacts[name].functions[0].operations[0],
                            timeout_default_ms=_operation_timeout_ms(
                                pack_id, function_id, operation
                            ),
                            timeout_hard_max_ms=selected["timeout_hard_max_ms"],
                        ),
                    ),
                ),
            ),
        )
    artifacts["execute"] = replace(
        artifacts["execute"],
        functions=(
            replace(
                artifacts["execute"].functions[0],
                operations=(
                    replace(
                        artifacts["execute"].functions[0].operations[0],
                        effect_class=EffectClass.PRIVILEGED,
                    ),
                ),
            ),
        ),
    )
    routes = {
        name: OperationRoute(
            contract_id=specs[name][0],
            operation_id=p.operation_id,
            artifact_digest=artifacts[name].digest,
            function_id=p.function_id,
            variant_id="provider.variant",
            execution_domain_profile="dedicated.provider",
            materialization_mode="on_demand",
            target_principal_ref=OpaqueAuthorityRef(p.principal_id),
        )
        for name, p in principals.items()
    }
    reviewers, reviewer_capture = ([], {})

    def configure(h, *_):
        if mode == "agent":
            review = build_shared_store_reviewer(h, lease_ttl_seconds=300)
            reviewers.append(review)
            reviewer_capture.update(review.facts)
            if outcome == "review_deny":
                original = review.transport.invoke

                def deny(contract, operation, payload, **kwargs):
                    import json

                    value = original(contract, operation, payload, **kwargs)
                    if contract == "tobkiri.service.ai.provider.generate.v1":
                        value["output"] = json.dumps(
                            {"decision": "deny", "reason": "Fixture write denied."}
                        )
                    return value

                review.transport.invoke = deny
        callers = {
            "executor": h.caller,
            "tool": principals["executor"],
            "coordinator": principals["tool"],
            "prepare": principals["coordinator"],
            "execute": principals["coordinator"],
        }
        for name, p in principals.items():
            domain, scope = (domains[name], scopes[name])
            h.kernel.register_execution_domain(
                domain,
                session_id="session-" + name,
                channel_digest=domain.authenticated_channel_digest,
                principal=p,
            )
            trust = HostExtensionTrustRecord(
                trust_id=name + "-trust",
                parent_artifact_digest=p.parent_artifact_digest,
                publisher_lineage="publisher.target",
                provider_principal_ids=(p.principal_id,),
                trust_provenance_digest=_digest(name + "-trust"),
                security_epoch=1,
                valid_from=h.clock(),
            )
            provider = ProviderAuthorityRecord(
                record_id=name + "-provider",
                provider=p,
                execution_domain_id=domain.domain_id,
                execution_domain_identity_digest=domain.identity_digest,
                scope=scope,
                authority_mode=AuthorityMode.LEASE_ONLY,
                security_epoch=1,
                trust_provenance_digest=trust.trust_provenance_digest,
                publisher_lineage="publisher.target",
                host_extension_id=trust.trust_id,
                valid_from=h.clock(),
                host_broker_binding="broker." + name,
            )
            approval = replace(
                h.approval, approval_id=name + "-ordinary", caller=callers[name], target=p
            )
            grant = replace(
                h.grant,
                grant_id=name + "-ordinary",
                caller=callers[name],
                target=p,
                scope=scope,
                approval_id=approval.approval_id,
                delegation_allowed=True,
                max_delegation_depth=4,
            )
            h.kernel.commit_approval_bundle(
                approval,
                host_extension_trust=trust,
                provider_authorities=(provider,),
                grants=(grant,),
            )
        original = h.kernel._binding_resolver

        class Resolver:
            def resolve_authority_binding(self, **kwargs):
                name = next((n for n, p in principals.items() if p == kwargs["target"]), None)
                return (
                    _Resolver(scopes[name]).resolve_authority_binding(**kwargs)
                    if name
                    else original.resolve_authority_binding(**kwargs)
                )

        h.kernel._binding_resolver = Resolver()
        boundary = {
            "contract": create.CONTRACT,
            "operation": create.EXECUTE,
            "caller_principal_id": principals["coordinator"].principal_id,
            "operation_class": EffectClass.PRIVILEGED.value,
            "ancestor_chain": [
                h.caller.principal_id,
                principals["executor"].principal_id,
                principals["tool"].principal_id,
            ],
            "ceiling": scopes["execute"].to_dict(),
            "caller_publisher_lineage": artifacts["coordinator"].publisher_lineage,
        }
        return (tuple(artifacts.values()), tuple(routes.values()), (boundary,))

    with bind_host_contract(_CONTRACT):
        selected = build_bounded_selection(
            tmp_path,
            monkeypatch,
            mode,
            configure=configure,
            reviewer_capture=reviewer_capture if mode == "agent" else None,
        )
        h = selected.harness
        h.kernel = PolicyExactGrantKernel(
            h.store, h.kernel._binding_resolver, clock=h.clock, lease_ttl_seconds=300
        )
        h.kernel.policy_roots = {}
        authority = PolicyExactAuthorityV4Adapter(
            h.kernel, _Principals(h.caller, h.target, *principals.values())
        )
        parent_context = h.context(
            request_id="executor-parent",
            request_digest=_digest("parent"),
            effect_digest=scopes["executor"].digest,
            target=principals["executor"],
            target_domain_id=domains["executor"].domain_id,
            activation_digest=selected.context.activation_digest,
        )
        auth = h.kernel.authorize(parent_context, scopes["executor"])
        parent = h.kernel.dispatch(
            auth.lease_token,
            target_domain_id=domains["executor"].domain_id,
            target_boot_epoch=domains["executor"].boot_epoch,
            request_digest=parent_context.request_digest,
        )
        root = selected.context
        contexts, parents, active = ({}, {}, {})
        registry, policy_registry = (SavedToolEntryGuardRegistry(), PolicyInvocationRegistry())
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        inode = workspace.stat()
        binding = WorkspaceMutationBinding(
            root.profile_id, "workspace", 1, workspace, inode.st_dev, inode.st_ino
        )
        mutation = HostWorkspaceMutationPort(
            WorkspaceMutationCoordinator(tmp_path / "locks"), binding_resolver=lambda *_: binding
        )
        monkeypatch.setattr(create, "_binding", lambda _: binding)
        native_capture = NS(
            user_data_root=tmp_path,
            profile_id=root.profile_id,
            workspace_mutation_port=mutation,
            interactive_approval_port=authority,
            authority_approval_window_port=object(),
            selected_tool_policy_port=policy_registry.capture_current,
        )
        saved = NS(
            envelope=NS(
                contract_id="conversation.saved-turn.v1",
                operation_id="saved_complete",
                context=root,
                payload={"request": {"turn_id": "turn-1", "action_approval_mode": mode}},
            ),
            parent=None,
            assert_current=lambda: None,
        )
        tool_broker = NS(
            envelope=NS(
                contract_id="tobkiri.service.tool.invoke.v1",
                operation_id="rumi_tool_broker_pack.tool-invoke",
                context=root,
            ),
            parent=saved,
            assert_current=lambda: None,
        )

        def live():
            current, state = h.store.inspect_lease_token(auth.lease_token)
            if (
                state is not LeaseState.DISPATCHED
                or current != parent
                or parent.expires_at <= h.clock()
                or (time.monotonic() >= saved_deadline_monotonic)
            ):
                raise AuthorityDenied("executor parent changed")

        invocation = NS(
            envelope=NS(
                contract_id=specs["executor"][0],
                operation_id=principals["executor"].operation_id,
                context=root,
                target_principal=OpaqueAuthorityRef(principals["executor"].principal_id),
                lease=OpaqueInvocationLease(auth.lease_token.encode("ascii")),
                cancellation_requested=threading.Event(),
                deadline_monotonic=saved_deadline_monotonic,
            ),
            parent_invocation=tool_broker,
            assert_current=live,
            presentation_owner_principal_id=root.caller_principal.value,
            presentation_owner_session_id=root.caller_session_id,
        )
        count = [0]

        def context_for(caller_name, target_name, *, chain=()):
            count[0] += 1
            value = replace(
                root,
                request_id="nested-" + str(count[0]),
                caller_principal=OpaqueAuthorityRef(principals[caller_name].principal_id),
                caller_session_id="session-" + caller_name,
                caller_domain_id=domains[caller_name].domain_id,
                caller_boot_epoch=domains[caller_name].boot_epoch,
                target_domain_id=domains[target_name].domain_id,
                target_boot_epoch=domains[target_name].boot_epoch,
                target_backend_digest=_digest("backend"),
                delegation_chain=chain,
            )
            contexts[value.request_id] = value
            return value

        effects = []

        class ActualBackend(Backend):
            def materialize(self, binding, reservation_id):
                name = next(
                    (
                        n
                        for n, p in principals.items()
                        if p.principal_id == binding.principal_ref.value
                    )
                )
                return RuntimeEvidence(
                    domain_ref=OpaqueAuthorityRef(domains[name].domain_id),
                    executable_digest=principals[name].function_implementation_digest,
                    backend_digest=self.status.backend_digest,
                    authenticated_channel=True,
                    nonce_fresh=True,
                )

            def invoke(self, envelope):
                name = next(
                    (
                        n
                        for n, p in principals.items()
                        if p.principal_id == envelope.target_principal.value
                    )
                )
                lease, state = h.store.inspect_lease_token(envelope.lease.token.decode("ascii"))
                assert state is LeaseState.DISPATCHED
                upstream = parents.get(envelope.context.request_id, invocation)
                scope = NS(
                    envelope=upstream.envelope,
                    parent=upstream.parent_invocation,
                    assert_current=upstream.assert_current,
                )
                guard = registry.capture(envelope)

                def current():
                    _, state = h.store.inspect_lease_token(envelope.lease.token.decode("ascii"))
                    if state is not LeaseState.DISPATCHED:
                        raise AuthorityDenied("provider drained")
                    if guard:
                        guard()

                actual = NS(
                    envelope=envelope,
                    parent_invocation=scope,
                    assert_current=current,
                    presentation_owner_principal_id=root.caller_principal.value,
                    presentation_owner_session_id=root.caller_session_id,
                )

                def client(**_):

                    def invoke(contract, operation, payload):
                        target_name = next(
                            (n for n, s in specs.items() if s[:2] == (contract, operation))
                        )
                        ctx = context_for(
                            name,
                            target_name,
                            chain=tuple(
                                (
                                    OpaqueAuthorityRef(x)
                                    for x in lease.call_chain + (lease.caller.principal_id,)
                                )
                            ),
                        )
                        parents[ctx.request_id] = actual
                        return broker.invoke(
                            InvocationFrame(
                                contract_id=contract,
                                version_range=None,
                                operation_id=operation,
                                payload=payload,
                            ),
                            ctx,
                            effect_scope=scopes[target_name].to_dict(),
                        )

                    return NS(invoke=invoke)

                actual.contract_client = client
                active[envelope.context.request_id] = actual
                if name == "tool":
                    result = create._bind_tool(native_capture)(envelope.payload, actual)
                elif name == "prepare":
                    result = create._bind_prepare(native_capture)(envelope.payload, actual)
                    if outcome == "mutated_plan":
                        result = {**result, "content_digest": _digest("changed")}
                    if outcome == "preexisting":
                        (workspace / "created.txt").write_text("existing")
                    if outcome == "cancel":
                        invocation.envelope.cancellation_requested.set()
                elif name == "execute":
                    result = create._bind_execute(native_capture)(envelope.payload, actual)
                    effects.append("execute")
                else:
                    result = bridge.invoke(envelope.operation_id, envelope.payload, actual)
                return ProviderOutcome(result)

        backend = ActualBackend(h, h.target, h.target_domain, tmp_path / "unused", "approve", {})
        broker = RequestBroker(
            catalog=selected.catalog,
            adapters=AdapterPlanner(()),
            adapter_executor=_NoAdapters(),
            backends=BackendRegistry((backend,)),
            materialization=MaterializationCoordinator(),
            admission=_Admission(),
            authority=authority,
            audit=authority,
            reconciliation=InMemoryReconciliationStore(),
        )
        dispatch_errors = []
        original_invoke_prepared = broker.invoke_prepared

        def observe_invoke_prepared(*args, **kwargs):
            try:
                return original_invoke_prepared(*args, **kwargs)
            except Exception as exc:
                dispatch_errors.append((type(exc).__name__, str(exc)))
                raise

        broker.invoke_prepared = observe_invoke_prepared

        def validate(retained):
            broker.validate_prepared_snapshot(
                PreparedInvocationSnapshot.from_dict(retained.prepared_snapshot),
                contexts[retained.context.request_id],
            )

        def saved_guard(actual, authenticated, capture):
            live()
            if (
                actual is not invocation
                or authenticated != root
                or capture["workspace"] != str(tmp_path.resolve())
            ):
                raise AuthorityDenied("saved root changed")

        policy_root = CommittedPolicyDerivationRoot(
            policy=selected.policy,
            command=selected.command,
            expected=selected.kwargs,
            prepared_validator=validate,
            current_guard=lambda _: live(),
            saved_root_guard=saved_guard,
            reviewer=reviewers[0].reviewer if reviewers else None,
            review_transport_verifier=reviewers[0].verifier if reviewers else None,
        )
        h.kernel.policy_roots[policy_root.selection_id] = policy_root
        policy_port = PolicyEffectAuthorizationAdapter(
            authority=authority,
            broker=broker,
            root_for=lambda _: policy_root,
            owned_file_guard=assert_file_request_live,
        )
        controller = PendingEffectController(
            persistence=TypedPendingEffectPersistence(h.store),
            approvals=authority,
            coordinator_principal=OpaqueAuthorityRef(principals["coordinator"].principal_id),
            coordinator_publisher_lineage="publisher.target",
            policy_authorizations=policy_port,
            clock=h.clock,
        )
        route = CapturedInteractiveEffectRoute(
            INTERACTIVE_EFFECT_SPECS["file_create"],
            OpaqueAuthorityRef(principals["coordinator"].principal_id),
            OpaqueAuthorityRef(principals["execute"].principal_id),
            scopes["execute"],
        )
        service = HostInteractiveEffectService(
            broker=broker,
            controller=controller,
            routes=(route,),
            context_for_execute=lambda *_: context_for("coordinator", "execute"),
            assert_current_capture=live,
            profile_id=root.profile_id,
            activation_id=root.activation_id,
            plan_digest=root.plan_digest,
            security_epoch=root.security_epoch,
            policy_effect_authorization_port=policy_port,
            clock=h.clock,
        )
        capture = NS(
            profile_id=root.profile_id,
            plan_digest=root.plan_digest,
            security_epoch=root.security_epoch,
            activation={"activation_id": root.activation_id},
            selected_tool_policy_port=policy_registry.capture_current,
        )
        bridge = InteractiveEffectCoordinatorBridgeV4(
            capture=capture,
            binding=selected.catalog.resolve_pinned(*specs["coordinator"][:2]),
            effect_port=service,
        )

        return NS(**locals())
