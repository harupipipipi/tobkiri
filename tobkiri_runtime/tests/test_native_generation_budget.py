"""Existing Native approval and management hooks, with ephemeral Host state."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
import time

import pytest

from core_runtime.authority.v4 import AuthorityScope, AuthorityStore
from core_runtime.credential_transport import (
    CredentialMaterialStoreBinding,
    CredentialTransportDenied,
)
from core_runtime.host_contract import bind_host_contract
from core_runtime.interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_SPECS,
    _execute_payload,
    _presentation_metadata,
)
from core_runtime.invocation_scope_v4 import (
    CapturedInvocationScopeV4,
    assert_dispatched_invocation,
)
from core_runtime.native_generation_budget import (
    CONFIGURE_CONTRACT,
    CONFIGURE_FUNCTION,
    CONFIGURE_OPERATION,
    MANAGE_FUNCTION,
    NativeGenerationBudgetOwner,
    NativeGenerationBudgetTemplate,
    capture_native_generation_budget,
    owner_startup_generation_budget_template,
)
from ecosystem.rumi_credential_broker_pack.runtime.process import (
    CredentialManagementHostFactoryV4,
)
from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
    CREDENTIAL_CONTRACT,
    CREDENTIAL_OPERATION,
    execute_configuration,
    prepare_configuration,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tests import test_authority_v4_lifecycle as lifecycle
from tests import test_interactive_approval_v4 as native
from tests.test_tobkiri_host_authority_v4_adapter import _context, _digest
from tobkiri_host.broker import RequestEnvelope
from tobkiri_host.credential_store import HostCredentialMaterialStore, KEY_VERSION
from tobkiri_host.models import OpaqueAuthorityRef
from tobkiri_host.ports import OpaqueInvocationLease
from tobkiri_protocol.canonical import canonical_digest


@pytest.mark.parametrize(
    "connection_name, accepted",
    [("openrouter.main", True), ("main", False), ("openrouter.default", False)],
)
def test_fixed_owner_accepts_only_normal_openrouter_main_public_plan(
    tmp_path: Path, connection_name: str, accepted: bool,
) -> None:
    """The usual preset identity reaches the real Host guard without aliases."""
    with AuthorityStore(tmp_path / "authority" / "v4.sqlite3") as authority:
        user_data = tmp_path / "owner"
        material = HostCredentialMaterialStore(user_data_root=user_data)
        template = owner_startup_generation_budget_template()
        binding = capture_native_generation_budget(
            binding=CredentialMaterialStoreBinding(material, KEY_VERSION),
            authority_store=authority,
            user_data_root=user_data,
            profile_id="defaults",
            template=template,
        )
        owner = binding.generation_admission
        assert isinstance(owner, NativeGenerationBudgetOwner)
        registry = ProviderRegistry("defaults", user_data_root=user_data)
        request = {
            "connection_name": connection_name,
            "protocol": "openai-compatible",
            "endpoint": "https://openrouter.ai/api/v1",
            "key_value": "synthetic-openrouter-dummy-key",
            "catalog_provider_id": "openrouter",
            "display_name": "main",
        }
        try:
            public_plan = prepare_configuration(registry, request)
            assert {
                key: public_plan[key]
                for key in (
                    "profile_id", "provider_instance_id", "adapter_id", "endpoint"
                )
            } == {
                "profile_id": "defaults",
                "provider_instance_id": "provider." + connection_name,
                "adapter_id": "openai-compatible",
                "endpoint": "https://openrouter.ai/api/v1",
            }
            assert not material.requires_generation_budget(profile_id="defaults")
            assert authority.get_host_pending_effect(owner._active_id) is None
            assert owner._witness.get_host_pending_effect(owner._active_id) is None
            if accepted:
                prepared = prepare_configuration(registry, request, budget_port=owner)
                policy = prepared["generation_budget"]
                assert policy["connection_id"] == "provider.openrouter.main"
                assert policy["max_requests"] == 3
                assert policy["max_output_tokens"] == 2048
                assert policy["model"] == "stepfun/step-5-preview"
                assert policy["max_prompt_price_microusd_per_million"] == 1_000_000
                assert policy["max_completion_price_microusd_per_million"] == 2_700_000
                assert policy["external_key_limit_microusd"] == 2_000_000
                assert prepare_configuration(registry, request, budget_port=owner) == prepared
            else:
                with pytest.raises(CredentialTransportDenied) as denied:
                    prepare_configuration(registry, request, budget_port=owner)
                assert denied.value.code == "generation_budget_invalid"
            assert material.list(profile_id="defaults")["count"] == 0
            assert registry.snapshot()["revision"] == 0
            assert not registry.path.exists()
        finally:
            owner.close()


def test_fixed_owner_rejects_immutable_previous_connection_template(
    tmp_path: Path,
) -> None:
    """A connection correction cannot replace an existing required Host policy."""
    with AuthorityStore(tmp_path / "authority" / "v4.sqlite3") as authority:
        user_data = tmp_path / "owner"
        material = HostCredentialMaterialStore(user_data_root=user_data)
        previous = NativeGenerationBudgetTemplate("defaults", "provider.main")
        binding = capture_native_generation_budget(
            binding=CredentialMaterialStoreBinding(material, KEY_VERSION),
            authority_store=authority,
            user_data_root=user_data,
            profile_id="defaults",
            template=previous,
        )
        owner = binding.generation_admission
        assert isinstance(owner, NativeGenerationBudgetOwner)
        corrected = owner_startup_generation_budget_template()
        assert corrected is not None
        assert previous.effect_id == corrected.effect_id
        primary_before = authority.get_host_pending_effect(previous.effect_id)
        witness_before = owner._witness.get_host_pending_effect(previous.effect_id)
        assert primary_before == witness_before
        assert authority.get_host_pending_effect(owner._active_id) is None
        assert owner._witness.get_host_pending_effect(owner._active_id) is None
        assert not material.requires_generation_budget(profile_id="defaults")
        try:
            with pytest.raises(CredentialTransportDenied) as denied:
                NativeGenerationBudgetOwner(
                    template=corrected,
                    authority_store=authority,
                    witness_store=owner._witness,
                    material_store=material,
                    key_version=KEY_VERSION,
                    install=True,
                )
            assert denied.value.code == "generation_budget_invalid"
            assert authority.get_host_pending_effect(previous.effect_id) == primary_before
            assert owner._witness.get_host_pending_effect(previous.effect_id) == witness_before
            assert material.list(profile_id="defaults")["count"] == 0
        finally:
            owner.close()


def envelope(harness, issued, context, payload, *, contract, operation):
    durable, _ = harness.store.inspect_lease_token(issued.lease_token)
    harness.kernel.dispatch(
        issued.lease_token,
        target_domain_id=context.target_domain_id,
        target_boot_epoch=context.target_boot_epoch,
        request_digest=durable.request_digest,
    )
    return RequestEnvelope(
        context=context,
        target_principal=OpaqueAuthorityRef(durable.target.principal_id),
        target_domain=OpaqueAuthorityRef(context.target_domain_id),
        contract_id=contract,
        contract_version="1.0.0",
        operation_id=operation,
        payload=payload,
        request_digest=durable.request_digest,
        deadline_monotonic=time.monotonic() + 60,
        lease=OpaqueInvocationLease(issued.lease_token.encode("ascii")),
        idempotency_key=None,
    )


@pytest.fixture
def setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    template: NativeGenerationBudgetTemplate | None = None,
):
    original = lifecycle._principal

    def principal(seed, *, operation="invoke"):
        value = original(seed, operation=operation)
        return (
            replace(value, function_id=CONFIGURE_FUNCTION, operation_id=CONFIGURE_OPERATION)
            if seed == "b"
            else value
        )

    monkeypatch.setattr(lifecycle, "_principal", principal)
    harness = lifecycle._Harness(tmp_path / "authority")
    material = HostCredentialMaterialStore(user_data_root=tmp_path / "owner")
    binding = capture_native_generation_budget(
        binding=CredentialMaterialStoreBinding(material, KEY_VERSION),
        authority_store=harness.store,
        user_data_root=tmp_path / "owner",
        profile_id="profile-1",
        template=template or NativeGenerationBudgetTemplate("profile-1", "provider.main"),
    )
    owner = binding.generation_admission
    registry = ProviderRegistry("profile-1", user_data_root=tmp_path / "owner")
    request = {
        "connection_name": "main",
        "protocol": "openai-compatible",
        "endpoint": "https://openrouter.ai/api/v1",
        "key_value": "synthetic-native-test-key",
    }
    plan = prepare_configuration(registry, request, budget_port=owner)
    spec = INTERACTIVE_EFFECT_SPECS["provider_configure"]
    payload = _execute_payload(spec, request, plan)
    digest = canonical_digest(payload)
    presentation = _presentation_metadata(
        spec,
        SimpleNamespace(
            normalized_payload=payload,
            request_digest=digest,
        ),
    )
    authority = native._adapter(harness)
    command = native._request_command(harness, request_digest=digest, phrase=None)
    command = replace(command, redacted_metadata=presentation)
    with bind_host_contract(native._CONTRACT):
        pending = authority.request_interactive_approval(command)
        authority.approve_interactive_approval(
            native._decision_command(harness, pending.request_id)
        )
    harness.store.revoke(
        target_kind="grant", target_id="grant-1", reason="use-native-only"
    )
    approved_request = harness.store.get_interactive_approval_request(
        pending.request_id
    )
    scope = AuthorityScope.from_dict(approved_request.base_scope.to_dict())
    harness.kernel.commit_provider_authority_bundle(
        provider_authorities=(
            replace(
                harness.provider, record_id="native-configure-provider", scope=scope
            ),
        )
    )
    harness.store.revoke(
        target_kind="provider_authority",
        target_id=harness.provider.record_id,
        reason="exact-native-fixture",
    )
    harness.kernel._binding_resolver = lifecycle._Resolver(scope)
    parent_context = _context(harness, request_id="native-configure-execution")
    issued = harness.kernel.authorize(
        harness.context(request_id=parent_context.request_id, request_digest=digest),
        scope,
    )
    parent = envelope(
        harness,
        issued,
        parent_context,
        payload,
        contract=CONFIGURE_CONTRACT,
        operation=CONFIGURE_OPERATION,
    )

    harness.kernel._binding_resolver = lifecycle._Resolver(harness.scope)
    manager = replace(
        original("manager"),
        function_id=MANAGE_FUNCTION,
        operation_id=CREDENTIAL_OPERATION,
    )
    manager_domain = lifecycle._domain("manager", manager)
    harness.kernel.register_execution_domain(
        manager_domain,
        session_id="session-manager",
        channel_digest=manager_domain.authenticated_channel_digest,
        principal=manager,
    )
    child_approval = replace(
        harness.approval,
        approval_id="child-fixture-approval",
        caller=harness.target,
        target=manager,
    )
    child_grant = replace(
        harness.grant,
        grant_id="child-fixture-grant",
        caller=harness.target,
        target=manager,
        approval_id=child_approval.approval_id,
    )
    child_provider = replace(
        harness.provider,
        record_id="child-provider-authority",
        provider=manager,
        execution_domain_id=manager_domain.domain_id,
        execution_domain_identity_digest=manager_domain.identity_digest,
        host_extension_id="child-extension",
    )
    child_extension = replace(
        harness.extension_trust,
        trust_id="child-extension",
        parent_artifact_digest=manager.parent_artifact_digest,
        provider_principal_ids=(manager.principal_id,),
    )
    harness.kernel.commit_approval_bundle(
        child_approval,
        host_extension_trust=child_extension,
        provider_authorities=(child_provider,),
        grants=(child_grant,),
    )
    credential_payload = {
        "operation": "create",
        "profile_id": "profile-1",
        "consumer_pack_id": "rumi_provider_adapters_pack",
        "provider_instance_id": "provider.main",
        "secret_material": {"api_key": request["key_value"]},
        "scopes": ["ai.generate", "ai.stream"],
    }
    child_digest = canonical_digest(credential_payload)
    child_context = replace(
        parent_context,
        request_id="native-credential-create",
        caller_principal=OpaqueAuthorityRef(harness.target.principal_id),
        caller_session_id="session-target",
        caller_domain_id=harness.target_domain.domain_id,
        target_domain_id=manager_domain.domain_id,
        handle_namespace=harness.target_domain.resource_namespace,
    )
    child_issued = harness.kernel.authorize(
        harness.context(
            request_id=child_context.request_id,
            request_digest=child_digest,
            caller_session_id="session-target",
            target=manager,
            target_domain_id=manager_domain.domain_id,
        ),
        harness.scope,
    )
    child = envelope(
        harness,
        child_issued,
        child_context,
        credential_payload,
        contract=CREDENTIAL_CONTRACT,
        operation=CREDENTIAL_OPERATION,
    )
    def guard() -> None:
        assert_dispatched_invocation(child, harness.store)
    parent_scope = CapturedInvocationScopeV4(
        parent, lambda: assert_dispatched_invocation(parent, harness.store)
    )
    invocation = SimpleNamespace(
        envelope=child, parent_invocation=parent_scope, assert_current=guard
    )
    factory = CredentialManagementHostFactoryV4(readonly=False)
    provider_binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id, implementation_digest=_digest("hook")
        ),
        operation=SimpleNamespace(
            contract_id=factory.contract_id,
            operation_id=factory.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value=manager.principal_id),
        artifact=SimpleNamespace(digest=_digest("artifact")),
    )
    context = SimpleNamespace(
        user_data_root=tmp_path / "owner",
        profile_id="profile-1",
        provider_bindings=(provider_binding,),
        domain_ids={
            (
                factory.contract_id,
                factory.operation_id,
                manager.principal_id,
            ): manager_domain.domain_id
        },
        generation_budget_port=owner,
    )
    contribution = factory.capture(context).contributions[0]
    return SimpleNamespace(
        owner=owner,
        material=material,
        binding=binding,
        harness=harness,
        registry=registry,
        request=request,
        plan=plan,
        presentation=presentation,
        invocation=invocation,
        credential_payload=credential_payload,
        contribution=contribution,
        root=tmp_path / "owner",
    )


def test_native_approval_create_hook_enrolls_and_restart_restores(setup):
    class Client:
        def invoke(self, contract, operation, payload):
            assert contract == CREDENTIAL_CONTRACT and operation == CREDENTIAL_OPERATION
            return setup.contribution.invoke(operation, payload, setup.invocation)

    result = execute_configuration(
        setup.registry,
        Client(),
        {"request": setup.request, "plan": setup.plan},
        consumer_pack_id="rumi_provider_adapters_pack",
        budget_port=setup.owner,
    )
    assert result["configured"] is True
    restored = capture_native_generation_budget(
        binding=CredentialMaterialStoreBinding(setup.material, KEY_VERSION),
        authority_store=setup.harness.store,
        user_data_root=setup.root,
        profile_id="profile-1",
    )
    assert (
        restored.generation_admission.credential_budget_binding
        == setup.owner.credential_budget_binding
    )
    assert "3" in setup.presentation["detail"]
    assert "google/gemma-4-31b-it:free" in setup.presentation["detail"]
    assert "2048" in setup.presentation["detail"]
    assert setup.request["key_value"] not in str(setup.presentation)
    with pytest.raises(CredentialTransportDenied):
        prepare_configuration(
            setup.registry, setup.request, budget_port=restored.generation_admission
        )


def test_priced_native_approval_enrolls_exact_review_and_restores(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    template = replace(
        owner_startup_generation_budget_template(),
        profile_id="profile-1",
        connection_id="provider.main",
    )
    approved = setup.__wrapped__(tmp_path, monkeypatch, template)
    detail = approved.presentation["detail"]
    assert "stepfun/step-5-preview" in detail
    assert "$1/$2.7" in detail and "per million tokens" in detail
    assert "3" in detail and "2048" in detail and "fallback disabled" in detail
    assert "Reasoning effort: low/medium/high" in detail
    summary = approved.presentation["summary"]
    assert "$2 total, including prior spend" in summary
    assert "has not verified it" in summary
    assert "does not maintain a USD spending ledger" in summary
    assert len(detail) <= 512 and len(summary) <= 512
    assert approved.request["key_value"] not in str(approved.presentation)
    approved.contribution.invoke(
        CREDENTIAL_OPERATION,
        approved.credential_payload,
        approved.invocation,
    )
    restored = capture_native_generation_budget(
        binding=CredentialMaterialStoreBinding(approved.material, KEY_VERSION),
        authority_store=approved.harness.store,
        user_data_root=approved.root,
        profile_id="profile-1",
    )
    assert restored.generation_admission.template == template
    assert (
        restored.generation_admission.credential_budget_binding
        == approved.owner.credential_budget_binding
    )
    with pytest.raises(CredentialTransportDenied):
        prepare_configuration(
            approved.registry,
            approved.request,
            budget_port=restored.generation_admission,
        )


def test_priced_startup_cannot_amend_an_enrolled_legacy_credential(setup) -> None:
    setup.contribution.invoke(
        CREDENTIAL_OPERATION,
        setup.credential_payload,
        setup.invocation,
    )
    primary = setup.harness.store.list_host_pending_effects()
    witness = setup.owner._witness.list_host_pending_effects()
    marker = setup.owner.credential_budget_binding
    priced = replace(
        owner_startup_generation_budget_template(),
        profile_id="profile-1",
        connection_id="provider.main",
    )
    with pytest.raises(CredentialTransportDenied):
        NativeGenerationBudgetOwner(
            template=priced,
            authority_store=setup.harness.store,
            witness_store=setup.owner._witness,
            material_store=setup.material,
            key_version=KEY_VERSION,
        )
    assert setup.harness.store.list_host_pending_effects() == primary
    assert setup.owner._witness.list_host_pending_effects() == witness
    assert setup.owner.credential_budget_binding == marker
    assert setup.material.list(profile_id="profile-1")["count"] == 1


def test_legacy_template_serialization_and_effect_identity_are_unchanged() -> None:
    template = NativeGenerationBudgetTemplate("defaults", "provider.openrouter.main")
    assert template.to_dict() == {
        "profile_id": "defaults",
        "connection_id": "provider.openrouter.main",
        "consumer_pack_id": "rumi_provider_adapters_pack",
        "model": "google/gemma-4-31b-it:free",
        "max_requests": 3,
        "max_output_tokens": 2048,
    }
    assert template.effect_id == ("generation-budget-operator:" + canonical_digest("defaults")[7:])


@pytest.mark.parametrize(
    "change", ["no_parent", "key_substitution", "plan_override", "grant_revoked"]
)
def test_native_create_hook_rejects_unapproved_changes(setup, change):
    invocation = setup.invocation
    payload = dict(setup.credential_payload)
    if change == "no_parent":
        invocation = SimpleNamespace(**{**vars(invocation), "parent_invocation": None})
    elif change == "key_substitution":
        payload["secret_material"] = {"api_key": "different-synthetic-key"}
    elif change == "plan_override":
        parent = invocation.parent_invocation.envelope
        plan = {
            **parent.payload["plan"],
            "generation_budget": {
                **parent.payload["plan"]["generation_budget"],
                "max_requests": 4,
            },
        }
        invocation = SimpleNamespace(
            **{
                **vars(invocation),
                "parent_invocation": CapturedInvocationScopeV4(
                    replace(parent, payload={**parent.payload, "plan": plan}),
                    invocation.parent_invocation.assert_current,
                ),
            }
        )
    else:
        parent = invocation.parent_invocation.envelope
        lease, _ = setup.harness.store.inspect_lease_token(
            parent.lease.token.decode("ascii")
        )
        setup.harness.store.revoke(
            target_kind="grant", target_id=lease.grant_id, reason="test"
        )
    with pytest.raises((CredentialTransportDenied, PermissionError)):
        setup.contribution.invoke(CREDENTIAL_OPERATION, payload, invocation)
    assert setup.material.list(profile_id="profile-1")["count"] == 0


def test_renderer_budget_flags_do_not_enter_native_configuration(setup):
    with pytest.raises(ValueError):
        prepare_configuration(
            setup.registry,
            {**setup.request, "max_requests": 999},
            budget_port=setup.owner,
        )
    with pytest.raises(PermissionError):
        setup.contribution.invoke(
            CREDENTIAL_OPERATION,
            {**setup.credential_payload, "approved": True},
            setup.invocation,
        )


def native_transport_world(
    setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Build Native-approved material with the restored production send guard."""
    from tests import test_authority_v4_lifecycle as lifecycle_tests
    from tests.test_generation_budget import FUNCTION, OPERATION, CONTRACT

    setup.contribution.invoke(
        CREDENTIAL_OPERATION,
        setup.credential_payload,
        setup.invocation,
    )
    configured_principal = lifecycle_tests._principal

    def generation_principal(seed, *, operation="invoke"):
        value = configured_principal(seed, operation=operation)
        return (
            replace(value, function_id=FUNCTION, operation_id=OPERATION)
            if seed == "b"
            else value
        )

    monkeypatch.setattr(lifecycle_tests, "_principal", generation_principal)
    send_harness = lifecycle_tests._Harness(tmp_path / "send-authority", max_uses=100)
    restored = capture_native_generation_budget(
        binding=CredentialMaterialStoreBinding(setup.material, KEY_VERSION),
        authority_store=setup.harness.store,
        user_data_root=setup.root,
        profile_id="profile-1",
    )
    world = {
        "harness": send_harness,
        "credential": setup.material,
        "policy": restored.generation_admission._admission().policy,
        "guard": restored.generation_admission,
        "calls": [],
        "count": 0,
        "is_stream": False,
        "contract": CONTRACT,
        "operation": OPERATION,
    }
    return world


def test_native_registered_credential_reaches_three_post_transport_cap(
    setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Native-bound material and restored Host guard constrain actual POSTs."""
    from tests.test_generation_budget import transport

    world = native_transport_world(setup, tmp_path, monkeypatch)
    for _ in range(3):
        owner, arguments = transport(world)
        owner.post_json(**arguments)
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied) as denied:
        owner.post_json(**arguments)
    assert denied.value.code == "generation_budget_exhausted"
    assert len(world["calls"]) == 3


@pytest.mark.parametrize("explicit_template", [False, True])
def test_missing_policy_cannot_reenroll_while_bounded_material_remains(
    setup,
    explicit_template,
):
    """Existing authenticated material prevents recreated policy/grant resets."""
    import sqlite3

    setup.contribution.invoke(
        CREDENTIAL_OPERATION,
        setup.credential_payload,
        setup.invocation,
    )
    owner = setup.owner
    for store in (owner._authority, owner._witness):
        with sqlite3.connect(store.path) as connection:
            connection.execute("DELETE FROM host_pending_effects")
    template = owner.template if explicit_template else None
    with pytest.raises(CredentialTransportDenied):
        capture_native_generation_budget(
            binding=CredentialMaterialStoreBinding(setup.material, KEY_VERSION),
            authority_store=setup.harness.store,
            user_data_root=setup.root,
            profile_id="profile-1",
            template=template,
        )


def test_deleted_active_pointers_do_not_allow_new_registration(setup):
    """Deleting both pointers cannot hide the material's required marker."""
    import sqlite3

    setup.contribution.invoke(
        CREDENTIAL_OPERATION,
        setup.credential_payload,
        setup.invocation,
    )
    owner = setup.owner
    for store in (owner._authority, owner._witness):
        with sqlite3.connect(store.path) as connection:
            connection.execute(
                "DELETE FROM host_pending_effects WHERE effect_id = ?",
                (owner._active_id,),
            )
    with pytest.raises(CredentialTransportDenied):
        prepare_configuration(setup.registry, setup.request, budget_port=owner)
    assert setup.material.list(profile_id="profile-1")["count"] == 1


def test_legacy_native_credential_hooks_keep_existing_validation(tmp_path: Path):
    """The optional owner port preserves unrelated encrypted management."""
    from tests import test_credential_management_host as existing

    existing.test_captured_credential_create_status_revoke_use_encrypted_owner(
        tmp_path,
    )
    existing.test_status_cannot_create_credentials(tmp_path / "readonly")


@pytest.mark.parametrize("boundary", ["budget", "active_primary", "active_witness"])
def test_revocation_during_native_enrollment_does_not_create_material(
    setup,
    monkeypatch: pytest.MonkeyPatch,
    boundary,
):
    """Each persistent boundary rechecks the parent Native and child authority."""
    owner = setup.owner
    parent = setup.invocation.parent_invocation.envelope
    lease, _ = setup.harness.store.inspect_lease_token(
        parent.lease.token.decode("ascii")
    )
    store = owner._witness if boundary == "active_witness" else owner._authority
    create = store.create_host_pending_effect

    def revoke_after_write(effect_id, payload):
        result = create(effect_id, payload)
        if (boundary == "budget" and effect_id.startswith("generation-budget:")) or (
            boundary != "budget" and effect_id == owner._active_id
        ):
            setup.harness.store.revoke(
                target_kind="grant", target_id=lease.grant_id, reason="test boundary"
            )
        return result

    monkeypatch.setattr(store, "create_host_pending_effect", revoke_after_write)
    with pytest.raises((CredentialTransportDenied, PermissionError)):
        setup.contribution.invoke(
            CREDENTIAL_OPERATION,
            setup.credential_payload,
            setup.invocation,
        )
    assert setup.material.list(profile_id="profile-1")["count"] == 0


def test_native_missing_active_pointer_keeps_terminal_budget_code(
    setup,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Host policy restore denial cannot become retryable provider-unavailable."""
    import sqlite3
    from tests.test_generation_budget import transport, CONTRACT, ENDPOINT
    from core_runtime.global_contract_dispatch import (
        GlobalContractClient,
        GlobalContractInvocationError,
    )
    from ecosystem.rumi_provider_adapters_pack.runtime import adapter
    from ecosystem.rumi_ai_pipeline_pack.runtime.pipeline import (
        create_failover_operation,
    )

    world = native_transport_world(setup, tmp_path, monkeypatch)
    with sqlite3.connect(setup.owner._authority.path) as connection:
        connection.execute(
            "DELETE FROM host_pending_effects WHERE effect_id = ?",
            (setup.owner._active_id,),
        )
    owner, arguments = transport(world)
    with pytest.raises(CredentialTransportDenied) as denied:
        owner.post_json(**arguments)
    assert denied.value.code == "generation_budget_invalid"
    owner, arguments = transport(world)
    client = GlobalContractClient(
        object(),
        frozenset({CONTRACT}),
        world["policy"].consumer_pack_id,
        host_credential_transport=owner,
    )
    with pytest.raises(GlobalContractInvocationError) as denied:
        adapter._post(
            client,
            ENDPOINT,
            {},
            arguments["body"],
            {"deadline": arguments["deadline"]},
            connection={"provider_instance_id": world["policy"].connection_id},
            credential_handle=arguments["credential_handle"],
            credential_scope="ai.generate",
            credential_scheme="bearer",
        )
    assert denied.value.code == "generation_budget_denied"
    decision = create_failover_operation(None)(
        "decide",
        {
            "error_code": denied.value.code,
            "allow_failover": True,
            "idempotency_key": "test",
            "attempt": 1,
            "candidate_count": 8,
            "deadline": 2000,
            "decision_time": 1000,
        },
    )
    assert not decision["allowed"]
    assert not decision["checks"]["retryable_error"]
    assert world["calls"] == []
