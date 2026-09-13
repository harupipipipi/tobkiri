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


# The activation identity is deliberately part of this key.  A caller and
# target pair can be reused by a successor Profile or activation, but the
# authority captured for one generation must never be reused implicitly.
AuthorityEdgeKey = tuple[str, str, str, str, str, str]


def _authority_edge_key(
    *,
    profile_id: str,
    activation_id: str,
    caller: FunctionPrincipal,
    target: FunctionPrincipal,
    contract_id: str,
    operation_id: str,
) -> AuthorityEdgeKey:
    """Return the fully-qualified key for one signed operation edge."""

    return (
        profile_id,
        activation_id,
        caller.principal_id,
        target.principal_id,
        contract_id,
        operation_id,
    )


class _CapturedResolver(PrincipalReferenceResolver):
    def __init__(
        self,
        principals: Mapping[str, FunctionPrincipal],
        ceilings: Mapping[AuthorityEdgeKey, AuthorityCeilings],
        target_operation_keys: Mapping[str, tuple[str, str]],
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
        self._target_operation_keys = dict(target_operation_keys)
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
        operation_key = self._target_operation_keys.get(target.principal_id)
        if operation_key is None:
            raise AuthorityDenied("target operation is outside the captured ResolvedPlan")
        contract_id, operation_id = operation_key
        ceilings = self._ceilings.get(
            _authority_edge_key(
                profile_id=self._profile_id,
                activation_id=self._activation_id,
                caller=caller,
                target=target,
                contract_id=contract_id,
                operation_id=operation_id,
            )
        )
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
        authority_ceilings: Mapping[tuple[str, ...], AuthorityCeilings],
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
        principals_by_function: dict[str, list[FunctionPrincipal]] = {}
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
                    principals_by_function.setdefault(function.function_id, []).append(
                        principal
                    )

        # Shared operation names are valid only when every selected edge still
        # resolves to the same immutable target.  OperationCatalog has one
        # route per Contract/operation, so collapse byte-for-byte duplicate
        # routes after checking for conflicting identities.
        unique_routes: list[OperationRoute] = []
        route_by_operation: dict[tuple[str, str], OperationRoute] = {}
        for route in routes:
            operation_key = (route.contract_id, route.operation_id)
            previous = route_by_operation.get(operation_key)
            if previous is not None:
                previous_identity = (
                    previous.artifact_digest,
                    previous.function_id,
                    previous.variant_id,
                    previous.target_principal_ref.value,
                )
                current_identity = (
                    route.artifact_digest,
                    route.function_id,
                    route.variant_id,
                    route.target_principal_ref.value,
                )
                if previous_identity != current_identity:
                    raise ResolutionError(
                        "one Contract operation has conflicting selected targets"
                    )
                continue
            route_by_operation[operation_key] = route
            unique_routes.append(route)

        catalog = OperationCatalog(artifacts, unique_routes)
        expected_bindings: set[tuple[str, str, str, str, str]] = set()
        expected_route_counts: dict[tuple[str, str, str, str, str], int] = {}
        expected_authority_edges: set[AuthorityEdgeKey] = set()
        target_operation_keys: dict[str, tuple[str, str]] = {}
        seen_plan_edges: set[tuple[str, str, str]] = set()
        profile_id = str(checked_profile["profile_id"])
        activation_id = str(checked_activation["activation_id"])
        for item in checked_plan["bindings"]:
            principal = FunctionPrincipal.from_dict(item["function_principal"])
            if principals.get(principal.principal_id) != principal:
                raise ResolutionError("ResolvedPlan principal is outside verified inventory")
            caller_candidates = tuple(
                principals_by_function.get(str(item["caller_function_id"]), ())
            )
            if len(caller_candidates) != 1:
                raise ResolutionError(
                    "ResolvedPlan caller Function does not identify one principal"
                )
            operation_key = (str(item["contract_id"]), str(item["operation_id"]))
            plan_edge_key = (str(item["caller_function_id"]), *operation_key)
            if plan_edge_key in seen_plan_edges:
                raise ResolutionError("ResolvedPlan contains a duplicate operation edge")
            seen_plan_edges.add(plan_edge_key)
            previous_operation = target_operation_keys.get(principal.principal_id)
            if previous_operation is not None and previous_operation != operation_key:
                raise ResolutionError(
                    "one target principal is reused across Contract operations"
                )
            target_operation_keys[principal.principal_id] = operation_key
            binding_identity = (
                item["contract_id"],
                item["operation_id"],
                item["artifact_digest"],
                principal.function_id,
                principal.principal_id,
            )
            expected_bindings.add(binding_identity)
            # OperationCatalog intentionally has one route per Contract/op.
            # Multiple signed callers may share that exact target route; the
            # caller distinction is retained by ``expected_authority_edges``
            # and must not require duplicate catalog rows.
            expected_route_counts[binding_identity] = 1
            expected_authority_edges.add(
                _authority_edge_key(
                    profile_id=profile_id,
                    activation_id=activation_id,
                    caller=caller_candidates[0],
                    target=principal,
                    contract_id=operation_key[0],
                    operation_id=operation_key[1],
                )
            )
        actual_route_counts: dict[tuple[str, str, str, str, str], int] = {}
        for route in unique_routes:
            route_identity = (
                route.contract_id,
                route.operation_id,
                route.artifact_digest,
                route.function_id,
                route.target_principal_ref.value,
            )
            actual_route_counts[route_identity] = (
                actual_route_counts.get(route_identity, 0) + 1
            )
        actual_bindings = set(actual_route_counts)
        if actual_bindings != expected_bindings or actual_route_counts != expected_route_counts:
            raise ResolutionError("OperationCatalog routes must exactly equal ResolvedPlan")

        normalized_ceilings: dict[AuthorityEdgeKey, AuthorityCeilings] = {}
        for raw_key, ceilings in authority_ceilings.items():
            if len(raw_key) == 6:
                edge_key: AuthorityEdgeKey = (
                    str(raw_key[0]),
                    str(raw_key[1]),
                    str(raw_key[2]),
                    str(raw_key[3]),
                    str(raw_key[4]),
                    str(raw_key[5]),
                )
                if edge_key[:2] != (profile_id, activation_id):
                    raise ResolutionError(
                        "authority ceiling belongs to another Profile activation"
                    )
                if edge_key in normalized_ceilings and normalized_ceilings[edge_key] != ceilings:
                    raise ResolutionError("duplicate authority ceiling edge")
                normalized_ceilings[edge_key] = ceilings
                continue

            # Keep the old two-part conformance fixture readable while making
            # production capture unambiguously six-part.  A legacy key is
            # accepted only when it maps to exactly one signed edge; it is
            # never used as a runtime lookup key.
            if len(raw_key) != 2:
                raise ResolutionError("authority ceiling key is not fully qualified")
            matches = tuple(
                edge
                for edge in expected_authority_edges
                if edge[2] == str(raw_key[0]) and edge[3] == str(raw_key[1])
            )
            if len(matches) != 1:
                raise ResolutionError(
                    "authority ceilings include an ambiguous or outside-plan legacy key"
                )
            if matches[0] in normalized_ceilings and normalized_ceilings[matches[0]] != ceilings:
                raise ResolutionError("duplicate authority ceiling edge")
            normalized_ceilings[matches[0]] = ceilings

        if set(normalized_ceilings) != expected_authority_edges:
            raise ResolutionError(
                "authority ceilings must cover exactly the ResolvedPlan edges"
            )
        resolver = _CapturedResolver(
            principals,
            normalized_ceilings,
            target_operation_keys,
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


__all__ = ["AuthorityCeilings", "AuthorityEdgeKey", "HostV4Composition"]
