"""Serializable contracts shared by runtime, Studio, approval, and trace UI."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from core_runtime.capability_plan import (
    CAPABILITY_PLAN_SCHEMA_VERSION,
    canonical_capability_plan_digest,
)


@dataclass(frozen=True, slots=True)
class CapabilityTarget:
    """One explicit target from a namespaced mention or structured request."""

    kind: str
    id: str

    def to_dict(self) -> dict[str, str]:
        """Return the stable wire representation."""

        return {"kind": self.kind, "id": self.id}


@dataclass(frozen=True, slots=True)
class CapabilityRegistrySnapshot:
    """One immutable, all-registry readiness publication."""

    revision: str
    activity_ids: tuple[str, ...]
    tool_ids: tuple[str, ...]
    skill_ids: tuple[str, ...]
    invalid_activity_ids: tuple[str, ...] = ()
    diagnostics: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class CapabilityPlan:
    """The single compiled authority for model attachment and execution."""

    registry_revision: str
    policy_revision: str
    intent_text_hash: str
    policy_generation: int = 0
    explicit_mentions: list[CapabilityTarget] = field(default_factory=list)
    activities: list[dict[str, Any]] = field(default_factory=list)
    tool_candidates: list[str] = field(default_factory=list)
    selected_tools: list[str] = field(default_factory=list)
    hydrated_tools: list[str] = field(default_factory=list)
    attached_tools: list[str] = field(default_factory=list)
    excluded_tools: list[dict[str, str]] = field(default_factory=list)
    required_skills: list[str] = field(default_factory=list)
    selected_skills: list[str] = field(default_factory=list)
    loaded_skills: list[str] = field(default_factory=list)
    tool_schema_hashes: dict[str, str] = field(default_factory=dict)
    tool_capability_grants: dict[str, list[str]] = field(default_factory=dict)
    provider_selections: dict[str, list[str]] = field(default_factory=dict)
    skill_instruction_hashes: dict[str, str] = field(default_factory=dict)
    approval_effects: list[dict[str, str]] = field(default_factory=list)
    tool_schema_tokens: int = 0
    skill_instruction_tokens: int = 0
    fallbacks: list[dict[str, str]] = field(default_factory=list)
    diagnostics: list[dict[str, str]] = field(default_factory=list)
    trace_id: str = field(default_factory=lambda: f"trace_{uuid4().hex}")
    plan_id: str = field(default_factory=lambda: f"plan_{uuid4().hex}")

    @classmethod
    def empty(
        cls,
        *,
        user_text: str,
        registry_revision: str = "",
        policy_revision: str = "",
    ) -> "CapabilityPlan":
        """Create an empty, traceable plan for disabled or no-tool turns."""

        return cls(
            registry_revision=registry_revision,
            policy_revision=policy_revision,
            intent_text_hash=_digest_text(user_text),
        )

    def to_dict(self) -> dict[str, Any]:
        """Return the versioned JSON contract."""

        result = {
            "schema_version": CAPABILITY_PLAN_SCHEMA_VERSION,
            "plan_id": self.plan_id,
            "registry_revision": self.registry_revision,
            "policy_revision": self.policy_revision,
            "policy_generation": self.policy_generation,
            "intent": {
                "text_hash": self.intent_text_hash,
                "explicit_mentions": [
                    target.to_dict() for target in self.explicit_mentions
                ],
            },
            "activities": list(self.activities),
            "tools": {
                "candidates": list(self.tool_candidates),
                "selected": list(self.selected_tools),
                "hydrated": list(self.hydrated_tools),
                "attached": list(self.attached_tools),
                "excluded": list(self.excluded_tools),
                "schema_hashes": dict(self.tool_schema_hashes),
                "capability_grants": {
                    key: list(value)
                    for key, value in sorted(
                        self.tool_capability_grants.items()
                    )
                },
            },
            "skills": {
                "required": list(self.required_skills),
                "selected": list(self.selected_skills),
                "loaded": list(self.loaded_skills),
                "instruction_hashes": dict(self.skill_instruction_hashes),
            },
            "approval": {"effects": list(self.approval_effects)},
            "effective_capabilities": sorted(
                {
                    capability
                    for tool_id in self.attached_tools
                    for capability in self.tool_capability_grants.get(
                        tool_id,
                        [],
                    )
                }
            ),
            "provider_selections": {
                key: sorted(set(value))
                for key, value in sorted(self.provider_selections.items())
            },
            "budget": {
                "tool_schema_tokens": self.tool_schema_tokens,
                "skill_instruction_tokens": self.skill_instruction_tokens,
            },
            "fallbacks": list(self.fallbacks),
            "diagnostics": list(self.diagnostics),
            "trace_id": self.trace_id,
        }
        result["digest"] = canonical_capability_plan_digest(result)
        return result


def stable_revision(value: Any) -> str:
    """Return a deterministic SHA-256 revision for JSON-compatible state."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return sha256(payload).hexdigest()


def _digest_text(value: str) -> str:
    return sha256(str(value or "").encode("utf-8")).hexdigest()
