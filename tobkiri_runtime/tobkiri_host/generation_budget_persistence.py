"""Identify authenticated generation-budget rows outside interactive recovery.

This only classifies an already authenticated inventory. It never grants
authority, repairs state, compares witnesses, or admits a generation request.
Malformed records remain visible to the strict interactive-effect parser.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from core_runtime.authority.v4_models import authority_digest
from core_runtime.generation_transport_budget import (
    GenerationBudgetPolicy,
    PRICE_POLICY_FIELDS,
)
from core_runtime.native_generation_budget import NativeGenerationBudgetTemplate

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_PLAN_FIELDS = {
    "profile_id",
    "provider_instance_id",
    "adapter_id",
    "endpoint",
    "expected_revision",
    "request_digest",
}


def _identifier(value: Any) -> bool:
    return (
        type(value) is str
        and 0 < len(value) <= 512
        and not any(char in value for char in "\x00\r\n")
    )


def is_generation_budget_record(revision: int, payload: Mapping[str, Any]) -> bool:
    """Recognize only complete records in their existing Host owner namespace.

    The immutable Native rows have revision one. The ledger's revision and
    state must match its unique consumed-attempt inventory. Exact key sets
    prevent a corrupt interactive record disguised with a budget kind/ID from
    being hidden. Authorization and both-store checks remain with the owner.
    """
    if type(revision) is not int or revision < 1:
        return False
    try:
        if payload.get("kind") == "native_generation_budget_template_v1":
            if set(payload) != {"effect_id", "state", "kind", "template"}:
                return False
            template = NativeGenerationBudgetTemplate(**payload["template"])
            return (
                revision == 1
                and payload["state"] == "required"
                and payload["effect_id"] == template.effect_id
                and payload["template"] == template.to_dict()
            )
        policy = GenerationBudgetPolicy(**payload["policy"])
        if payload["policy"] != policy.to_dict():
            return False
        if payload.get("kind") == "credential_generation_budget_v1":
            if set(payload) != {
                "effect_id",
                "state",
                "kind",
                "policy",
                "policy_digest",
                "approval_request_id",
                "approval_digest",
                "attempts",
            }:
                return False
            attempts = payload["attempts"]
            if type(attempts) is not list or any(
                type(value) is not str or not _DIGEST.fullmatch(value)
                for value in attempts
            ):
                return False
            count = len(attempts)
            return (
                payload["effect_id"] == policy.effect_id
                and payload["policy_digest"] == policy.digest
                and _identifier(payload["approval_request_id"])
                and type(payload["approval_digest"]) is str
                and bool(_DIGEST.fullmatch(payload["approval_digest"]))
                and count == len(set(attempts))
                and count <= policy.max_requests
                and revision == count + 1
                and payload["state"] == ("exhausted" if count == policy.max_requests else "active")
            )
        template = NativeGenerationBudgetTemplate(
            policy.profile_id,
            policy.connection_id,
            model=policy.model,
            **{field: getattr(policy, field) for field in PRICE_POLICY_FIELDS},
        )
        if (
            revision != 1
            or policy.consumer_pack_id != template.consumer_pack_id
            or policy.model != template.model
            or policy.max_requests != template.max_requests
            or policy.max_output_tokens != template.max_output_tokens
        ):
            return False
        if set(payload) == {"effect_id", "state", "policy", "approval_request_id"}:
            return (
                payload["effect_id"] == template.effect_id + ":active"
                and payload["state"] == "enrolled"
                and _identifier(payload["approval_request_id"])
            )
        if set(payload) != {"effect_id", "state", "plan", "policy"}:
            return False
        plan = payload["plan"]
        return (
            isinstance(plan, Mapping)
            and set(plan) == _PLAN_FIELDS
            and plan["profile_id"] == policy.profile_id
            and plan["provider_instance_id"] == policy.connection_id
            and plan["adapter_id"] == "openai-compatible"
            and plan["endpoint"] == "https://openrouter.ai/api/v1"
            and type(plan["expected_revision"]) is int
            and 0 <= plan["expected_revision"] <= 2**53 - 1
            and type(plan["request_digest"]) is str
            and bool(_DIGEST.fullmatch(plan["request_digest"]))
            and payload["state"] == "prepared"
            and payload["effect_id"]
            == (template.effect_id + ":draft:" + authority_digest(dict(plan))[7:])
        )
    except (KeyError, TypeError, ValueError):
        return False
