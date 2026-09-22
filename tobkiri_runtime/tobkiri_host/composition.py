"""Host-owned composition root for one immutable Pack v4 activation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from core_runtime.authority.v4 import (
    AuthorityBinding,
    AuthorityDenied,
    AuthorityKernel,
    AuthorityScope,
    AuthorityStore,
    FunctionPrincipal,
    InvocationContext,
)
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document

from .authority_v4 import AuthorityV4Adapter, PrincipalReferenceResolver
from .contracts import OperationCatalog, OperationRoute
from .errors import ResolutionError
from .models import OpaqueAuthorityRef, PackArtifact
from .tauri_roles import validate_production_tauri_roles


@dataclass(frozen=True)
class AuthorityCeilings:
    """Captured ceilings for one exact caller-to-provider plan edge."""

    caller_effect: AuthorityScope
    runtime_safety: AuthorityScope
    profile_admin: AuthorityScope


class _CapturedResolver(PrincipalReferenceResolver):
    def __init__(
        self,
        principals: Mapping[str, FunctionPrincipal],
        ceilings: Mapping[tuple[str, str], AuthorityCeilings],
        *,
        profile_id: str,
        activation_id: str,
        activation_digest: str,
        plan_digest: str,
        profile_authority_digest: str,
        fencing_token: int,
        security_epoch: int,
    ) -> None:
        self._principals = dict(principals)
        self._ceilings = dict(ceilings)
        self._profile_id = profile_id
        self._activation_id = activation_id
        self._activation_digest = activation_digest
        self._plan_digest = plan_digest
        self._profile_authority_digest = profile_authority_digest
        self._fencing_token = fencing_token
        self._security_epoch = security_epoch

    def resolve_principal(self, reference: OpaqueAuthorityRef) -> FunctionPrincipal:
        principal = self._principals.get(reference.value)
        if principal is None or principal.principal_id != reference.value:
            raise AuthorityDenied("principal is outside the captured activation")
        return principal

    def resolve_authority_binding(
        self,
        *,
        context: InvocationContext,
        caller: FunctionPrincipal,
        target: FunctionPrincipal,
    ) -> AuthorityBinding:
        ceilings = self._ceilings.get((caller.principal_id, target.principal_id))
        if ceilings is None:
            raise AuthorityDenied("operation edge is outside the captured ResolvedPlan")
        binding = AuthorityBinding(
            caller_effect_ceiling=ceilings.caller_effect,
            runtime_safety_ceiling=ceilings.runtime_safety,
            profile_admin_ceiling=ceilings.profile_admin,
            profile_id=self._profile_id,
            activation_id=self._activation_id,
            activation_digest=self._activation_digest,
            plan_digest=self._plan_digest,
            profile_authority_digest=self._profile_authority_digest,
            fencing_token=self._fencing_token,
            security_epoch=self._security_epoch,
        )
        if not binding.validates_context(context):
            raise AuthorityDenied("ResolvedPlan authority binding does not match")
        return binding


@dataclass(frozen=True)
class HostV4Composition:
    """Exact, restart-safe production snapshot used by every request surface."""

    catalog: OperationCatalog
    resolver: _CapturedResolver
    profile: Mapping[str, Any]
    lock: Mapping[str, Any]
    plan: Mapping[str, Any]
    activation: Mapping[str, Any]

    @classmethod
    def capture(
        cls,
        *,
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
        activation: Mapping[str, Any],
        artifacts: Sequence[PackArtifact],
        routes: Sequence[OperationRoute],
        authority_ceilings: Mapping[tuple[str, str], AuthorityCeilings],
        effective_artifacts: Mapping[str, str] | None = None,
    ) -> "HostV4Composition":
        """Capture a complete v4 graph, rejecting missing, stale, or extra input."""
        checked_profile = validate_document(profile, "profile")
        checked_lock = validate_document(lock, "profile_lock")
        checked_plan = validate_document(plan, "resolved_plan")
        checked_activation = validate_document(activation, "activation")
        validate_production_tauri_roles(checked_profile, checked_lock)
        cls._validate_record_graph(
            checked_profile, checked_lock, checked_plan, checked_activation
        )

        effective = {
            (item["identity"], item["artifact_digest"])
            for item in checked_lock["effective_set"]
        }
        supplied = {(item.pack_id, item.digest) for item in artifacts}
        verified_effective = (
            set(effective_artifacts.items())
            if effective_artifacts is not None
            else supplied
        )
        if (
            verified_effective != effective
            or len(verified_effective) != len(effective)
            or not supplied <= effective
            or len(supplied) != len(artifacts)
        ):
            raise ResolutionError(
                "verified artifact inventory must exactly equal ProfileLock effective_set"
            )

        principals: dict[str, FunctionPrincipal] = {}
        for artifact in artifacts:
            for function in artifact.functions:
                for operation in function.operations:
                    principal = FunctionPrincipal(
                        parent_artifact_digest=artifact.digest,
                        function_implementation_digest=function.implementation_digest,
                        function_id=function.function_id,
                        contract_revision_digest=operation.revision_digest,
                        operation_id=operation.operation_id,
                    )
                    if principal.principal_id in principals:
                        raise ResolutionError("duplicate Function principal in inventory")
                    principals[principal.principal_id] = principal

        catalog = OperationCatalog(artifacts, routes)
        expected_bindings: set[tuple[str, str, str, str, str]] = set()
        for item in checked_plan["bindings"]:
            principal = FunctionPrincipal.from_dict(item["function_principal"])
            if principals.get(principal.principal_id) != principal:
                raise ResolutionError("ResolvedPlan principal is outside verified inventory")
            expected_bindings.add(
                (
                    item["contract_id"],
                    item["operation_id"],
                    item["artifact_digest"],
                    principal.function_id,
                    principal.principal_id,
                )
            )
        actual_bindings = {
            (
                route.contract_id,
                route.operation_id,
                route.artifact_digest,
                route.function_id,
                route.target_principal_ref.value,
            )
            for route in routes
        }
        if actual_bindings != expected_bindings or len(actual_bindings) != len(routes):
            raise ResolutionError("OperationCatalog routes must exactly equal ResolvedPlan")

        unknown_edges = {
            edge
            for edge in authority_ceilings
            if edge[0] not in principals or edge[1] not in principals
        }
        target_ids = {item[-1] for item in expected_bindings}
        covered_targets = {target for _caller, target in authority_ceilings}
        if unknown_edges or covered_targets != target_ids:
            raise ResolutionError(
                "authority ceilings must cover exactly the ResolvedPlan targets"
            )
        resolver = _CapturedResolver(
            principals,
            authority_ceilings,
            profile_id=checked_profile["profile_id"],
            activation_id=checked_activation["activation_id"],
            activation_digest=canonical_digest(checked_activation),
            plan_digest=checked_plan["plan_digest"],
            profile_authority_digest=checked_activation[
                "profile_authority_snapshot_digest"
            ],
            fencing_token=checked_activation["fencing_token"],
            security_epoch=checked_activation["security_epoch"],
        )
        return cls(
            catalog=catalog,
            resolver=resolver,
            profile=checked_profile,
            lock=checked_lock,
            plan=checked_plan,
            activation=checked_activation,
        )

    def authority_adapter(
        self,
        store: AuthorityStore,
        *,
        terminate_domain: Any | None = None,
    ) -> AuthorityV4Adapter:
        """Construct the only production authority bridge for this snapshot."""
        kernel = AuthorityKernel(
            store,
            self.resolver,
            terminate_domain=terminate_domain,
        )
        return AuthorityV4Adapter(kernel, self.resolver)

    @staticmethod
    def _validate_record_graph(
        profile: Mapping[str, Any],
        lock: Mapping[str, Any],
        plan: Mapping[str, Any],
        activation: Mapping[str, Any],
    ) -> None:
        profile_revision = canonical_digest(profile)
        plan_digest = canonical_digest(
            {key: value for key, value in plan.items() if key != "plan_digest"}
        )
        lock_digest = canonical_digest(
            {key: value for key, value in lock.items() if key != "lock_digest"}
        )
        if profile["state"] != "resolved":
            raise ResolutionError("production composition requires a resolved Profile")
        if profile_revision != plan["profile_revision"] or profile_revision != lock["profile_revision"]:
            raise ResolutionError("Profile revision is stale")
        if plan_digest != plan["plan_digest"] or plan_digest != lock["plan_digest"]:
            raise ResolutionError("ResolvedPlan digest is stale")
        if lock_digest != lock["lock_digest"]:
            raise ResolutionError("ProfileLock digest is stale")
        pin_fields = (
            "pack_id",
            "artifact_digest",
            "executable_catalog_digest",
            "variant_id",
            "platform",
            "architecture",
            "runtime_abi",
            "backend",
            "execution_kind",
            "domain_kind",
        )
        plan_pins = [
            {
                field: binding[field]
                for field in pin_fields
            }
            for binding in plan["bindings"]
        ]
        plan_pin_keys = {tuple(pin[field] for field in pin_fields) for pin in plan_pins}
        lock_pins = lock.get("variant_pins", [])
        lock_pin_keys = {tuple(pin[field] for field in pin_fields) for pin in lock_pins}
        if lock_pin_keys != plan_pin_keys or len(lock_pin_keys) != len(lock_pins):
            raise ResolutionError("ProfileLock executable variant pins do not match ResolvedPlan")
        expected = (
            profile["profile_id"],
            plan["plan_digest"],
            profile["profile_authority_snapshot_digest"],
            plan["security_epoch"],
        )
        actual = (
            activation["profile_id"],
            activation["plan_digest"],
            activation["profile_authority_snapshot_digest"],
            activation["security_epoch"],
        )
        if activation["state"] != "active" or actual != expected:
            raise ResolutionError("ActivationRecord does not match the captured plan")


__all__ = ["AuthorityCeilings", "HostV4Composition"]
