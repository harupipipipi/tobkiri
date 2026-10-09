"""Bind the existing Native Provider setup approval to a required budget.

Only the composition root supplies a template. The renderer submits its usual
connection fields; it cannot choose, clear, enlarge, or renew the Host policy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
import secrets
from typing import Any, NoReturn

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.authority.v4_models import authority_digest
from core_runtime.credential_transport import (
    CredentialMaterialStoreBinding,
    CredentialTransportDenied,
)
from core_runtime.generation_transport_budget import (
    GenerationBudgetPolicy,
    HostGenerationBudgetAdmission,
)
from core_runtime.invocation_scope_v4 import assert_dispatched_invocation

CONFIGURE_FUNCTION = "rumi_provider_registry_pack.provider-configure.execute"
CONFIGURE_OPERATION = "rumi_provider_registry_pack.provider-configure"
CONFIGURE_CONTRACT = "tobkiri.action.ai.provider.registry.manage.v1"
MANAGE_FUNCTION = "rumi_credential_broker_pack.credential-broker.manage"


@dataclass(frozen=True)
class NativeGenerationBudgetTemplate:
    """Host-selected restrictions, persisted before Native key registration."""

    profile_id: str
    connection_id: str
    consumer_pack_id: str = "rumi_provider_adapters_pack"
    model: str = "google/gemma-4-31b-it:free"
    max_requests: int = 3
    max_output_tokens: int = 2048

    def __post_init__(self) -> None:
        """Keep this isolated test policy exactly within the owner's limits."""
        if (
            type(self.profile_id) is not str
            or not self.profile_id
            or type(self.connection_id) is not str
            or not self.connection_id
            or self.consumer_pack_id != "rumi_provider_adapters_pack"
            or self.model != "google/gemma-4-31b-it:free"
            or type(self.max_requests) is not int
            or self.max_requests != 3
            or type(self.max_output_tokens) is not int
            or self.max_output_tokens != 2048
        ):
            raise ValueError("Native generation budget template is invalid")

    @property
    def effect_id(self) -> str:
        """Pin one operator policy per Profile, across new grant attempts."""
        return "generation-budget-operator:" + authority_digest(self.profile_id)[7:]


def owner_startup_generation_budget_template(
    requested: NativeGenerationBudgetTemplate | None = None,
) -> NativeGenerationBudgetTemplate | None:
    """Select the owner-approved isolated test policy from signed Source."""
    approved = NativeGenerationBudgetTemplate("defaults", "provider.openrouter.main")
    if requested is not None and (
        type(requested) is not NativeGenerationBudgetTemplate or requested != approved
    ):
        raise PermissionError("isolated generation budget policy is fixed")
    return approved


class NativeGenerationBudgetOwner:
    """Narrow Host port shared by Native prepare/create and provider transport."""

    def __init__(
        self,
        *,
        template: NativeGenerationBudgetTemplate,
        authority_store: AuthorityStore,
        witness_store: AuthorityStore,
        material_store: Any,
        key_version: str,
        install: bool = False,
    ) -> None:
        self.template = template
        self._authority = authority_store
        self._witness = witness_store
        self._material = material_store
        self._key_version = key_version
        self._active_id = template.effect_id + ":active"
        expected = {
            "effect_id": template.effect_id,
            "state": "required",
            "kind": "native_generation_budget_template_v1",
            "template": asdict(template),
        }
        left = authority_store.get_host_pending_effect(template.effect_id)
        right = witness_store.get_host_pending_effect(template.effect_id)
        if left is None and right is None and install:
            authority_store.create_host_pending_effect(template.effect_id, expected)
            witness_store.create_host_pending_effect(template.effect_id, expected)
        elif left is None or left != right or left[1] != expected:
            self._deny()

    @staticmethod
    def _deny() -> NoReturn:
        raise CredentialTransportDenied("generation_budget_invalid")

    def prepare(self, plan: Mapping[str, Any]) -> dict[str, Any]:
        """Attach a stable Host draft to the existing frozen setup plan."""
        required = getattr(self._material, "requires_generation_budget", None)
        if (
            (callable(required) and required(profile_id=self.template.profile_id))
            or plan.get("profile_id") != self.template.profile_id
            or plan.get("provider_instance_id") != self.template.connection_id
            or plan.get("adapter_id") != "openai-compatible"
            or plan.get("endpoint") != "https://openrouter.ai/api/v1"
            or self._authority.get_host_pending_effect(self._active_id) is not None
            or self._witness.get_host_pending_effect(self._active_id) is not None
        ):
            self._deny()
        draft_id = (
            self.template.effect_id + ":draft:" + authority_digest(dict(plan))[7:]
        )
        left = self._authority.get_host_pending_effect(draft_id)
        right = self._witness.get_host_pending_effect(draft_id)
        if left is None and right is None:
            policy = GenerationBudgetPolicy(
                enrollment_id=secrets.token_hex(16),
                profile_id=self.template.profile_id,
                consumer_pack_id=self.template.consumer_pack_id,
                connection_id=self.template.connection_id,
                credential_handle="credential:" + secrets.token_hex(16),
                credential_key_version=self._key_version,
                endpoint="https://openrouter.ai/api/v1/chat/completions",
                model=self.template.model,
                max_requests=self.template.max_requests,
                max_output_tokens=self.template.max_output_tokens,
            )
            payload = {
                "effect_id": draft_id,
                "state": "prepared",
                "plan": dict(plan),
                "policy": policy.to_dict(),
            }
            self._authority.create_host_pending_effect(draft_id, payload)
            self._witness.create_host_pending_effect(draft_id, payload)
        elif left is None or left != right or left[1].get("plan") != dict(plan):
            self._deny()
        else:
            policy = GenerationBudgetPolicy(**left[1]["policy"])
        return {**plan, "generation_budget": policy.to_dict()}

    def create(self, payload: Mapping[str, Any], invocation: Any) -> dict[str, Any]:
        """Require the exact live Native-approved configure ancestry at create."""
        invocation.assert_current()
        child = invocation.envelope
        parent_scope = invocation.parent_invocation
        if parent_scope is None:
            self._deny()
        parent_scope.assert_current()
        parent = parent_scope.envelope
        assert_dispatched_invocation(child, self._authority)
        assert_dispatched_invocation(parent, self._authority)
        parent_lease, _ = self._authority.inspect_lease_token(
            parent.lease.token.decode("ascii")
        )
        child_lease, _ = self._authority.inspect_lease_token(
            child.lease.token.decode("ascii")
        )
        if (
            parent.contract_id != CONFIGURE_CONTRACT
            or parent.operation_id != CONFIGURE_OPERATION
            or parent_lease.target.function_id != CONFIGURE_FUNCTION
            or child_lease.target.function_id != MANAGE_FUNCTION
            or child_lease.caller != parent_lease.target
            or parent.context.profile_id != self.template.profile_id
            or child.context.profile_id != parent.context.profile_id
            or child.context.activation_id != parent.context.activation_id
            or child.context.security_epoch != parent.context.security_epoch
        ):
            self._deny()
        request, plan = parent.payload.get("request"), parent.payload.get("plan")
        if not isinstance(request, Mapping) or not isinstance(plan, Mapping):
            self._deny()
        base = {key: value for key, value in plan.items() if key != "generation_budget"}
        if dict(plan) != self.prepare(base):
            self._deny()
        policy = GenerationBudgetPolicy(**plan["generation_budget"])
        if (
            payload.get("operation") != "create"
            or payload.get("profile_id") != policy.profile_id
            or payload.get("consumer_pack_id") != policy.consumer_pack_id
            or payload.get("provider_instance_id") != policy.connection_id
            or payload.get("secret_material") != {"api_key": request.get("key_value")}
            or payload.get("scopes") != ["ai.generate", "ai.stream"]
            or set(payload)
            != {
                "operation",
                "profile_id",
                "consumer_pack_id",
                "provider_instance_id",
                "secret_material",
                "scopes",
            }
        ):
            self._deny()
        grant = self._authority.get_grant(parent_lease.grant_id)
        approval = (
            self._authority.get_approval(grant.approval_id or "") if grant else None
        )
        native_request = None
        decision = None
        for candidate in self._authority.list_interactive_approval_requests():
            candidate_decision = self._authority.get_interactive_approval_decision(
                candidate.request_id
            )
            if (
                candidate_decision is not None
                and candidate_decision.grant_id == parent_lease.grant_id
            ):
                native_request, decision = candidate, candidate_decision
                break
        if (
            grant is None
            or approval is None
            or native_request is None
            or decision is None
            or decision.decision != "approved"
            or decision.request_snapshot_digest != native_request.digest
            or approval.snapshot_digest != native_request.digest
            or decision.approval_id != approval.approval_id
            or native_request.target != parent_lease.target
            or native_request.request_digest != parent.request_digest
            or native_request.base_scope.exact_request_digest != parent.request_digest
            or native_request.security_epoch != parent.context.security_epoch
            or grant.approval_id != approval.approval_id
            or approval.decision != "approved"
            or self._authority.is_revoked("grant", grant.grant_id)
        ):
            self._deny()
        admission = HostGenerationBudgetAdmission(
            policy=policy,
            authority_store=self._authority,
            witness_store=self._witness,
        )

        def assert_authorized() -> None:
            invocation.assert_current()
            parent_scope.assert_current()
            assert_dispatched_invocation(child, self._authority)
            assert_dispatched_invocation(parent, self._authority)

        assert_authorized()
        admission._persist_enrollment(
            approval_request_id=native_request.request_id,
            approval_digest=decision.digest,
        )
        pointer = {
            "effect_id": self._active_id,
            "state": "enrolled",
            "policy": policy.to_dict(),
            "approval_request_id": native_request.request_id,
        }
        assert_authorized()
        self._authority.create_host_pending_effect(self._active_id, pointer)
        assert_authorized()
        self._witness.create_host_pending_effect(self._active_id, pointer)
        # First material-store version is bounded. Failure after enrollment is
        # terminal and never authorizes another create or reservation refund.
        assert_authorized()
        return self._material.create(
            secret_material=payload["secret_material"],
            consumer_pack_id=policy.consumer_pack_id,
            provider_instance_id=policy.connection_id,
            scopes=["ai.generate", "ai.stream"],
            profile_id=policy.profile_id,
            credential_handle=policy.credential_handle,
            generation_budget_binding=admission.credential_budget_binding,
        )

    def _admission(self) -> HostGenerationBudgetAdmission:
        left = self._authority.get_host_pending_effect(self._active_id)
        right = self._witness.get_host_pending_effect(self._active_id)
        if left is None or left != right or left[1].get("state") != "enrolled":
            self._deny()
        policy = GenerationBudgetPolicy(**left[1]["policy"])
        return HostGenerationBudgetAdmission(
            policy=policy,
            authority_store=self._authority,
            witness_store=self._witness,
        )

    def close(self) -> None:
        """Release the independently owned witness on Host shutdown."""
        self._witness.close()

    @property
    def credential_budget_binding(self) -> Mapping[str, str]:
        """Restore the same required marker after activation/restart."""
        return self._admission().credential_budget_binding

    def reserve(self, **kwargs: Any) -> None:
        """Use the approved credential's common durable transport boundary."""
        self._admission().reserve(**kwargs)


def capture_native_generation_budget(
    *,
    binding: CredentialMaterialStoreBinding,
    authority_store: AuthorityStore,
    user_data_root: Path,
    profile_id: str,
    template: NativeGenerationBudgetTemplate | None = None,
) -> CredentialMaterialStoreBinding:
    """Install an explicit Host restriction or restore its persisted policy."""
    effect_id = "generation-budget-operator:" + authority_digest(profile_id)[7:]
    retained = authority_store.get_host_pending_effect(effect_id)
    witness_path = user_data_root / "credentials" / "generation-budget-witness.sqlite3"
    witness = AuthorityStore(witness_path) if witness_path.exists() else None
    secondary = (
        witness.get_host_pending_effect(effect_id) if witness is not None else None
    )
    marker_required = getattr(binding.store, "requires_generation_budget", None)
    if template is None:
        if retained is None:
            if secondary is not None or (
                callable(marker_required) and marker_required(profile_id=profile_id)
            ):
                raise CredentialTransportDenied("generation_budget_invalid")
            if witness is not None:
                witness.close()
            return binding
        template = NativeGenerationBudgetTemplate(**retained[1]["template"])
    if (
        type(template) is not NativeGenerationBudgetTemplate
        or template.profile_id != profile_id
    ):
        raise CredentialTransportDenied("generation_budget_invalid")
    if retained is None and (
        callable(marker_required) and marker_required(profile_id=profile_id)
    ):
        raise CredentialTransportDenied("generation_budget_invalid")
    witness = witness or AuthorityStore(witness_path)
    owner = NativeGenerationBudgetOwner(
        template=template,
        authority_store=authority_store,
        witness_store=witness,
        material_store=binding.store,
        key_version=binding.key_version,
        install=retained is None,
    )
    return CredentialMaterialStoreBinding(binding.store, binding.key_version, owner)
