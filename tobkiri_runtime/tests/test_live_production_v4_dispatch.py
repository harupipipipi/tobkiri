"""Clean-home proof for the sole ProductionRuntimeV4/RequestBroker path."""

from __future__ import annotations

import time
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityScope,
    AuthorityStore,
    FunctionPrincipal,
    authority_digest,
)
from core_runtime.global_contract_dispatch import GlobalContractInvocationError
from core_runtime.bootstrap.production_v4 import capture_production_dispatch
from core_runtime.packvm_lifecycle_v4 import PackVMLifecycleV4
from core_runtime import credential_transport as credential_transport_module
from core_runtime.credential_transport import CredentialMaterialStoreBinding
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from ecosystem.defaultspack.domain.runtime_v4 import ProfileResolutionDenied
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from ecosystem.rumi_credential_broker_pack.runtime.service import (
    CredentialBrokerService,
)
from ecosystem.rumi_credential_broker_pack.runtime.store import (
    CredentialBrokerStore,
    KEY_VERSION,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.effects import ProviderOutcome
from tobkiri_host.errors import AuthorizationError
from tobkiri_host.models import (
    ExecutionKind,
    InvocationFrame,
    OpaqueAuthorityRef,
    RuntimeEvidence,
)
from tobkiri_host.ports import FinalAuthorizationQuery
from tobkiri_host.runtime import V4DispatchSession
from tobkiri_protocol.canonical import canonical_digest


def _bundle_root() -> Path:
    from tests.conformance_support.packaged_profile import packaged_profile_bundle_root

    return packaged_profile_bundle_root()


def _digest(seed: str) -> str:
    return authority_digest({"seed": seed})


def _credential_store_factory(*, user_data_root: Path) -> CredentialMaterialStoreBinding:
    return CredentialMaterialStoreBinding(
        store=CredentialBrokerStore(user_data_root=user_data_root),
        key_version=KEY_VERSION,
    )


class _CapturedBackend:
    def __init__(self, backend_digest: str) -> None:
        self.status = BackendStatus(
            backend_id="tobkiri.python-pack-v4",
            execution_kind=ExecutionKind.PACK_VM,
            platform="any",
            backend_digest=backend_digest,
            production_enabled=True,
            conformance_only=False,
            satisfied_gates=REQUIRED_PRODUCTION_GATES,
        )
        self.target_domain_id = ""
        self.target_executable_digest = ""
        self.artifact_resolver = None
        self.target_domain_resolver = None
        self.capability_bridge = None

    def bind_artifact_resolver(self, resolver) -> None:
        assert self.artifact_resolver is None
        self.artifact_resolver = resolver

    def bind_target_domain_resolver(self, resolver) -> None:
        assert self.target_domain_resolver is None
        self.target_domain_resolver = resolver

    def bind_capability_bridge(self, callback) -> None:
        assert self.capability_bridge is None
        self.capability_bridge = callback

    def materialize(self, binding, reservation_id: str) -> RuntimeEvidence:
        assert reservation_id
        assert binding.variant.backend == self.status.backend_id
        assert self.artifact_resolver is not None
        assert self.target_domain_resolver is not None
        artifact = self.artifact_resolver(binding)
        assert artifact.artifact_digest == binding.artifact.digest
        assert artifact.implementation_digest == binding.function.implementation_digest
        self.target_domain_id = self.target_domain_resolver(binding)
        return RuntimeEvidence(
            domain_ref=OpaqueAuthorityRef(self.target_domain_id),
            executable_digest=self.target_executable_digest,
            backend_digest=self.status.backend_digest,
            authenticated_channel=True,
            nonce_fresh=True,
        )

    def invoke(self, request: object) -> ProviderOutcome:
        assert request is not None
        return ProviderOutcome({"ok": True})

    def cancel(self, request_id: str) -> None:
        assert request_id

    def terminate(self, domain_id: str) -> None:
        assert domain_id


class _ProviderResponse:
    def __enter__(self) -> "_ProviderResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, amount: int | None = None) -> bytes:
        value = json.dumps(
            {
                "choices": [
                    {
                        "message": {"content": "production-ok"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {},
            }
        ).encode("utf-8")
        return value[:amount]


def test_production_dispatch_executes_credentialed_provider_request(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise registry lookup and one envelope-bound transport end to end."""
    user_data = tmp_path / "provider-production"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    credential_service = CredentialBrokerService(user_data_root=user_data)
    credential = credential_service.invoke(
        "create",
        {
            "secret_material": {"api_key": "production-secret-sentinel"},
            "profile_id": "defaults",
            "consumer_pack_id": "rumi_provider_adapters_pack",
            "provider_instance_id": "provider.production-test",
            "scopes": ["ai.generate"],
        },
    )
    registry = ProviderRegistry("defaults", user_data_root=user_data)
    registry.save(
        {
            "provider_instance_id": "provider.production-test",
            "adapter_id": "openai-compatible",
            "credential_handle": credential["handle"],
            "endpoint": "https://provider.example/v1",
            "enabled": True,
        },
        expected_revision=0,
    )
    observed: list[tuple[str | None, float]] = []

    def open_request(request, *, timeout: float) -> _ProviderResponse:
        observed.append((request.headers.get("Authorization"), timeout))
        return _ProviderResponse()

    monkeypatch.setattr(
        credential_transport_module,
        "_open_pinned_request",
        open_request,
    )
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=AuthorityStore(user_data / "authority" / "v4.sqlite3"),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        credential_store_factory=_credential_store_factory,
    )
    try:
        result = session.invoke(
            "tobkiri.service.ai.provider.generate.v1",
            "rumi_provider_adapters_pack.provider-generate",
            {
                "_session_id": "session.panel.provider-production",
                "profile_id": "defaults",
                "provider_id": "production-test",
                "model_id": "production-test/model",
                "messages": [{"role": "user", "content": "hello"}],
                "deadline": time.time() + 30.0,
            },
        )
    finally:
        session.close()

    assert result["output"] == "production-ok"
    assert observed and observed[0][0] == "Bearer production-secret-sentinel"
    assert 0 < observed[0][1] <= 30.0
    assert "production-secret-sentinel" not in json.dumps(result)


def test_clean_home_broker_dispatches_then_revocation_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "clean-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.turn.v1"
    )
    target = FunctionPrincipal.from_dict(binding["function_principal"])
    backend = _CapturedBackend(_digest("backend"))
    store = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=store,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={target.principal_id: backend.status.backend_digest},
    )
    control = session.authority_control
    assert control is not None
    assert callable(backend.capability_bridge)
    session_id = "session.panel.clean-home"
    context = session.context_for("conversation.turn.v1", "complete", session_id)
    resolved_target = control._principals.resolve_principal(OpaqueAuthorityRef(target.principal_id))
    assert resolved_target == target
    caller_domain = store.get_domain(context.caller_domain_id)
    assert caller_domain is not None
    target_domain = store.get_domain(context.target_domain_id)
    assert target_domain is not None
    assert target_domain.principals == (target,)
    assert target_domain.activation_id == active.activation["activation_id"]
    assert target_domain.security_epoch == active.activation["security_epoch"]
    backend.target_executable_digest = target.function_implementation_digest

    scope = AuthorityScope.from_dict(
        session.effect_scope_for("conversation.turn.v1", "complete", {})
    )
    persisted = store.list_grants()
    current_context = session.context_for("conversation.turn.v1", "complete", session_id)
    current_caller = control._principals.resolve_principal(current_context.caller_principal)
    persisted_grant = next(item for item in persisted if item.target == target)
    assert persisted_grant.caller == current_caller
    assert persisted_grant.target == target
    assert persisted_grant.profile_id == current_context.profile_id
    assert persisted_grant.activation_id == current_context.activation_id
    assert persisted_grant.profile_authority_digest == current_context.profile_authority_digest
    assert persisted_grant.security_epoch == current_context.security_epoch
    assert persisted_grant.issued_at <= time.time()
    assert persisted_grant.expires_at is None
    assert persisted_grant.revoked is False
    assert not store.is_revoked("grant", persisted_grant.grant_id)
    assert scope.is_subset_of(persisted_grant.scope)
    approval = store.get_approval(persisted_grant.approval_id)
    assert approval is not None
    assert approval.caller == current_caller
    assert approval.target == target
    provider = next(item for item in store.list_provider_authorities() if item.provider == target)
    assert provider.execution_domain_id == target_domain.domain_id
    assert provider.execution_domain_identity_digest == target_domain.identity_digest
    translated, translated_scope = control._translate_query(
        current_context,
        OpaqueAuthorityRef(target.principal_id),
        _digest("request"),
        scope.to_dict(),
    )
    assert translated.target == target
    assert translated_scope.is_subset_of(persisted_grant.scope)
    selected = control._kernel._select_grant(
        context=translated,
        caller=current_caller,
        request_scope=translated_scope,
        now=time.time(),
    )
    assert selected == persisted_grant

    try:
        assert session.invoke(
            "conversation.turn.v1",
            "complete",
            {"_session_id": session_id, "messages": [{"role": "user"}]},
        ) == {"ok": True}
        assert store.grant_usage(persisted_grant.grant_id) == (0, 1)
        _revocation_id, revoked_grants = store.revoke_pack_approval(
            pack_id="defaultspack",
            approval_revision=_digest("pack-approval-revision"),
            profile_id="defaults",
            activation_id=active.activation["activation_id"],
            artifact_digest=target.parent_artifact_digest,
            reason="test exact Pack approval revoke",
        )
        assert revoked_grants == (persisted_grant.grant_id,)
        with pytest.raises(AuthorizationError, match="static authorization failed"):
            session.invoke(
                "conversation.turn.v1",
                "complete",
                {"_session_id": session_id, "messages": [{"role": "user"}]},
            )
        assert store.is_revoked("grant", persisted_grant.grant_id)
        assert store.audit_events()[-1]["event_type"] == "pack_approval_revoked"

        restarted = capture_production_dispatch(
            active,
            bundle_root=_bundle_root(),
            ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
            authority_store=store,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
            backends=BackendRegistry((_CapturedBackend(_digest("backend")),)),
            target_backend_digests={target.principal_id: _digest("backend")},
        )
        try:
            with pytest.raises(AuthorizationError, match="static authorization failed"):
                restarted.invoke(
                    "conversation.turn.v1",
                    "complete",
                    {
                        "_session_id": "session.panel.clean-home-restart",
                        "messages": [{"role": "user"}],
                    },
                )
        finally:
            restarted.broker.close()
    finally:
        session.broker.close()

    assert not (user_data / "settings" / "startup_profiles.json").exists()


def test_direct_vz_auth_failure_never_falls_back_to_path_lima_or_mints_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production exposes direct-VZ failure and leaves baseline authority absent."""

    import ecosystem.defaultspack.backend.sandbox.isolation.macos_vz_provisioner as vz

    user_data = tmp_path / "unavailable-packvm"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    invocation_marker = tmp_path / "limactl-invoked"
    limactl = tmp_path / "limactl"
    limactl.write_text(
        f"#!/bin/sh\ntouch '{invocation_marker}'\nexit 0\n",
        encoding="utf-8",
    )
    limactl.chmod(0o700)
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setattr(vz, "_default_state_dir", lambda: user_data / "packvm-vz")
    monkeypatch.setattr(vz, "_packaged_packvm_bundle_binding", lambda: None)
    lifecycle = PackVMLifecycleV4(vz.default_packvm_provisioner())
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.turn.v1" and item["operation_id"] == "complete"
    )
    target = FunctionPrincipal.from_dict(binding["function_principal"])
    store = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=store,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        packvm_provisioner=lifecycle,
    )
    try:
        readiness = lifecycle.readiness_snapshot()
        assert readiness["ready"] is False
        assert "PackVM VZ" in str(readiness["reason"])
        assert "backend_substrate" not in readiness
        assert not invocation_marker.exists()
        context = session.context_for(
            "conversation.turn.v1",
            "complete",
            "session.panel.unavailable-packvm",
        )
        assert store.get_domain(context.target_domain_id) is None
        assert all(item.target != target for item in store.list_grants())
        assert all(item.provider != target for item in store.list_provider_authorities())
        assert all(
            target.principal_id not in json.dumps(event, sort_keys=True)
            for event in store.audit_events()
        )
        metadata = session.provider_metadata("conversation.turn.v1")
        assert len(metadata) == 1
        assert "authenticated PackVM supervisor" in metadata[0]["backend_unavailable_reason"]
        assert not invocation_marker.exists()
    finally:
        session.broker.close()


def test_packvm_bridge_uses_only_the_captured_ai_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guest bridge cannot select a caller, target, plan, or session."""

    user_data = tmp_path / "bridge-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    conversation_binding = next(
        item
        for item in active.resolved.plan["bindings"]
        if item["contract_id"] == "conversation.turn.v1" and item["operation_id"] == "complete"
    )
    target = FunctionPrincipal.from_dict(conversation_binding["function_principal"])
    backend = _CapturedBackend(_digest("bridge-backend"))
    store = AuthorityStore(user_data / "authority" / "v4.sqlite3")
    session = capture_production_dispatch(
        active,
        bundle_root=_bundle_root(),
        ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
        authority_store=store,
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
        backends=BackendRegistry((backend,)),
        target_backend_digests={target.principal_id: backend.status.backend_digest},
    )
    try:
        assert callable(backend.capability_bridge)
        context = session.context_for(
            "conversation.turn.v1",
            "complete",
            "session.panel.bridge",
        )
        request = {
            "messages": [{"role": "user", "content": "hello"}],
            "requirements": {"request_surface": "defaultspack.conversation"},
        }
        bridge_request = {
            "kind": "tobkiri.packvm.bridge.request.v1",
            "protocol": "io.tobkiri.packvm.bridge.v1",
            "version": 1,
            "target": {
                "contract_id": "tobkiri.service.ai.generate.v1",
                "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
            },
            "request": request,
            "request_digest": canonical_digest(request),
            "continuation": {
                "kind": "tobkiri.packvm.continuation.v1",
                "protocol": "io.tobkiri.packvm.bridge.v1",
                "version": 1,
                "operation_id": "complete",
                "nonce": "a" * 48,
                "target": {
                    "contract_id": "tobkiri.service.ai.generate.v1",
                    "operation_id": "rumi_ai_gateway_pack.ai-gateway.generate",
                },
                "request_digest": canonical_digest(request),
            },
        }
        outer = SimpleNamespace(
            context=context,
            target_principal=OpaqueAuthorityRef(target.principal_id),
            target_domain=OpaqueAuthorityRef(context.target_domain_id),
            contract_id="conversation.turn.v1",
            operation_id="complete",
        )
        invocations: list[tuple[str, str, dict[str, object]]] = []

        def captured_ai_dispatch(
            self: V4DispatchSession,
            contract_id: str,
            operation_id: str,
            payload: dict[str, object],
            *,
            version_range: str | None = None,
        ) -> dict[str, object]:
            assert self is session
            assert version_range is None
            invocations.append((contract_id, operation_id, dict(payload)))
            return {"content": "verified completion"}

        monkeypatch.setattr(V4DispatchSession, "invoke", captured_ai_dispatch)
        result = backend.capability_bridge(outer, bridge_request)
        assert len(invocations) == 1
        contract_id, operation_id, payload = invocations[0]
        assert (contract_id, operation_id) == (
            "tobkiri.service.ai.generate.v1",
            "rumi_ai_gateway_pack.ai-gateway.generate",
        )
        assert payload["messages"] == request["messages"]
        assert payload["requirements"] == request["requirements"]
        assert set(payload) == {"messages", "requirements", "_session_id"}
        host_session_id = payload["_session_id"]
        assert isinstance(host_session_id, str)
        assert host_session_id.startswith(f"session.packvm-bridge.{context.request_id}.")
        assert bridge_request["continuation"]["nonce"] not in host_session_id
        assert result["result"] == {
            "status": "ok",
            "value": {"content": "verified completion"},
        }
        assert result["result_digest"] == canonical_digest(result["result"])
        with pytest.raises(AuthorityDenied, match="bridge request is invalid"):
            backend.capability_bridge(
                outer,
                {**bridge_request, "profile_id": "guest-controlled"},
            )

        def unavailable_ai_dispatch(
            self: V4DispatchSession,
            contract_id: str,
            operation_id: str,
            payload: dict[str, object],
            *,
            version_range: str | None = None,
        ) -> dict[str, object]:
            del self, contract_id, operation_id, payload, version_range
            raise GlobalContractInvocationError(
                "missing_provider",
                "no selected provider",
            )

        monkeypatch.setattr(V4DispatchSession, "invoke", unavailable_ai_dispatch)
        unavailable = backend.capability_bridge(
            outer,
            {
                **bridge_request,
                "continuation": {
                    **bridge_request["continuation"],
                    "nonce": "b" * 48,
                },
            },
        )
        assert unavailable["result"] == {
            "status": "error",
            "error": {
                "code": "PROVIDER_UNAVAILABLE",
                "message": "The verified AI capability is unavailable.",
            },
        }
    finally:
        session.close()


def test_pack_catalog_read_is_profile_bound_audited_and_restart_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults confirmation grants only the selected catalog read edge."""

    user_data = tmp_path / "catalog-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    with pytest.raises(ProfileResolutionDenied, match="confirmation"):
        capture_default_profile()

    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    store = AuthorityStore(user_data / "authority" / "v4.sqlite3")

    def capture():
        return capture_production_dispatch(
            capture_default_profile(),
            bundle_root=_bundle_root(),
            ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
            authority_store=store,
            activation_snapshot_loader=defaultspack_activation_snapshot_loader,
            runtime_surface_factory=create_runtime_surface_services,
        )

    session = capture()
    providers = session.provider_metadata("tobkiri.host.pack-control.v4")
    assert {provider["operation_id"] for provider in providers} == {
        "approval.approve",
        "approval.candidate",
        "approval.revoke",
        "catalog.read",
        "dashboard.read",
        "pack.disable",
        "pack.enable",
        "pack.install",
        "pack.status",
        "profile.reload",
        "runtime.restart",
    }
    assert {provider["provider_id"] for provider in providers} == {"tobkiri.host.pack-control"}
    result = session.invoke(
        "tobkiri.host.pack-control.v4",
        "catalog.read",
        {"_session_id": "session.panel.first-start"},
    )
    assert result["count"] == 140
    assert result["profile_id"] == "defaults"
    assert result["plan_digest"] == active.resolved.plan["plan_digest"]
    assert [event["event_state"] for event in store.audit_events()][-3:] == [
        "reserved",
        "dispatched",
        "committed",
    ]

    context = session.context_for(
        "tobkiri.host.pack-control.v4",
        "catalog.read",
        "session.panel.first-start",
    )
    frame = InvocationFrame(
        contract_id="tobkiri.host.pack-control.v4",
        version_range=">=1,<2",
        operation_id="catalog.read",
        payload={},
    )
    scope = session.effect_scope_for("tobkiri.host.pack-control.v4", "catalog.read", {})
    binding = session.broker._catalog.resolve(
        "tobkiri.host.pack-control.v4", "catalog.read", ">=1,<2"
    )
    replay_request_digest = _digest("catalog-lease-replay")
    lease = session.authority_control.authorize_and_issue_lease(
        FinalAuthorizationQuery(
            context=context,
            target_principal=binding.principal_ref,
            request_digest=replay_request_digest,
            effect_scope=scope,
            evidence=RuntimeEvidence(
                domain_ref=OpaqueAuthorityRef(context.target_domain_id),
                executable_digest=binding.function.implementation_digest,
                backend_digest=context.target_backend_digest,
                authenticated_channel=True,
                nonce_fresh=True,
            ),
        )
    )
    reservation = session.authority_control.reserve_effect(context, binding, replay_request_digest)
    session.authority_control.recheck_effect_boundary(context, binding.principal_ref, lease)
    with pytest.raises(AuthorityDenied, match="already consumed"):
        session.authority_control.recheck_effect_boundary(context, binding.principal_ref, lease)
    session.authority_control.fail_effect(reservation, "replay-test", False)

    for wrong_context in (
        replace(context, profile_id="wrong-profile"),
        replace(context, security_epoch=context.security_epoch + 1),
        replace(
            context,
            caller_principal=OpaqueAuthorityRef(authority_digest({"wrong": "caller"})),
        ),
        replace(context, target_domain_id="domain.provider.wrong"),
    ):
        with pytest.raises(AuthorizationError):
            session.broker.invoke(frame, wrong_context, effect_scope=scope)

    restarted = capture()
    assert (
        restarted.invoke(
            "tobkiri.host.pack-control.v4",
            "catalog.read",
            {"_session_id": "session.panel.restart"},
        )["count"]
        == 140
    )

    catalog_grant = next(
        item
        for item in store.list_grants()
        if item.target.function_id == "tobkiri.host.pack-control"
        and item.scope.dimensions["operation"] == ("catalog.read",)
    )
    restarted.authority_control.revoke(
        target_kind="grant",
        target_id=catalog_grant.grant_id,
        reason="test catalog revocation",
    )
    with pytest.raises(AuthorizationError):
        restarted.invoke(
            "tobkiri.host.pack-control.v4",
            "catalog.read",
            {"_session_id": "session.panel.restart"},
        )
    session.broker.close()
    restarted.broker.close()


def test_dispatch_rejects_authority_store_from_another_state_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "canonical-home"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    active = capture_default_profile(confirmation=prepare_default_profile_confirmation())
    alternate_store = AuthorityStore(tmp_path / "alternate-home" / "authority" / "v4.sqlite3")

    try:
        with pytest.raises(AuthorityDenied, match="not bound to the captured"):
            capture_production_dispatch(
                active,
                bundle_root=_bundle_root(),
                ecosystem_root=Path(__file__).resolve().parents[1] / "ecosystem",
                authority_store=alternate_store,
                activation_snapshot_loader=defaultspack_activation_snapshot_loader,
                runtime_surface_factory=create_runtime_surface_services,
            )
    finally:
        alternate_store.close()
