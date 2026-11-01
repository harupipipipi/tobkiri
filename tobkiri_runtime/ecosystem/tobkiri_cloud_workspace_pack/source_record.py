"""Generate a scoped canonical-v4 source declaration without installing a Pack."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

PACK = Path(__file__).resolve().parent
PACK_ID = "tobkiri_cloud_workspace_pack"
ID = {
    "type": "string",
    "minLength": 1,
    "maxLength": 128,
    "pattern": "^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
}
REVISION = {"type": "integer", "minimum": 0, "maximum": 2**53 - 1}


def _schema(kind: str) -> dict[str, Any]:
    from ecosystem.tobkiri_cloud_workspace_pack.runtime.host import FIELDS

    fields = FIELDS[kind]
    properties = {
        "profile_id": ID,
        "operation": {"enum": list(fields)},
        "conversation_id": ID,
        "workspace_id": ID,
        "expected_revision": REVISION,
        "expected_writer_epoch": REVISION,
        "paths": {"type": "string", "minLength": 1, "maxLength": 32768},
        "argv_json": {"type": "string", "minLength": 1, "maxLength": 24000},
        "archive_base64": {"type": "string", "minLength": 1, "maxLength": 13981016},
        "expected_receiver_head": {
            "type": "string",
            "maxLength": 71,
            "pattern": "^(?:sha256:[0-9a-f]{64})?$",
        },
    }
    used = {"profile_id", "operation"} | set().union(*fields.values())
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {k: v for k, v in properties.items() if k in used},
        "required": ["profile_id", "operation"],
        "oneOf": [
            {
                "properties": {"operation": {"const": operation}},
                "required": sorted(required),
            }
            for operation, required in fields.items()
        ],
    }


def build_source() -> dict[str, Any]:
    """Derive current byte digests and public finite operation requirements."""
    shared = PACK.parents[1] / "tobkiri_protocol/workspace_capsule_v1.py"
    (PACK / "container/workspace_capsule_v1.py").write_bytes(shared.read_bytes())
    (PACK / "container/workspace_tree_v1.py").write_bytes(
        (PACK.parents[1] / "tobkiri_protocol/workspace_tree_v1.py").read_bytes()
    )
    recipe_paths = [
        "container/Dockerfile",
        "container/runtime.py",
        "container/workspace_capsule_v1.py",
    ]
    recipe = {
        "version": "tobkiri.workspace-container-recipe.v1",
        "files": [
            {
                "path": path,
                "digest": "sha256:"
                + hashlib.sha256((PACK / path).read_bytes()).hexdigest(),
            }
            for path in recipe_paths
        ],
        "runtime_user": "65532:65532",
        "privileged": False,
        "base_image_policy": "reviewed-oci-index-digest-pinned",
        "base_image_reference": "python:3.13-slim@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81",
        "container_port": 8765,
        "container_execution": "unverified",
        "workload_execution": "not_implemented",
        "required_mounts": {
            "capsule": "/input/workspace.zip:ro",
            "workspace": "/workspace:rw",
        },
        "health_route": "/health",
        "start_command": [
            "python3",
            "-B",
            "/opt/tobkiri/workspace_runtime.py",
            "--capsule",
            "/input/workspace.zip",
            "--workspace",
            "/workspace",
            "--listen",
            "0.0.0.0",
            "--port",
            "8765",
        ],
    }
    recipe_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                recipe,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )
    (PACK / "container/recipe.v1.json").write_text(json.dumps(recipe, indent=2) + "\n")
    task_recipe = {
        "version": "tobkiri.workspace-task-recipe.v1",
        "image_reference": "rumiai/mimo-coding-company-worker@sha256:1c4c9e219f46c9311bc4cba2f328303208ff24d95e2e39f769ba12383ef075ec",
        "portable_recipe_digest": recipe_digest,
        "network": "none",
        "nonroot": True,
        "read_only_root": True,
        "max_timeout_seconds": 120,
        "max_work_bytes": 4 * 1024 * 1024,
        "workspace_tmpfs_bytes": 8 * 1024 * 1024,
        "workspace_tmpfs_inodes": 512,
        "tmp_tmpfs_bytes": 16 * 1024 * 1024,
        "tmp_tmpfs_inodes": 256,
        "files": [
            {
                "path": path,
                "digest": "sha256:"
                + hashlib.sha256((PACK / path).read_bytes()).hexdigest(),
            }
            for path in [
                "container/task_runner.py",
                "container/workspace_capsule_v1.py",
                "container/workspace_tree_v1.py",
            ]
        ],
    }
    (PACK / "container/task-recipe.v1.json").write_text(
        json.dumps(task_recipe, indent=2) + "\n"
    )
    task_digest = (
        "sha256:"
        + hashlib.sha256(
            json.dumps(
                task_recipe, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode()
        ).hexdigest()
    )
    (PACK / "runtime/recipe.py").write_text(
        '"""Digest of the reviewed container recipe; no provision capability."""\n\n'
        f'RECIPE_DIGEST = (\n    "{recipe_digest}"\n)\n'
        f'TASK_RECIPE_DIGEST = (\n    "{task_digest}"\n)\n'
    )
    providers = []
    for kind, contract, operation, effect in [
        (
            "resource",
            "tobkiri.resource.cloud.workspace.v1",
            "workspace-resource",
            "read",
        ),
        ("manage", "tobkiri.action.cloud.workspace.v1", "workspace-manage", "write"),
    ]:
        capability = f"cloud.workspace.{kind}"
        providers.append(
            {
                "function_id": f"{PACK_ID}.{kind}",
                "contract_id": contract,
                "operation_id": f"{PACK_ID}.{operation}",
                "contract_version": "1.0.0",
                "capability": capability,
                "effect_class": effect,
                "effect_ceiling": ["capability:" + capability]
                + (["host:brokered-execution"] if effect == "write" else []),
                "schemas": {
                    "input": _schema(kind),
                    "output": {"type": "object"},
                    "error": {"type": "object"},
                },
            }
        )
    artifact_paths = [
        "runtime/__init__.py",
        "runtime/host.py",
        "runtime/capsule.py",
        "runtime/store.py",
        "runtime/service.py",
        "runtime/task.py",
        "runtime/recipe.py",
        "container/Dockerfile",
        "container/runtime.py",
        "container/recipe.v1.json",
        "container/task-recipe.v1.json",
        "container/workspace_capsule_v1.py",
        "container/workspace_tree_v1.py",
        "container/task_runner.py",
        "pack-source.v1.json",
        "frontend/contributions/composer.json",
        "frontend/contributions/workspace.json",
    ]
    return {
        "source_api_version": "tobkiri.pack-source.v1",
        "pack_id": PACK_ID,
        "version": "1.0.0",
        "display_name": "Tobkiri Cloud Workspace",
        "kind": "host_extension",
        "optional": True,
        "implementation_path": "runtime/host.py",
        "runtime_module": "ecosystem.tobkiri_cloud_workspace_pack.runtime.host",
        "factory_symbol": "HOST_PROVIDER_FACTORY",
        "providers": providers,
        "runtime_artifacts": [
            {
                "path": path,
                "kind": "executable" if path.startswith("runtime/") else "sidecar",
            }
            for path in artifact_paths
        ],
        "public_capabilities": [
            "workspace.metadata.read",
            "file.inspect",
            "coding.sandbox.task.read",
            "host.authority.consume",
        ],
        "public_dependencies": [
            {
                "contract_id": "tobkiri.service.interactive-effect.v1",
                "version_range": ">=1.0.0 <2.0.0",
                "operation_id": "interactive_effect.manage",
                "optional": True,
            },
            {
                "contract_id": "tobkiri.resource.workspace.container-task.v1",
                "version_range": ">=1.0.0 <2.0.0",
                "operation_id": "rumi_coding_sandbox_service_pack.task-resource",
                "optional": True,
            },
            {
                "contract_id": "tobkiri.resource.workspace.v1",
                "version_range": ">=1.0.0 <2.0.0",
                "operation_id": "rumi_workspace_mount_pack.workspace-resource",
                "optional": True,
            },
            {
                "contract_id": "tobkiri.service.file.inspect.v1",
                "version_range": ">=1.0.0 <2.0.0",
                "operation_id": "rumi_file_inspect_pack.file-inspect",
                "optional": True,
            },
        ],
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "ui_contributions": [
            "frontend/contributions/composer.json",
            "frontend/contributions/workspace.json",
        ],
        "future_boundaries": {
            "container_host_contract": "tobkiri.action.workspace.container.v1",
            "deployment_pack_contract": "tobkiri.service.workspace.deploy.v1",
            "handoff_protocol": "tobkiri.workspace-handoff.v1",
            "bound": False,
            "remote_execution": "unavailable",
        },
    }


if __name__ == "__main__":
    (PACK / "pack-source.v1.json").write_text(
        json.dumps(build_source(), ensure_ascii=False, indent=2) + "\n",
    )
