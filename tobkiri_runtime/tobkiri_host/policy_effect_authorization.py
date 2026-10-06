"""Dedicated policy authorization for the retained native file-create effect.

The future effect references the real root native selection request. It never
creates a future interactive approval request, decision, or ApprovalRecord.
Exact operation authority is settled only at claim under the fresh Host resume
invocation, then coupled to the existing PendingEffect/Broker state machine.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from core_runtime.authority.v4 import AuthorityDenied, AuthorityScope, authority_digest

FILE_CREATE = ("tobkiri.service.file.create.v1", "rumi_default_tools_pack.file-create")
NATIVE_KIND = "interactive_native_v1"


@dataclass(frozen=True)
class PolicyDerivedEffectPlan:
    """Private native-selected policy admission, not a future native approval."""

    selection_id: str
    root_native_request_id: str
    native_boundary_digest: str
    root_capture_digest: str
    turn_id: str
    workspace_root: str
    prepared_request_digest: str

    def to_dict(self) -> dict[str, Any]:
        """Serialize the typed private admission inside authenticated encryption."""
        from dataclasses import asdict

        return asdict(self)


@dataclass(frozen=True)
class PolicyDerivedEffectAttestation:
    """Exact immutable encrypted effect inputs checked before every transition."""

    effect_id: str
    prepared: Any
    context: Any
    effect_scope: Mapping[str, Any]
    policy_plan: Mapping[str, Any]
    policy_derived_grant_id: str | None
    expires_at: float
    invocation_owner_id: str
    presentation_owner_principal_id: str
    presentation_owner_session_id: str


def policy_effect_attestation(record: Any) -> PolicyDerivedEffectAttestation:
    """Project exact retained record; no client flags or approval reconstruction."""
    return PolicyDerivedEffectAttestation(
        record.effect_id,
        record.prepared,
        record.context,
        record.effect_scope,
        record.policy_plan,
        record.policy_derived_grant_id,
        record.expires_at,
        record.invocation_owner_id,
        record.presentation_owner_principal_id,
        record.presentation_owner_session_id,
    )


class PolicyEffectAuthorizationAdapter:
    """Narrow actual adapter/root port for the existing guarded file effect."""

    def __init__(
        self,
        *,
        authority: Any,
        broker: Any,
        root_for: Callable[[str], Any],
        owned_file_guard: Callable[[Mapping[str, Any], Any], None],
    ) -> None:
        self.authority, self.broker, self.root_for = authority, broker, root_for
        self.owned_file_guard = owned_file_guard

    def _prepared(self, snapshot: Any, context: Any) -> Any:
        restored = self.broker.validate_prepared_snapshot(snapshot, context)
        if (
            restored.binding.operation.contract_id,
            restored.binding.operation.operation_id,
        ) != FILE_CREATE:
            raise AuthorityDenied("policy effect is outside finite native file-create route")
        return restored

    def capture_file_create_policy(
        self,
        *,
        command: Any,
        prepared: Any,
        execute_context: Any,
        effect_scope: Mapping[str, Any],
        owned_guard: Callable[[], None],
    ) -> PolicyDerivedEffectPlan | None:
        """Admit exact prepared effect under the already consumed native policy."""
        inherited = getattr(command, "policy_inheritance", None)
        if inherited is None or inherited.policy.mode == "ask":
            return None
        inherited.assert_current()
        inherited.policy.assert_current()
        owned_guard()
        root = self.root_for(inherited.policy.selection_id)
        if root.resolve_current() != inherited.policy.mode:
            raise AuthorityDenied("file effect selected policy changed")
        restored = self._prepared(prepared.to_snapshot(), execute_context)
        self.authority.check_policy_derived_operation(
            selection_id=root.selection_id,
            prepared_snapshot=restored.to_snapshot(),
            request_context=execute_context,
            binding=restored.binding,
            base_ceiling=effect_scope,
            workspace_root=inherited.policy.workspace_root,
            assert_current=owned_guard,
            parent_lease=inherited.parent_lease,
        )
        return PolicyDerivedEffectPlan(
            root.selection_id,
            root.command.context.request_id,
            authority_digest(root.capture()),
            inherited.policy.capture_digest,
            inherited.policy.turn_id,
            inherited.policy.workspace_root,
            prepared.request_digest,
        )

    def assert_effect_policy(self, attestation: PolicyDerivedEffectAttestation) -> None:
        """Repeat native/current/jail/prepared identity; no new authority created."""
        plan = PolicyDerivedEffectPlan(**dict(attestation.policy_plan))
        root = self.root_for(plan.selection_id)
        root.resolve_current()
        capture = root.capture()
        if (
            attestation.expires_at <= root.policy.clock()
            or plan.native_boundary_digest != authority_digest(capture)
            or plan.root_native_request_id != root.command.context.request_id
            or plan.workspace_root != capture["workspace"]
            or plan.turn_id != capture["turn"]
            or plan.prepared_request_digest != attestation.prepared.request_digest
        ):
            raise AuthorityDenied("retained policy effect native capture changed")
        restored = self._prepared(attestation.prepared, attestation.context)
        request = restored.normalized_payload.get("request")
        if not isinstance(request, Mapping):
            raise AuthorityDenied("native file-create retained request unavailable")
        self.owned_file_guard(request, attestation.context)
        scope = AuthorityScope.from_dict(attestation.effect_scope)
        if scope.exact_request_digest != attestation.prepared.request_digest:
            raise AuthorityDenied("policy effect exact scope changed")
        if attestation.policy_derived_grant_id:
            if scope.dimensions.get("policy_derivation_id") != (
                attestation.policy_derived_grant_id,
            ):
                raise AuthorityDenied("policy effect exact authority reference changed")
            self.authority.assert_policy_derived_operation_current(
                selection_id=plan.selection_id, grant_id=attestation.policy_derived_grant_id
            )

    def settle_effect_policy(
        self,
        attestation: PolicyDerivedEffectAttestation,
        inherited: Any,
        owned_guard: Callable[[], None],
    ) -> Any:
        """Freshly review and settle claim using the actual current resume parent."""
        self.assert_effect_policy(attestation)
        plan = PolicyDerivedEffectPlan(**dict(attestation.policy_plan))
        if (
            inherited is None
            or inherited.policy.selection_id != plan.selection_id
            or inherited.policy.capture_digest != plan.root_capture_digest
            or inherited.policy.native_boundary_digest != plan.native_boundary_digest
            or inherited.policy.turn_id != plan.turn_id
        ):
            raise AuthorityDenied("current policy effect inheritance unavailable")

        def guard() -> None:
            inherited.assert_current()
            inherited.policy.assert_current()
            owned_guard()
            self.assert_effect_policy(attestation)

        guard()
        restored = self._prepared(attestation.prepared, attestation.context)
        return self.authority.settle_policy_derived_operation(
            selection_id=plan.selection_id,
            prepared_snapshot=attestation.prepared,
            request_context=attestation.context,
            binding=restored.binding,
            base_ceiling=attestation.effect_scope,
            workspace_root=plan.workspace_root,
            assert_current=guard,
            parent_lease=inherited.parent_lease,
            pending_effect_id=attestation.effect_id,
        )
