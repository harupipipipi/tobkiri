"""Actual store and two Broker routes: consent never adds a second read Grant."""

import tobkiri_host.saved_tool_entry_guards as _host_saved_tool_entry_guards
import tobkiri_host.consumed_tool_consent as _host_consumed_tool_consent
from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import pytest
from core_runtime.authority.v4 import (
    AuthorityScope,
    HostExtensionTrustRecord,
    ProviderAuthorityRecord,
    AuthorityMode,
)
from core_runtime.host_contract import bind_host_contract
from tests import test_authority_v4_lifecycle as lifecycle
from tests.test_authority_v4_lifecycle import (
    _Harness,
    _principal,
    _domain,
    _Resolver,
    _digest,
)
from tests.test_interactive_approval_v4 import _CONTRACT, _decision_command
from tests.test_tobkiri_host_authority_v4_adapter import (
    _context,
    _Principals,
    _Admission,
    _NoAdapters,
    _artifact,
)
from tobkiri_host.authority_v4 import AuthorityV4Adapter
from tobkiri_host.backends import (
    BackendRegistry,
    BackendStatus,
    REQUIRED_PRODUCTION_GATES,
)
from tobkiri_host.broker import RequestBroker
from tobkiri_host.contracts import OperationCatalog, OperationRoute, AdapterPlanner
from tobkiri_host.effects import ProviderOutcome, InMemoryReconciliationStore
from tobkiri_host.materialization import MaterializationCoordinator
from tobkiri_host.models import (
    OpaqueAuthorityRef,
    RuntimeEvidence,
    ExecutionKind,
    EffectClass,
)
from tobkiri_host.ports import InteractiveApprovalGetQuery
import tobkiri_host.saved_tool_consent as consent_module
import tobkiri_host.saved_tool_context as prepared_module

READ_CONTRACT = "tobkiri.service.tool.local.operation.v1"
READ_OPERATION = "rumi_default_tools_pack.files-read-operation"
READ_FUNCTION = "rumi_default_tools_pack.files-read"
CONSENT_FUNCTION = "rumi_host_authority_bridge_pack.host-authority.host-consent"


def make_artifact(principal, contract, *, consent=False):
    stub = SimpleNamespace(target=principal)
    artifact = _artifact(stub)
    operation = replace(
        artifact.functions[0].operations[0],
        contract_id=contract,
        operation_id=principal.operation_id,
        effect_class=EffectClass.PURE if consent else EffectClass.READ,
        idempotency="none",
        reconcile_operation=None,
    )
    function = replace(artifact.functions[0], operations=(operation,))
    return replace(
        artifact,
        pack_id=("rumi_host_authority_bridge_pack" if consent else "rumi_default_tools_pack"),
        functions=(function,),
        variants=(
            replace(
                artifact.variants[0],
                execution_kind=ExecutionKind.HOST_EXTENSION,
                backend="tobkiri.python-host-v4",
            ),
        ),
    )


class Backend:
    def __init__(
        self,
        harness,
        consent_principal,
        consent_domain,
        fixture,
        outcome,
        original_payload,
    ):
        self.harness, self.consent, self.domain = (
            harness,
            consent_principal,
            consent_domain,
        )
        self.fixture, self.outcome, self.original_payload = (
            fixture,
            outcome,
            original_payload,
        )
        self.calls = []
        self.effects = []
        self.status = BackendStatus(
            backend_id="tobkiri.python-host-v4",
            execution_kind=ExecutionKind.HOST_EXTENSION,
            platform="macos-arm64",
            backend_digest=_digest("backend"),
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )

    def materialize(self, binding, reservation_id):
        if (
            binding.principal_ref.value != self.consent.principal_id
            and self.outcome == "revoke_pre_effect"
        ):
            decision = self.harness.store.get_interactive_approval_decision(self.native_request_id)
            self.harness.kernel.revoke(
                target_kind="grant",
                target_id=decision.grant_id,
                reason="fixture consent revoked before read effect",
            )
        target = (
            self.consent
            if binding.principal_ref.value == self.consent.principal_id
            else self.harness.target
        )
        domain = self.domain if target == self.consent else self.harness.target_domain
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(domain.domain_id),
            executable_digest=target.function_implementation_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, envelope):
        self.calls.append(envelope.operation_id)
        if envelope.operation_id == consent_module.CONSENT_OPERATION:
            if self.outcome == "changed_args":
                self.original_payload["arguments"].update(
                    {"expression": "999"}
                    if self.original_payload["tool_id"] == "calculator"
                    else {"path": "changed.txt"}
                )
            if self.outcome == "stale":
                self.invocation.stale = True
            return ProviderOutcome(
                {"admitted_operation_digest": envelope.payload["retained_operation_digest"]}
            )
        if self.outcome == "revoke_nested":
            parent_guard = self.entry_guards.capture(envelope)
            assert parent_guard is not None
            decision = self.harness.store.get_interactive_approval_decision(self.native_request_id)
            self.harness.kernel.revoke(
                target_kind="grant",
                target_id=decision.grant_id,
                reason="fixture consumed consent revoked inside original provider",
            )
            parent_guard()
        self.effects.append(envelope.operation_id)
        if envelope.operation_id == "rumi_default_tools_pack.calculator-evaluate":
            from ecosystem.rumi_default_tools_pack.domain.tool.calculator import (
                calculate,
            )

            return ProviderOutcome(
                {"result": calculate(envelope.payload["arguments"]["expression"])}
            )
        assert envelope.payload["arguments"] == {"path": "fixture.txt"}
        return ProviderOutcome({"result": self.fixture.read_text()})

    def cancel(self, request_id):
        pass

    def terminate(self, domain_id):
        pass


@pytest.mark.parametrize("tool_kind", ["read", "calculator"])
@pytest.mark.parametrize(
    "outcome",
    [
        "approve",
        "deny",
        "stale",
        "changed_args",
        "oversize",
        "unknown_args",
        "revoke_between",
        "revoke_pre_effect",
        "revoke_nested",
    ],
)
def test_native_consent_uses_distinct_grant_then_actual_read_broker(
    tmp_path, monkeypatch, outcome, tool_kind
):
    operation = (
        READ_OPERATION if tool_kind == "read" else "rumi_default_tools_pack.calculator-evaluate"
    )
    function = READ_FUNCTION if tool_kind == "read" else "rumi_default_tools_pack.calculator"
    read_principal = replace(_principal("b", operation=operation), function_id=function)
    original_principal = lifecycle._principal
    monkeypatch.setattr(
        lifecycle,
        "_principal",
        lambda seed, **kwargs: (
            read_principal if seed == "b" else original_principal(seed, **kwargs)
        ),
    )
    read_scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=read_principal.contract_revision_digest,
        dimensions={"contract": (READ_CONTRACT,), "operation": (operation,)},
        quotas={"operations": 1},
    )
    harness = _Harness(tmp_path, scope=read_scope)
    consent_principal = replace(
        _principal("c", operation=consent_module.CONSENT_OPERATION),
        function_id=CONSENT_FUNCTION,
    )
    consent_domain = _domain("consent", consent_principal)
    consent_scope = AuthorityScope(
        capability="operation.invoke",
        semantics_digest=consent_principal.contract_revision_digest,
        dimensions={
            "contract": (consent_module.CONSENT_CONTRACT,),
            "operation": (consent_module.CONSENT_OPERATION,),
        },
        quotas={"operations": 1},
    )

    class Resolver:
        def resolve_authority_binding(self, **kwargs):
            scope = consent_scope if kwargs["target"] == consent_principal else read_scope
            return _Resolver(scope).resolve_authority_binding(**kwargs)

    harness.kernel._binding_resolver = Resolver()
    harness.kernel.register_execution_domain(
        consent_domain,
        session_id="session-consent",
        channel_digest=consent_domain.authenticated_channel_digest,
        principal=consent_principal,
    )
    trust = HostExtensionTrustRecord(
        trust_id="consent-trust",
        parent_artifact_digest=consent_principal.parent_artifact_digest,
        publisher_lineage="publisher.target",
        provider_principal_ids=(consent_principal.principal_id,),
        trust_provenance_digest=_digest("consent-trust"),
        security_epoch=1,
        valid_from=harness.clock(),
    )
    provider = ProviderAuthorityRecord(
        record_id="consent-provider",
        provider=consent_principal,
        execution_domain_id=consent_domain.domain_id,
        execution_domain_identity_digest=consent_domain.identity_digest,
        scope=consent_scope,
        authority_mode=AuthorityMode.LEASE_ONLY,
        security_epoch=1,
        trust_provenance_digest=trust.trust_provenance_digest,
        publisher_lineage="publisher.target",
        host_extension_id=trust.trust_id,
        valid_from=harness.clock(),
        host_broker_binding="broker.consent.v1",
    )
    harness.kernel.commit_provider_authority_bundle(
        provider_authorities=(provider,), host_extension_trust=trust
    )
    shell = _principal("shell")
    shell_domain = _domain("shell", shell)
    harness.kernel.register_execution_domain(
        shell_domain,
        session_id="session-shell",
        channel_digest=shell_domain.authenticated_channel_digest,
        principal=shell,
    )
    authority = AuthorityV4Adapter(
        harness.kernel,
        _Principals(harness.caller, harness.target, consent_principal, shell),
    )
    artifacts = (
        make_artifact(read_principal, READ_CONTRACT),
        make_artifact(consent_principal, consent_module.CONSENT_CONTRACT, consent=True),
    )
    routes = tuple(
        (
            OperationRoute(
                contract_id=artifact.functions[0].operations[0].contract_id,
                operation_id=artifact.functions[0].operations[0].operation_id,
                artifact_digest=artifact.digest,
                function_id=artifact.functions[0].function_id,
                variant_id="provider.variant",
                execution_domain_profile="dedicated.provider",
                materialization_mode="on_demand",
                target_principal_ref=OpaqueAuthorityRef(principal.principal_id),
            )
            for artifact, principal in zip(artifacts, (read_principal, consent_principal))
        )
    )
    catalog = OperationCatalog(artifacts, routes)
    fixture = tmp_path / "fixture.txt"
    fixture.write_text("isolated fixture content")
    payload = {
        "tool_id": "coding_file_read",
        "tool_call_id": "call-1",
        "arguments": {"path": "fixture.txt"},
    }
    if tool_kind == "calculator":
        payload["tool_id"] = "calculator"
        payload["arguments"] = {"expression": "2 + 3"}
    if outcome == "oversize":
        payload["arguments"] = (
            {"path": "x" * 600} if tool_kind == "read" else {"expression": " " * 600 + "2"}
        )
    if outcome == "unknown_args":
        payload["arguments"]["password"] = "untrusted"
    backend = Backend(harness, consent_principal, consent_domain, fixture, outcome, payload)
    broker = RequestBroker(
        catalog=catalog,
        adapters=AdapterPlanner(()),
        adapter_executor=_NoAdapters(),
        backends=BackendRegistry((backend,)),
        materialization=MaterializationCoordinator(),
        admission=_Admission(),
        authority=authority,
        audit=authority,
        reconciliation=InMemoryReconciliationStore(),
    )
    root_context = replace(
        _context(harness, request_id="saved-root"),
        caller_principal=OpaqueAuthorityRef(shell.principal_id),
        caller_session_id="session-shell",
        caller_domain_id=shell_domain.domain_id,
    )

    def parent(contract, operation, ancestor=None, request=None):
        return SimpleNamespace(
            envelope=SimpleNamespace(
                contract_id=contract,
                operation_id=operation,
                context=root_context,
                payload={"request": request} if request else {},
            ),
            parent=ancestor,
            assert_current=lambda: None,
        )

    saved = parent("conversation.saved-turn.v1", "saved_complete", request={"turn_id": "turn-1"})
    parent_broker = parent(
        "tobkiri.service.tool.invoke.v1", "rumi_tool_broker_pack.tool-invoke", saved
    )
    invocation = SimpleNamespace(
        envelope=SimpleNamespace(
            contract_id="tobkiri.service.tool.execute.v1",
            operation_id="rumi_tool_local_executor_pack.tool-local-execute",
            context=root_context,
            target_principal=OpaqueAuthorityRef(harness.caller.principal_id),
            cancellation_requested=threading.Event(),
            deadline_monotonic=time.monotonic() + 45,
        ),
        parent_invocation=parent_broker,
        presentation_owner_principal_id=shell.principal_id,
        presentation_owner_session_id="session-shell",
        stale=False,
    )

    def guard():
        if invocation.stale:
            raise PermissionError("stale capture")

    invocation.assert_current = guard
    backend.invocation = invocation
    opened = []

    def open_window(command):
        opened.append(command.request_id)
        backend.native_request_id = command.request_id
        decision = _decision_command(
            harness,
            command.request_id,
            phrase="",
            action="deny" if outcome == "deny" else "approve",
        )
        decision = replace(decision, context=replace(root_context, request_id=command.request_id))
        pending = authority.get_interactive_approval(
            InteractiveApprovalGetQuery(root_context, command.request_id)
        )
        assert str(payload["arguments"]) != ""
        assert pending.redacted_metadata["summary"].endswith(
            '{"expression":"2 + 3"}' if tool_kind == "calculator" else '{"path":"fixture.txt"}'
        )
        (
            authority.deny_interactive_approval
            if outcome == "deny"
            else authority.approve_interactive_approval
        )(decision)
        return {"opened": True, "request_id": command.request_id}

    releases = []

    def bind_nested(invocation, contract, operation, request):
        context = replace(
            _context(harness),
            request_id=(
                "consent-request" if contract == consent_module.CONSENT_CONTRACT else "read-request"
            ),
            target_domain_id=(
                consent_domain.domain_id
                if contract == consent_module.CONSENT_CONTRACT
                else harness.target_domain.domain_id
            ),
        )
        return prepared_module.NestedToolBinding(
            context,
            (
                consent_scope.to_dict()
                if contract == consent_module.CONSENT_CONTRACT
                else read_scope.to_dict()
            ),
            "publisher.caller",
            None,
            lambda: releases.append(operation),
        )

    route = consent_module.ConsentRouteAttestation(
        catalog.resolve(consent_module.CONSENT_CONTRACT, consent_module.CONSENT_OPERATION, None),
        consent_scope.to_dict(),
    )
    Verifier = _host_consumed_tool_consent.ConsumedConsentVerifier

    class CurrentConsent(Verifier):
        def assert_current(self, proof):
            if outcome == "revoke_between" and (not getattr(self, "revoked", False)):
                super().assert_current(proof)
                decision = harness.store.get_interactive_approval_decision(
                    proof.command.context.request_id
                )
                harness.kernel.revoke(
                    target_kind="grant",
                    target_id=decision.grant_id,
                    reason="fixture consumed consent revoked after commit",
                )
                self.revoked = True
            super().assert_current(proof)

    current_consent = CurrentConsent(harness.store, clock=harness.clock)
    entry_guards = _host_saved_tool_entry_guards.SavedToolEntryGuardRegistry()
    backend.entry_guards = entry_guards
    executor = consent_module.SavedToolConsentExecution(
        broker=broker,
        authority=authority,
        window=SimpleNamespace(open_authority_approval_window=open_window),
        route=route,
        bind_nested=bind_nested,
        clock=harness.clock,
        consent_proof=current_consent,
        entry_guard_registry=entry_guards,
    )
    execution = {
        "contract_id": READ_CONTRACT,
        "operation": operation,
        "provider_instance_id": function,
    }
    try:
        with bind_host_contract(_CONTRACT):
            if outcome == "approve":
                assert executor(invocation, execution, payload) == {
                    "result": "isolated fixture content" if tool_kind == "read" else 5
                }
                recreated = consent_module.SavedToolConsentExecution(
                    broker=broker,
                    authority=authority,
                    window=SimpleNamespace(open_authority_approval_window=open_window),
                    route=route,
                    bind_nested=bind_nested,
                    clock=harness.clock,
                    entry_guard_registry=_host_saved_tool_entry_guards.SavedToolEntryGuardRegistry(),
                    consent_proof=_host_consumed_tool_consent.ConsumedConsentVerifier(
                        harness.store, clock=harness.clock
                    ),
                )
                with pytest.raises(PermissionError, match="already claimed"):
                    recreated(invocation, execution, payload)
                status = authority.get_interactive_approval(
                    InteractiveApprovalGetQuery(root_context, opened[0])
                )
                assert status.remaining_uses == 0
                assert backend.calls == [consent_module.CONSENT_OPERATION, operation]
            else:
                with pytest.raises(Exception):
                    executor(invocation, execution, payload)
                if outcome == "revoke_nested":
                    assert operation in backend.calls
                    assert backend.effects == []
                else:
                    assert operation not in backend.calls
        assert (
            len([grant for grant in harness.store.list_grants() if grant.target == harness.target])
            == 1
        )
        if outcome in {"oversize", "unknown_args"}:
            assert releases == []
            assert opened == []
            assert backend.calls == []
        else:
            assert releases == [consent_module.CONSENT_OPERATION, operation]
    finally:
        broker.close()
