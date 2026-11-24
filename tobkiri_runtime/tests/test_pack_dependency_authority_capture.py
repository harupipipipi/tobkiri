"""Capture exact optional dependency authority from one live root receipt."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import (
    AuthorityDenied,
    AuthorityStore,
    DomainBoundary,
    FunctionPrincipal,
    InvocationContext,
    LeaseState,
)
from core_runtime.bootstrap import production_v4
from core_runtime.bootstrap.profile_capture import (
    capture_default_profile,
    prepare_default_profile_confirmation,
)
from core_runtime.pack_control_v4 import (
    PACK_CONTROL_CONTRACT,
    PackControlDenied,
    capture_pack_control_session,
)
from ecosystem.defaultspack.defaultspack.runtime_composition import (
    defaultspack_activation_snapshot_loader,
)
from ecosystem.defaultspack.domain.runtime_surface_v4 import (
    create_runtime_surface_services,
)
from tests.conformance_support.packaged_profile import packaged_profile_bundle_root
from tobkiri_host.backends import (
    REQUIRED_PRODUCTION_GATES,
    BackendRegistry,
    BackendStatus,
)
from tobkiri_host.models import ExecutionKind
from tobkiri_protocol.canonical import canonical_digest


ROOT_PACK = "rumi_scheduler_tool_adapter_pack"
DEPENDENCY_PACKS = {"rumi_scheduler_runtime_pack", "rumi_schedule_store_pack"}
OTHER_ROOT_PACK = "rumi_media_inspect_service_pack"
RUNTIME_ROOT = Path(__file__).resolve().parents[1]


class _ReadyCaptureBackend:
    """Production-gated capture seam that never executes guest code."""

    status = BackendStatus(
        backend_id="tobkiri.python-pack-v4",
        execution_kind=ExecutionKind.PACK_VM,
        platform="any",
        backend_digest=canonical_digest({"test": "dependency-authority-capture"}),
        production_enabled=True,
        conformance_only=False,
        satisfied_gates=REQUIRED_PRODUCTION_GATES,
    )

    def supports(self, binding: Any) -> bool:
        return binding.variant.execution_kind is ExecutionKind.PACK_VM

    def bind_target_domain_resolver(self, resolver: Any) -> None:
        self.domain_resolver = resolver

    def bind_artifact_resolver(self, resolver: Any) -> None:
        self.artifact_resolver = resolver

    def bind_saved_capability_bridge(self, bridge: Any, preflight: Any) -> None:
        self.saved_bridge = bridge
        self.saved_preflight = preflight

    def bind_capability_bridge(self, bridge: Any) -> None:
        self.bridge = bridge

    def materialize(self, binding: Any, reservation_id: str) -> Any:
        raise AssertionError("capture-only backend must not materialize")

    def invoke(self, request: Any) -> Any:
        raise AssertionError("capture-only backend must not invoke")

    def cancel(self, request_id: str) -> None:
        pass

    def terminate(self, domain_id: str) -> None:
        pass


def _capture(active: Any, authority: AuthorityStore, backend: Any) -> Any:
    return production_v4.capture_production_dispatch(
        active,
        bundle_root=packaged_profile_bundle_root(),
        ecosystem_root=RUNTIME_ROOT / "ecosystem",
        authority_store=authority,
        backends=BackendRegistry((backend,)),
        activation_snapshot_loader=defaultspack_activation_snapshot_loader,
        runtime_surface_factory=create_runtime_surface_services,
    )


def test_scheduler_dependency_grants_bind_root_approval_and_revoke_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root-only approval covers six exact edges and fences stale capture."""
    user_data = tmp_path / "user-data"
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(user_data))
    capture_default_profile(confirmation=prepare_default_profile_confirmation())
    control = capture_pack_control_session(
        runtime_surface_factory=create_runtime_surface_services,
    )
    payload = {"pack_id": ROOT_PACK, "_session_id": "dependency-authority-test"}
    control.invoke(PACK_CONTROL_CONTRACT, "pack.install", payload)
    candidate = control.invoke(PACK_CONTROL_CONTRACT, "approval.candidate", payload)
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {**payload, "candidate_id": candidate["candidate_id"]},
    )
    control.invoke(PACK_CONTROL_CONTRACT, "pack.enable", payload)
    other_payload = {**payload, "pack_id": OTHER_ROOT_PACK}
    control.invoke(PACK_CONTROL_CONTRACT, "pack.install", other_payload)
    other_candidate = control.invoke(
        PACK_CONTROL_CONTRACT, "approval.candidate", other_payload,
    )
    control.invoke(
        PACK_CONTROL_CONTRACT,
        "approval.approve",
        {**other_payload, "candidate_id": other_candidate["candidate_id"]},
    )
    control.invoke(PACK_CONTROL_CONTRACT, "pack.enable", other_payload)
    active = capture_default_profile()
    captured_plan = canonical_digest(active.resolved.plan)
    approval_dir = user_data / "pack_control" / "approvals" / "defaults"
    receipt = json.loads((approval_dir / f"{ROOT_PACK}.json").read_text())
    assert all(
        not (approval_dir / f"{pack_id}.json").exists()
        for pack_id in DEPENDENCY_PACKS
    )
    callers: dict[str, set[FunctionPrincipal]] = {}
    for binding in active.resolved.plan["bindings"]:
        principal = FunctionPrincipal.from_dict(binding["function_principal"])
        callers.setdefault(principal.function_id, set()).add(principal)
    dependency_edges = [
        binding for binding in active.resolved.plan["bindings"]
        if binding["caller_function_id"].startswith(ROOT_PACK + ".")
        and binding["pack_id"] in DEPENDENCY_PACKS
    ]
    assert len(dependency_edges) == 6

    authority_path = user_data / "authority" / "v4.sqlite3"
    authority = AuthorityStore(authority_path)
    backend = _ReadyCaptureBackend()
    session = _capture(active, authority, backend)
    restarted = None
    try:
        grants = authority.list_grants()
        providers = {
            provider.record_id: provider
            for provider in authority.list_provider_authorities()
        }
        dependency_targets = set()
        dependency_grants = []
        for binding in dependency_edges:
            (caller,) = callers[binding["caller_function_id"]]
            target = FunctionPrincipal.from_dict(binding["function_principal"])
            dependency_targets.add(target.principal_id)
            resolved = session.broker._catalog.resolve_pinned(
                binding["contract_id"], binding["operation_id"],
            )
            assert resolved.variant.execution_kind is ExecutionKind.PACK_VM
            assert session.broker._backends.select(resolved) is backend
            exact = [
                grant for grant in grants
                if grant.activation_id == active.activation["activation_id"]
                and grant.caller == caller
                and grant.target == target
                and grant.scope.dimensions.get("contract")
                == (binding["contract_id"],)
                and grant.scope.dimensions.get("operation")
                == (binding["operation_id"],)
            ]
            assert len(exact) == 1
            grant = exact[0]
            dependency_grants.append(grant)
            approval = authority.get_approval(grant.approval_id)
            assert approval is not None
            assert approval.actor_id == "user.pack-approval"
            assert approval.caller == caller
            assert approval.target == target
            assert approval.snapshot_digest == canonical_digest({
                "ceremony": "defaults.activate",
                "activation_id": active.activation["activation_id"],
                "plan_digest": active.activation["plan_digest"],
                "profile_authority_snapshot_digest": active.activation[
                    "profile_authority_snapshot_digest"
                ],
                "security_epoch": active.activation["security_epoch"],
                "scope": grant.scope.to_dict(),
                "pack_approval_revision": receipt["approval_revision"],
            })
            provider = providers[grant.grant_id.replace("grant.", "provider.", 1)]
            assert provider.provider == target
            assert provider.scope == grant.scope
            domain = authority.get_domain(provider.execution_domain_id)
            assert domain is not None
            assert domain.boundary is DomainBoundary.DEDICATED_PROCESS
            assert target.principal_id in domain.principal_ids
            assert domain.activation_id == active.activation["activation_id"]
        session.assert_current()

        activation_row = authority.active_activation_reservation(
            active.activation["activation_id"],
        )
        assert activation_row is not None
        assert activation_row["plan_digest"] == active.activation["plan_digest"]
        assert activation_row["profile_authority_digest"] == active.activation[
            "profile_authority_snapshot_digest"
        ]
        assert activation_row["security_epoch"] == active.activation["security_epoch"]
        other_artifact = next(
            item["artifact_digest"] for item in active.resolved.lock["effective_set"]
            if item["identity"] == OTHER_ROOT_PACK
        )
        other_grants = [
            grant for grant in grants
            if grant.target.parent_artifact_digest == other_artifact
        ]
        assert other_grants
        leased_grant = next(
            grant for grant in dependency_grants
            if grant.caller.function_id == ROOT_PACK + ".tool-adapter.scheduler"
        )
        leased_provider = providers[
            leased_grant.grant_id.replace("grant.", "provider.", 1)
        ]
        leased_domain = authority.get_domain(leased_provider.execution_domain_id)
        assert leased_domain is not None
        caller_suffix = leased_grant.caller.principal_id.removeprefix("sha256:")[:24]
        context = InvocationContext(
            request_id="root-revoke-issued-dependency-lease",
            request_digest=canonical_digest({"test": "issued-dependency-lease"}),
            effect_digest=leased_grant.scope.digest,
            caller_session_id=(
                f"session.provider.pack_vm.{caller_suffix}."
                f"{active.activation['fencing_token']}"
            ),
            target=leased_grant.target,
            target_domain_id=leased_domain.domain_id,
            target_boot_epoch=leased_domain.boot_epoch,
            profile_id="defaults",
            activation_id=active.activation["activation_id"],
            activation_digest=canonical_digest(active.activation),
            plan_digest=active.activation["plan_digest"],
            profile_authority_digest=active.activation[
                "profile_authority_snapshot_digest"
            ],
            fencing_token=active.activation["fencing_token"],
            security_epoch=active.activation["security_epoch"],
        )
        kernel = session.authority_control._kernel
        issued = kernel.authorize(context, leased_grant.scope)
        assert authority.get_lease(issued.lease_id)[1] is LeaseState.ISSUED

        # Exercise the durable replay fence while leaving the historical Plan
        # intact. A revoked root must invalidate its existing capture and may
        # never mint fresh dependency authority from that immutable Plan.
        root_artifact = next(
            item["artifact_digest"] for item in active.resolved.lock["effective_set"]
            if item["identity"] == ROOT_PACK
        )
        _revocation_id, revoked_grants = authority.revoke_pack_approval(
            pack_id=ROOT_PACK,
            approval_revision=receipt["approval_revision"],
            profile_id="defaults",
            activation_id=active.activation["activation_id"],
            artifact_digest=root_artifact,
            reason="test root approval revocation",
        )
        assert {grant.grant_id for grant in dependency_grants}.issubset(
            revoked_grants,
        )
        assert all(
            authority.is_revoked("grant", grant.grant_id)
            for grant in dependency_grants
        )
        assert all(
            not authority.is_revoked("grant", grant.grant_id)
            for grant in other_grants
        )
        assert authority.get_lease(issued.lease_id)[1] is LeaseState.REVOKED
        with pytest.raises(AuthorityDenied, match="(?i)revok|not dispatchable"):
            kernel.dispatch(
                issued.lease_token,
                target_domain_id=leased_domain.domain_id,
                target_boot_epoch=leased_domain.boot_epoch,
                request_digest=context.request_digest,
            )
        with pytest.raises(PackControlDenied, match="approval_revoked"):
            session.assert_current()
        original_commit = production_v4._commit_plan_authority
        committed_targets: list[str] = []

        def track_commit(*args: Any, **kwargs: Any) -> None:
            committed_targets.append(kwargs["target"].principal_id)
            original_commit(*args, **kwargs)

        monkeypatch.setattr(production_v4, "_commit_plan_authority", track_commit)
        grant_ids_before = {grant.grant_id for grant in authority.list_grants()}
        restarted_authority = AuthorityStore(authority_path)
        restarted = _capture(active, restarted_authority, _ReadyCaptureBackend())
        assert not dependency_targets.intersection(committed_targets)
        assert grant_ids_before == {
            grant.grant_id for grant in restarted_authority.list_grants()
        }
        assert canonical_digest(active.resolved.plan) == captured_plan
    finally:
        if restarted is not None:
            restarted.close()
        session.close()
        control.close()
