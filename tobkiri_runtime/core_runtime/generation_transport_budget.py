"""Host-owned lifetime generation limit for one explicitly enrolled credential.

The Host injects this admission into its credential transport; no Pack payload
chooses whether it applies.  Two independently persisted authority snapshots
must agree before and after every reservation.  A partial update is terminal
fail-closed, never a reason to recreate, repair, refund, or retry a reservation.

This does not provide a hardware monotonic counter: coordinated rollback of
both complete Host stores is outside the guarantee of the existing storage.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
import time
import math
from typing import Any

from core_runtime.authority.v4 import AuthorityStore
from core_runtime.authority.v4_models import authority_digest
from core_runtime.credential_transport import (
    CredentialTransportBinding,
    CredentialTransportDenied,
)

ENROLL_FUNCTION = "tobkiri.host.generation-budget.enroll"
ENROLL_OPERATION = "enroll"
_GENERATION_ROUTES = frozenset(
    {
        (
            "tobkiri.service.ai.provider.generate.v1",
            "rumi_provider_adapters_pack.provider.compatibility.generate",
            "rumi_provider_adapters_pack.provider-generate",
        ),
        (
            "tobkiri.service.ai.provider.stream.v1",
            "rumi_provider_adapters_pack.provider.compatibility.stream",
            "rumi_provider_adapters_pack.provider-stream",
        ),
    }
)
_BODY_KEYS = frozenset(
    {
        "model",
        "messages",
        "tools",
        "tool_choice",
        "max_tokens",
        "temperature",
        "top_p",
        "stop",
        "seed",
        "stream",
        "stream_options",
        "parallel_tool_calls",
        "provider",
        "n",
    }
)


def _deny(code: str = "generation_budget_invalid") -> None:
    raise CredentialTransportDenied(code)


@dataclass(frozen=True)
class GenerationBudgetPolicy:
    """Exact Host-approved restriction; one enrollment never resets itself."""

    enrollment_id: str
    profile_id: str
    consumer_pack_id: str
    connection_id: str
    credential_handle: str
    credential_key_version: str
    endpoint: str
    model: str
    max_requests: int
    max_output_tokens: int

    def __post_init__(self) -> None:
        """Require finite ceilings and the selected free OpenRouter surface."""
        if (
            any(
                type(value) is not str
                or not value
                or len(value) > 512
                or any(c in value for c in "\x00\r\n")
                for name, value in asdict(self).items()
                if name not in {"max_requests", "max_output_tokens"}
            )
            or not self.credential_handle.startswith("credential:")
            or type(self.max_requests) is not int
            or not 1 <= self.max_requests <= 3
            or type(self.max_output_tokens) is not int
            or not 1 <= self.max_output_tokens <= 2048
            or self.endpoint != "https://openrouter.ai/api/v1/chat/completions"
            or not self.model.endswith(":free")
        ):
            raise ValueError("generation budget policy is invalid")

    @property
    def digest(self) -> str:
        """Return the exact policy digest shown in the approval snapshot."""
        return authority_digest(self.to_dict())

    @property
    def effect_id(self) -> str:
        """Return the immutable Host-only ledger identity."""
        return "generation-budget:" + self.enrollment_id

    def to_dict(self) -> dict[str, Any]:
        """Serialize only non-secret immutable enrollment data."""
        return asdict(self)


class HostGenerationBudgetAdmission:
    """Reserve before POST, over the entire approved credential lifetime."""

    def __init__(
        self,
        *,
        policy: GenerationBudgetPolicy,
        authority_store: AuthorityStore,
        witness_store: AuthorityStore,
    ) -> None:
        """Capture Host-owned policy and independent persistence owners."""
        if (
            authority_store.path == witness_store.path
            or authority_store.key_path == witness_store.key_path
        ):
            raise ValueError("generation budget requires independent stores")
        self.policy = policy
        self._authority = authority_store
        self._witness = witness_store

    @property
    def credential_budget_binding(self) -> Mapping[str, str]:
        """Require this exact enrollment whenever its material is resolved."""
        return {
            "enrollment_id": self.policy.enrollment_id,
            "policy_digest": self.policy.digest,
        }

    def enroll_after_interactive_approval(
        self,
        *,
        approval_request_id: str,
        now: float | None = None,
    ) -> None:
        """Enroll only after an exact, authenticated local operator decision.

        This is a Host-only lifecycle method, not a renderer or Pack tool.  The
        Host must create the interactive request for ENROLL_FUNCTION using the
        exact policy digest before exposing its native approval window.
        """
        instant = time.time() if now is None else now
        if type(instant) not in (int, float) or not math.isfinite(instant):
            _deny()
        try:
            request = self._authority.get_interactive_approval_request(
                approval_request_id,
            )
            decision = self._authority.get_interactive_approval_decision(
                approval_request_id,
            )
            if request is None or decision is None or decision.grant_id is None:
                _deny()
            approval = self._authority.get_approval(decision.approval_id or "")
            grant = self._authority.get_grant(decision.grant_id)
            if (
                request.target.function_id != ENROLL_FUNCTION
                or request.target.operation_id != ENROLL_OPERATION
                or request.profile_id != self.policy.profile_id
                or request.request_digest != self.policy.digest
                or request.base_scope.exact_request_digest != self.policy.digest
                or request.base_scope.capability != "host.generation-budget"
                or request.expires_at <= instant
                or request.security_epoch != self._authority.security_epoch
                or decision.decision != "approved"
                or decision.request_snapshot_digest != request.digest
                or decision.security_epoch != request.security_epoch
                or approval is None
                or grant is None
                or approval.decision != "approved"
                or approval.snapshot_digest != request.digest
                or approval.target != request.target
                or approval.profile_id != self.policy.profile_id
                or grant.approval_id != approval.approval_id
                or grant.target != request.target
                or grant.scope != request.base_scope
                or grant.profile_id != self.policy.profile_id
                or grant.revoked
                or self._authority.is_revoked("grant", grant.grant_id)
                or self._authority.is_revoked("profile", self.policy.profile_id)
                or self._authority.is_revoked(
                    "function_principal", request.target.principal_id
                )
                or self._authority.get_host_pending_effect(self.policy.effect_id)
                is not None
                or self._witness.get_host_pending_effect(self.policy.effect_id)
                is not None
            ):
                _deny()
            self._persist_enrollment(
                approval_request_id=approval_request_id,
                approval_digest=decision.digest,
            )
        except CredentialTransportDenied:
            raise
        except Exception:
            _deny()

    def _persist_enrollment(
        self,
        *,
        approval_request_id: str,
        approval_digest: str,
    ) -> None:
        """Persist a checked Host approval; never repair partial enrollment."""
        payload = {
            "effect_id": self.policy.effect_id,
            "state": "active",
            "kind": "credential_generation_budget_v1",
            "policy": self.policy.to_dict(),
            "policy_digest": self.policy.digest,
            "approval_request_id": approval_request_id,
            "approval_digest": approval_digest,
            "attempts": [],
        }
        # No automatic completion after a partial enrollment. If either
        # creation fails, a fresh policy needs a fresh operator decision.
        self._authority.create_host_pending_effect(
            self.policy.effect_id,
            payload,
        )
        self._witness.create_host_pending_effect(
            self.policy.effect_id,
            payload,
        )

    def reserve(
        self,
        *,
        binding: CredentialTransportBinding,
        contract_id: str,
        endpoint: str,
        body: Mapping[str, Any],
    ) -> None:
        """Durably consume one slot; failure and unknown outcomes stay spent."""
        self._validate_send(binding, contract_id, endpoint, body)
        attempt = authority_digest(
            {
                "profile_id": binding.profile_id,
                "request_id": binding.request_id,
                "request_digest": binding.request_digest,
                "credential_handle": binding.credential_handle,
            }
        )
        try:
            first = self._authority.get_host_pending_effect(self.policy.effect_id)
            second = self._witness.get_host_pending_effect(self.policy.effect_id)
            if first is None or first != second:
                _deny()
            revision, state = first
            attempts = state.get("attempts")
            if (
                state.get("policy") != self.policy.to_dict()
                or state.get("policy_digest") != self.policy.digest
                or state.get("kind") != "credential_generation_budget_v1"
                or state.get("state") not in {"active", "exhausted"}
                or type(attempts) is not list
                or len(attempts) != len(set(attempts))
                or any(type(value) is not str for value in attempts)
                or revision != len(attempts) + 1
                or len(attempts) > self.policy.max_requests
            ):
                _deny()
            if attempt in attempts:
                _deny("generation_budget_replay")
            if len(attempts) >= self.policy.max_requests:
                _deny("generation_budget_exhausted")
            following = {**state, "attempts": [*attempts, attempt]}
            if len(following["attempts"]) == self.policy.max_requests:
                following["state"] = "exhausted"
            # The first CAS is the atomic linearization point. A losing peer
            # cannot send. Both audited commits must finish before returning.
            next_revision = self._authority.compare_and_swap_host_pending_effect(
                self.policy.effect_id,
                expected_revision=revision,
                payload=following,
            )
            self._witness.compare_and_swap_host_pending_effect(
                self.policy.effect_id,
                expected_revision=revision,
                payload=following,
            )
            expected = (next_revision, following)
            if (
                self._authority.get_host_pending_effect(self.policy.effect_id)
                != expected
                or self._witness.get_host_pending_effect(self.policy.effect_id)
                != expected
            ):
                _deny()
        except CredentialTransportDenied:
            raise
        except Exception:
            _deny()

    def _validate_send(
        self,
        binding: CredentialTransportBinding,
        contract_id: str,
        endpoint: str,
        body: Mapping[str, Any],
    ) -> None:
        policy = self.policy
        provider = body.get("provider")
        maximum = provider.get("max_price") if isinstance(provider, Mapping) else None
        if (
            (contract_id, binding.provider_function_id, binding.operation_id)
            not in _GENERATION_ROUTES
            or binding.profile_id != policy.profile_id
            or binding.consumer_pack_id != policy.consumer_pack_id
            or binding.provider_instance_id != policy.connection_id
            or binding.credential_handle != policy.credential_handle
            or binding.credential_key_version != policy.credential_key_version
            or binding.credential_purpose != "provider.invoke"
            or binding.endpoint_origin != "https://openrouter.ai"
            or binding.credential_scope
            != ("ai.stream" if contract_id.endswith("stream.v1") else "ai.generate")
            or endpoint != policy.endpoint
            or set(body) - _BODY_KEYS
            or body.get("model") != policy.model
            or type(body.get("max_tokens")) is not int
            or not 1 <= body["max_tokens"] <= policy.max_output_tokens
            or ("n" in body and (type(body["n"]) is not int or body["n"] != 1))
            or not isinstance(provider, Mapping)
            or set(provider) != {"allow_fallbacks", "max_price"}
            or provider.get("allow_fallbacks") is not False
            or not isinstance(maximum, Mapping)
            or set(maximum) != {"prompt", "completion"}
            or any(
                type(value) not in (int, float) or value != 0
                for value in maximum.values()
            )
        ):
            _deny()
