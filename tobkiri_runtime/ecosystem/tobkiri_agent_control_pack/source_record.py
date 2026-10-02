"""Produce this optional Pack's canonical source declaration for integration."""

from __future__ import annotations
import hashlib
import json
from pathlib import Path
from typing import Any

PACK = "tobkiri_agent_control_pack"
ROOT = Path(__file__).resolve().parent
ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 256,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]*$",
}
TEXT = {"type": "string", "minLength": 1, "maxLength": 32000}
INTEGER = {"type": "integer", "minimum": 0}
STRINGS = {"type": "array", "maxItems": 1000, "items": TEXT}


def obj(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    """Declare finite wire properties for generic catalog admission."""
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def _saved_input_schema() -> dict[str, Any]:
    """Inline the neutral saved input schema inside the finite ack schema."""
    source = json.loads(
        (
            ROOT.parents[1] / "tobkiri_protocol/schemas/saved_conversation_input_v1.schema.json"
        ).read_text()
    )

    def inline(value: Any) -> Any:
        if isinstance(value, list):
            return [inline(item) for item in value]
        if not isinstance(value, dict):
            return value
        if "$ref" in value:
            return inline(source["$defs"][value["$ref"].split("/")[-1]])
        return {
            key: inline(item)
            for key, item in value.items()
            if key not in {"$defs", "$id", "$schema"}
        }

    return inline(source)


def schemas() -> dict[str, dict[str, Any]]:
    """Return strict public request schemas, including nested settings."""
    common = {
        "profile_id": ID,
        "plan_id": ID,
        "conversation_id": ID,
        "operation_id": ID,
        "expected_revision": INTEGER,
        "conversation_revision": {"type": "integer", "minimum": 1},
    }
    model = obj(
        {
            "mode": {"enum": ["inherit_conversation", "fixed", "snapshot"]},
            "profile_id": ID,
            "snapshot_profile_id": ID,
            "fallback_profile_id": ID,
            "on_unavailable": {"enum": ["fail", "fallback"]},
            "required_capabilities": STRINGS,
        },
        ["mode"],
    )
    thinking = obj(
        {
            "mode": {"enum": ["inherit_conversation", "fixed", "model_default"]},
            "level": TEXT,
        },
        ["mode"],
    )
    settings = obj(
        {
            "enabled": {"type": "boolean"},
            "interval_seconds": {"type": "integer", "minimum": 60, "maximum": 86400},
            "executor": {"anyOf": [ID, {"type": "null"}]},
            "reviewer": {"anyOf": [ID, {"type": "null"}]},
            "model_policy": model,
            "thinking_policy": thinking,
        },
        [],
    )
    action_names = [
        "goal.set",
        "goal.refine",
        "goal.prepare_replace",
        "goal.cancel_replace",
        "todo.add",
        "todo.update",
        "todo.cancel",
        "remind.deliver",
        "remind.create",
        "remind.cancel",
    ]
    action = obj(
        {
            **common,
            "operation": {"enum": action_names},
            "goal_id": ID,
            "subgoal_id": ID,
            "preview_id": ID,
            "item_id": ID,
            "body": TEXT,
            "criteria": STRINGS,
            "constraints": STRINGS,
            "dependencies": {"type": "array", "items": ID, "maxItems": 1000},
            "order": INTEGER,
            "agent_binding": ID,
            "event_id": ID,
            "lifetime": {"enum": ["one_turn", "until_task_end"]},
            "expires_at_ms": INTEGER,
            "updates": obj(
                {
                    "body": TEXT,
                    "criteria": STRINGS,
                    "dependencies": STRINGS,
                    "order": INTEGER,
                },
                [],
            ),
            "reminder_id": ID,
            "timezone": TEXT,
            "delay_seconds": {"type": "integer", "minimum": 1},
            "at_ms": {"type": "integer", "minimum": 1},
            "interval_seconds": INTEGER,
        },
        ["operation", "plan_id", "expected_revision"],
    )
    configure = obj(
        {
            **common,
            "operation": {
                "enum": [
                    "settings.configure",
                    "plan.pause",
                    "plan.resume",
                    "plan.cancel",
                ]
            },
            "settings": settings,
            "executor": ID,
            "reviewer": ID,
            "enabled": {"type": "boolean"},
            "interval_seconds": {
                "anyOf": [
                    {"type": "integer", "minimum": 60, "maximum": 86400},
                    {"type": "string", "pattern": "^[0-9]{2,5}$"},
                ]
            },
        },
        ["operation", "plan_id", "expected_revision"],
    )
    replace = obj(
        {
            **common,
            "operation": {"const": "goal.commit_replace"},
            "preview_id": ID,
            "preview_digest": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        },
        ["operation", "plan_id", "expected_revision", "preview_id", "preview_digest"],
    )
    context = obj(
        {
            **common,
            "operation": {"enum": ["prepare_input", "prepare_input_for_conversation"]},
            "agent_binding": ID,
            "input_id": ID,
            "boundary": {"enum": ["before_turn", "between_tools"]},
        },
        ["operation", "conversation_id", "input_id", "boundary"],
    )
    ack = obj(
        {
            **common,
            "operation": {"const": "ack"},
            "input_id": ID,
            "event_ids": {"type": "array", "items": ID, "maxItems": 10000},
            "accepted_input": _saved_input_schema(),
        },
        ["operation", "plan_id", "expected_revision", "input_id", "event_ids", "accepted_input"],
    )
    job = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "operation": {"enum": ["describe", "dispatch", "cancel", "status"]},
            "profile_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$",
            },
            "action_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$",
            },
            "payload": {"type": "object"},
            "idempotency_key": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "pattern": "^[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}$",
            },
            "schedule_id": {"type": "string", "maxLength": 256},
            "lease_id": {"type": "string", "maxLength": 256},
        },
        "required": ["operation"],
    }
    return {
        "resource": obj(
            {
                "profile_id": ID,
                "operation": {"enum": ["get", "list", "get_for_conversation"]},
                "plan_id": ID,
                "conversation_id": ID,
            },
            ["operation"],
        ),
        "action": action,
        "settings": configure,
        "replace": replace,
        "context": context,
        "inbox": ack,
        "job": job,
        "review": obj(
            {
                "operation": {"const": "inspect"},
                "profile_id": ID,
                "plan_id": ID,
                "generation": INTEGER,
                "occurrence_id": ID,
            },
            ["operation", "plan_id", "generation", "occurrence_id"],
        ),
        "execute": obj(
            {**common, "operation": {"const": "run_next"}},
            ["operation", "plan_id", "expected_revision"],
        ),
    }


def build() -> dict[str, Any]:
    """Create hashes only for this Pack; global catalogs remain integration-owned."""
    from ecosystem.tobkiri_agent_control_pack.runtime.host import (
        CONTRACTS,
        DEPENDENCIES,
    )

    artifacts = [
        {
            "kind": "executable" if p.suffix == ".py" else "sidecar",
            "path": str(p.relative_to(ROOT)),
            "digest": "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest(),
        }
        for p in sorted(ROOT.rglob("*"))
        if p.is_file()
        and (
            p.parts[-2] == "runtime"
            and p.suffix == ".py"
            or p.suffix == ".json"
            and "contributions" in p.parts
            or p.parent.name == "flows"
        )
    ]
    implementation = "sha256:" + hashlib.sha256((ROOT / "runtime/host.py").read_bytes()).hexdigest()
    provided = []
    for kind, (contract, suffix) in CONTRACTS.items():
        operation = f"{PACK}.{suffix}"
        provided.append(
            {
                "contract_id": contract,
                "version": "2.0.0" if kind == "job" else "1.0.0",
                "owner": PACK,
                "provider_id": f"{PACK}.work-plan.{kind}",
                "cardinality": "many" if kind == "job" else "one",
                "failure": "fail_closed",
                "isolation": "in_process",
                "security": "sensitive",
                "required_capabilities": [f"work_plan.{kind}"],
                "lifecycle": {
                    "data_owner": PACK,
                    "introduced": "1.0.0",
                    "deprecated": False,
                },
                "operations": [
                    {
                        "id": operation,
                        "entrypoint_id": suffix,
                        "implementation_digest": implementation,
                    }
                ],
                "schemas": {
                    "input": schemas()[kind],
                    "output": {"type": "object"},
                    "error": {"type": "object"},
                },
            }
        )
    return {
        "pack_id": PACK,
        "display_name": "Tobkiri Agent Control",
        "description": "Optional persistent work plan with dependency Todo execution and independent review guidance.",
        "version": "1.0.0",
        "authority": "v4-authoritative",
        "kind": "host_extension",
        "execution_boundary": "host_brokered",
        "workspace_boundary": "none",
        "approval_policy": "capability_gated",
        "capabilities": [f"work_plan.{kind}" for kind in CONTRACTS],
        "dependencies": {},
        "provided_contracts": provided,
        "required_contracts": [
            {
                "contract_id": c,
                "cardinality": "one",
                "version_range": ">=1.0.0 <2.0.0",
                "optional": True,
            }
            for c in sorted(DEPENDENCIES)
        ],
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "legacy_ids": [],
        "legacy_operations": [],
        "migration": {"removal_wave": 10, "sunset_at": "2027-12-31"},
        "source_provenance": {
            "owner": PACK,
            "mode": "canonical-v4",
            "historical_classification": "modern-only",
            "source_format": "pack_v4_catalog.v1",
        },
        "source_evidence": [],
        "runtime_artifacts": artifacts,
    }


if __name__ == "__main__":
    print(json.dumps(build(), indent=2, sort_keys=True))
