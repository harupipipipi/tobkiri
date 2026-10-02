"""Register and generate Workflow Pack artifacts through official v4 compilers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
from typing import Any, Mapping

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
PACK_ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.workflow_v4.provider import WORKFLOW_OPERATIONS  # noqa: E402
from scripts.generate_executable_catalogs_v4 import _render as render_executables  # noqa: E402
from scripts.migrate_pack_artifacts_v4 import (  # noqa: E402
    _render_record,
    verify_rendered_artifacts,
)
from tobkiri_protocol.validation import validate_document  # noqa: E402

PACK_ID = "tobkiri_workflow_pack"
CATALOG_PATH = ROOT / "schemas" / "pack_v4_catalog.v1.json"
AUTHORITY_PATH = ROOT / "schemas" / "manifest_authority.v1.json"
RUNTIME_PATH = "runtime/provider.py"
FRONTEND_MAP_PATH = "frontend_contract_map.v4.json"
BACKEND_INTEGRITY_PATH = "backend-integrity.v4.json"
SCHEMA_PATHS = (
    "schemas/workflow-definition.v4.schema.json",
    "schemas/workflow-operation.v4.schema.json",
    "schemas/workflow-result.v4.schema.json",
    "schemas/workflow-error.v4.schema.json",
    "schemas/frontend-contract-map.v4.schema.json",
)

_PAYLOAD_KEYS = {
    "definition.archive": ["definition_id", "if_match"],
    "definition.create": ["definition_id", "document"],
    "definition.delete": ["definition_id", "if_match"],
    "definition.get": ["definition_id"],
    "definition.list": [],
    "definition.publish": ["definition_id", "if_match"],
    "definition.update": ["definition_id", "document", "if_match"],
    "definition.validate": ["document"],
    "definition.compile-preview": ["document"],
    "operation.palette": [],
    "run.advance": ["run_id"],
    "run.cancel": ["run_id"],
    "run.create": ["definition_id", "inputs", "occurrence_id", "run_id"],
    "run.get": ["run_id"],
    "run.pause": ["run_id"],
    "run.reconcile-recovery": ["run_id"],
    "run.resume": ["run_id"],
    "run.step.execute": ["run_id", "step_id"],
    "run.step.resume": ["run_id", "step_id"],
    "run.step.retry": ["run_id", "step_id"],
}


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _frontend_map() -> dict[str, Any]:
    routes = []
    for operation in WORKFLOW_OPERATIONS:
        routes.append(
            {
                "method": "GET"
                if operation in {"definition.list", "operation.palette"}
                else "POST",
                "path": f"/api/contracts/{PACK_ID}/{operation}",
                "presentation": "broker_result",
                "targets": [
                    {
                        "contribution_id": f"workflow.{operation}",
                        "contract_id": "tobkiri.workflow.v4",
                        "operation_id": operation,
                        "provider_id": "tobkiri.workflow.provider",
                        "function_id": "tobkiri.workflow.provider",
                        "allowed_payload_keys": _PAYLOAD_KEYS[operation],
                    }
                ],
            }
        )
    return {
        "schema": "io.tobkiri.frontend-contract-map.v4",
        "pack_id": PACK_ID,
        "routes": routes,
    }


def _backend_integrity() -> dict[str, Any]:
    backend_root = ROOT / "core_runtime" / "workflow_v4"
    return {
        "schema": "io.tobkiri.workflow-backend-integrity.v4",
        "files": {
            path.relative_to(ROOT).as_posix(): _file_digest(path)
            for path in sorted(backend_root.glob("*.py"))
        },
    }


def _record(frontend_digest: str, backend_integrity_digest: str) -> dict[str, Any]:
    implementation_digest = _file_digest(PACK_ROOT / RUNTIME_PATH)
    operation_schema = json.loads(
        (PACK_ROOT / "schemas/workflow-operation.v4.schema.json").read_text(encoding="utf-8")
    )
    result_schema = json.loads(
        (PACK_ROOT / "schemas/workflow-result.v4.schema.json").read_text(encoding="utf-8")
    )
    error_schema = json.loads(
        (PACK_ROOT / "schemas/workflow-error.v4.schema.json").read_text(encoding="utf-8")
    )
    runtime_artifacts = [
        {
            "path": RUNTIME_PATH,
            "digest": implementation_digest,
            "kind": "executable",
            "index_role": "runtime",
        },
        *[
            {
                "path": path,
                "digest": _file_digest(PACK_ROOT / path),
                "kind": "schema",
                "index_role": "schema",
            }
            for path in SCHEMA_PATHS
        ],
        {
            "path": FRONTEND_MAP_PATH,
            "digest": frontend_digest,
            "kind": "asset",
            "index_role": "asset",
        },
        {
            "path": BACKEND_INTEGRITY_PATH,
            "digest": backend_integrity_digest,
            "kind": "asset",
            "index_role": "asset",
        },
    ]
    source_paths = [
        PACK_ROOT / "generate_v4.py",
        *sorted((ROOT / "core_runtime/workflow_v4").glob("*.py")),
        *[PACK_ROOT / path for path in SCHEMA_PATHS],
        PACK_ROOT / FRONTEND_MAP_PATH,
        PACK_ROOT / BACKEND_INTEGRITY_PATH,
    ]
    capabilities = [
        "workflow.definition.write",
        "workflow.run.control",
        "workflow.step.authority.reserve",
    ]
    return {
        "pack_id": PACK_ID,
        "version": "4.0.0",
        "kind": "host_extension",
        "display_name": "Tobkiri Workflow Pack",
        "description": "Authority-aware local-first Workflow v4 provider.",
        "authority": "v4-authoritative",
        "source_provenance": {
            "owner": PACK_ID,
            "mode": "canonical-v4",
            "source_format": "pack.v4.json",
            "historical_classification": "modern-only",
        },
        "dependencies": {},
        "required_contracts": [],
        "capabilities": capabilities,
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "execution_boundary": "host_brokered",
        "approval_policy": "capability_gated",
        "workspace_boundary": "host_brokered",
        "provided_contracts": [
            {
                "contract_id": "tobkiri.workflow.v4",
                "version": "4.0.0",
                "owner": PACK_ID,
                "provider_id": "tobkiri.workflow.provider",
                "operations": [
                    {
                        "id": operation,
                        "entrypoint_id": operation,
                        "implementation_digest": implementation_digest,
                    }
                    for operation in WORKFLOW_OPERATIONS
                ],
                "cardinality": "one",
                "security": "restricted",
                "failure": "fail_closed",
                "isolation": "in_process",
                "required_capabilities": capabilities,
                "lifecycle": {
                    "deprecated": False,
                    "introduced": "4.0.0",
                    "local_first": True,
                    "authority_per_attempt": True,
                },
                "schemas": {
                    "input": operation_schema,
                    "output": result_schema,
                    "error": error_schema,
                },
            }
        ],
        "legacy_operations": [],
        "runtime_artifacts": runtime_artifacts,
        "legacy_ids": [],
        "migration": {
            "compatibility": "none",
            "removal_wave": 13,
            "sunset_at": "2026-08-10",
        },
        "source_evidence": [
            {
                "path": path.relative_to(ROOT).as_posix(),
                "rule_id": "workflow-v4-canonical-source",
                "digest": _file_digest(path),
            }
            for path in source_paths
        ],
    }


def _upsert_catalog(record: Mapping[str, Any]) -> str:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    records = {
        str(item["pack_id"]): item for item in payload["packs"] if item["pack_id"] != PACK_ID
    }
    records[PACK_ID] = dict(record)
    payload["pack_ids"] = sorted(records)
    payload["packs"] = [records[pack_id] for pack_id in payload["pack_ids"]]
    return _text(payload)


def _upsert_authority() -> str:
    payload = json.loads(AUTHORITY_PATH.read_text(encoding="utf-8"))
    payload["packs"][PACK_ID] = "v4-authoritative"
    payload["packs"] = dict(sorted(payload["packs"].items()))
    return _text(payload)


def render(*, check: bool) -> dict[Path, str]:
    """Render canonical source, Pack, executable, and frontend artifacts."""

    frontend_text = _text(_frontend_map())
    frontend_schema = json.loads(
        (PACK_ROOT / "schemas/frontend-contract-map.v4.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator(frontend_schema).validate(json.loads(frontend_text))
    frontend_digest = "sha256:" + hashlib.sha256(frontend_text.encode()).hexdigest()
    backend_integrity_text = _text(_backend_integrity())
    backend_integrity_digest = (
        "sha256:" + hashlib.sha256(backend_integrity_text.encode()).hexdigest()
    )
    frontend_path = PACK_ROOT / FRONTEND_MAP_PATH
    backend_integrity_path = PACK_ROOT / BACKEND_INTEGRITY_PATH
    source_assets = {
        frontend_path: frontend_text,
        backend_integrity_path: backend_integrity_text,
    }
    stale_source_assets = [
        path
        for path, content in source_assets.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if check and stale_source_assets:
        raise RuntimeError("Workflow Pack v4 generated source assets are stale")
    if not check:
        for path, content in source_assets.items():
            path.write_text(content, encoding="utf-8")
    record = _record(frontend_digest, backend_integrity_digest)
    files = _render_record(record)
    verify_rendered_artifacts(files)
    rendered: dict[Path, str] = {
        CATALOG_PATH: _upsert_catalog(record),
        AUTHORITY_PATH: _upsert_authority(),
        **source_assets,
        **{PACK_ROOT / name: content for name, content in files.items()},
    }
    compiler_inputs = {PACK_ROOT / name: content for name, content in files.items()}
    if check:
        stale_inputs = [
            path
            for path, content in compiler_inputs.items()
            if not path.is_file() or path.read_text(encoding="utf-8") != content
        ]
        if stale_inputs:
            raise RuntimeError("Workflow Pack v4 official compiler inputs are stale")
    else:
        for path, content in compiler_inputs.items():
            path.write_text(content, encoding="utf-8")
    rendered[PACK_ROOT / "executables.v4.json"] = _text(render_executables(PACK_ID))
    return rendered


def generate(*, check: bool = False) -> dict[str, int]:
    """Write or check all Workflow Pack canonical artifacts."""

    rendered = render(check=check)
    stale = [
        path
        for path, content in rendered.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    ]
    if check and stale:
        raise RuntimeError(
            "Workflow Pack v4 artifacts are stale: "
            + ", ".join(path.relative_to(ROOT).as_posix() for path in stale)
        )
    if not check:
        for path, content in rendered.items():
            path.write_text(content, encoding="utf-8")
    for name, schema in (
        ("pack.v4.json", "pack"),
        ("contracts.v4.json", "pack_contract_catalog"),
        ("artifact-index.v4.json", "pack_artifact_index"),
        ("executables.v4.json", "executable_catalog"),
    ):
        validate_document(rendered[PACK_ROOT / name], schema)
    return {"packs": 1, "contracts": 1, "operations": len(WORKFLOW_OPERATIONS)}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generate(check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
