"""Reviewed finite task provider declarations, preserving legacy source identities."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from tobkiri_protocol.provenance import sha256_file
from tobkiri_protocol.workspace_task_v1 import (
    TASK_CONTRACT,
    TASK_RESOURCE,
    PREPARE,
    EXECUTE,
    RESOURCE,
    PLAN_FIELDS,
    REQUEST_FIELDS,
)

PACK = Path(__file__).resolve().parent
ROOT = PACK.parents[1]
PACK_ID = "rumi_coding_sandbox_service_pack"
ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
}
DIGEST = {"type": "string", "pattern": "^sha256:[0-9a-f]{64}$"}
INTEGER = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}


def schemas() -> dict[str, dict[str, Any]]:
    """Define exact finite request, prepare and execute plan schemas."""
    properties = {
        "task_request_id": ID,
        "profile_id": ID,
        "workspace_id": ID,
        "expected_revision": INTEGER | {"minimum": 1},
        "expected_writer_epoch": INTEGER | {"minimum": 1},
        "argv": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {"type": "string", "maxLength": 8192},
        },
        "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 120},
        "version": {"const": "tobkiri.workspace-task-plan.v1"},
        "task_id": ID,
        "plan_digest": DIGEST,
        "security_epoch": INTEGER,
        "checkpoint_digest": DIGEST,
        "recipe_digest": DIGEST,
        "image_reference": {"type": "string", "maxLength": 272},
        "expires_at_ms": INTEGER | {"minimum": 1},
        "request_digest": DIGEST,
    }
    request = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: properties[key] for key in REQUEST_FIELDS},
        "required": sorted(REQUEST_FIELDS),
    }
    plan = {
        "type": "object",
        "additionalProperties": False,
        "properties": {key: properties[key] for key in PLAN_FIELDS},
        "required": sorted(PLAN_FIELDS),
    }
    execute = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"task_plan": plan, "task_plan_digest": DIGEST},
        "required": ["task_plan", "task_plan_digest"],
    }
    prepared = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "executed": {"const": False},
            "task_plan": plan,
            "task_plan_digest": DIGEST,
        },
        "required": ["executed", "task_plan", "task_plan_digest"],
    }
    resource = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "profile_id": ID,
            "operation": {"enum": ["availability", "status", "export"]},
            "task_id": ID,
        },
        "required": ["profile_id", "operation"],
        "oneOf": [
            {
                "properties": {"operation": {"const": "availability"}},
                "not": {"required": ["task_id"]},
            },
            {
                "properties": {"operation": {"enum": ["status", "export"]}},
                "required": ["task_id"],
            },
        ],
    }
    return {
        "request": request,
        "plan": plan,
        "execute": execute,
        "prepared": prepared,
        "resource": resource,
    }


def build_source() -> dict[str, Any]:
    """Generate current source bytes without modifying shared adoption catalogs."""
    schema = schemas()
    providers = []
    for kind, contract, operation, effect in [
        ("prepare", TASK_CONTRACT, PREPARE, "read"),
        ("execute", TASK_CONTRACT, EXECUTE, "write"),
        ("resource", TASK_RESOURCE, RESOURCE, "read"),
    ]:
        capability = "coding.sandbox.task." + ("read" if kind == "resource" else kind)
        providers.append(
            {
                "function_id": PACK_ID + ".task-" + kind,
                "contract_id": contract,
                "operation_id": operation,
                "capability": capability,
                "effect_class": effect,
                "effect_ceiling": ["capability:" + capability]
                + (
                    ["host:brokered-execution", "capability:cloud.workspace.resource"]
                    if kind != "resource"
                    else []
                ),
                "schemas": {
                    "input": schema[
                        {
                            "prepare": "request",
                            "execute": "execute",
                            "resource": "resource",
                        }[kind]
                    ],
                    "output": schema["prepared"]
                    if kind == "prepare"
                    else {"type": "object"},
                    "error": {"type": "object"},
                },
            }
        )
    return {
        "source_api_version": "tobkiri.pack-source.v1",
        "pack_id": PACK_ID,
        "version": "1.0.0",
        "display_name": "Tobkiri Coding Sandbox Service",
        "implementation_path": "runtime/host_v4.py",
        "runtime_module": "ecosystem.rumi_coding_sandbox_service_pack.runtime.host_v4",
        "factory_symbol": "HOST_PROVIDER_FACTORY",
        "providers": providers,
        "compatibility_source": "compatibility-source.v1.json",
        "runtime_artifacts": [
            {
                "path": path,
                "kind": "executable" if path.startswith("runtime/") else "sidecar",
            }
            for path in [
                "runtime/__init__.py",
                "runtime/sandbox.py",
                "runtime/host_v4.py",
                "runtime/task_state.py",
                "runtime/task_container.py",
                "pack-source.v1.json",
                "compatibility-source.v1.json",
            ]
        ],
        "protocol_sources": [
            {"path": path, "digest": sha256_file(ROOT / path)}
            for path in [
                "tobkiri_protocol/workspace_capsule_v1.py",
                "tobkiri_protocol/workspace_task_v1.py",
            ]
        ],
        "public_dependencies": [
            {
                "contract_id": "tobkiri.resource.cloud.workspace.v1",
                "operation_id": "tobkiri_cloud_workspace_pack.workspace-resource",
                "version_range": ">=1.0.0 <2.0.0",
                "optional": True,
            }
        ],
        "declared_pack_data": [
            {"pack_id": "tobkiri_cloud_workspace_pack", "path_prefix": "container/"}
        ],
        "public_capabilities": ["cloud.workspace.resource"],
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "ui_contributions": [],
    }


if __name__ == "__main__":
    (PACK / "pack-source.v1.json").write_text(
        json.dumps(build_source(), indent=2, sort_keys=True) + "\n"
    )
