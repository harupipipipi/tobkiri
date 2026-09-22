#!/usr/bin/env python3
"""Generate exact executable catalogs for every canonical production Pack.

The generator consumes only the three verified v4 authority artifacts emitted by
``migrate_pack_artifacts_v4.py``.  It never scans source files for entrypoints and
never guesses a Function/Contract/Operation mapping.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tobkiri_protocol.canonical import canonical_digest  # noqa: E402
from tobkiri_protocol.validation import validate_document  # noqa: E402


ECOSYSTEM = ROOT / "ecosystem"


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _text(value: dict[str, Any]) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _one(items: list[dict[str, Any]], label: str, pack_id: str) -> dict[str, Any]:
    if len(items) != 1:
        raise ValueError(f"{label} must resolve exactly once for {pack_id}; found {len(items)}")
    return items[0]


def _effect_class(operation: dict[str, Any]) -> str:
    """Return the conservative executable admission class for a Contract operation."""
    effects = tuple(str(item) for item in operation["effect_ceiling"])
    if any(item.startswith(("host:", "secret:")) for item in effects):
        return "privileged"
    if any(item.startswith("network:") for item in effects):
        return "external_effect"
    if any("write" in item or "mutat" in item or "delete" in item for item in effects):
        return "write"
    if effects:
        return "read"
    return "pure"


def _execution_metadata(manifest: dict[str, Any], function: dict[str, Any]) -> dict[str, str]:
    isolation = function["isolation"]
    if manifest["pack"]["kind"] == "host_extension":
        execution_kind = "host_extension"
        backend = "tobkiri.python-host-v4"
        domain = "host.extension.default.v1"
    elif isolation == "remote":
        execution_kind = "remote"
        backend = "tobkiri.remote-pack-v4"
        domain = "remote.default.v1"
    else:
        execution_kind = "pack_vm"
        backend = "tobkiri.python-pack-v4"
        domain = "sandbox.default.v1"
    return {
        "execution_kind": execution_kind,
        "platform": "any",
        "architecture": "any",
        "runtime_abi": "python3.13",
        "backend": backend,
        "materialization_mode": "on_demand",
        "execution_domain_profile": domain,
    }


def _render_document(
    pack_id: str,
    root: Path,
    manifest: dict[str, Any],
    contracts: dict[str, Any],
    index: dict[str, Any],
) -> dict[str, Any]:
    """Render one catalog from already-rendered canonical v4 documents."""
    runtime_paths: dict[str, list[str]] = {}
    for artifact in index["artifacts"]:
        if artifact["role"] == "runtime":
            runtime_paths.setdefault(artifact["digest"], []).append(artifact["path"])

    variants: list[dict[str, Any]] = []
    for function in manifest["functions"]:
        implementation_path = _one(
            [
                {"path": path}
                for path in runtime_paths.get(function["implementation_digest"], [])
            ],
            f"implementation path for Function {function['id']}",
            pack_id,
        )["path"]
        implementation = root / implementation_path
        digest = _file_digest(implementation)
        if digest != function["implementation_digest"]:
            raise ValueError(f"canonical implementation digest is stale: {pack_id}")
        contract = _one(
            [
                item
                for item in contracts["contracts"]
                if item["revision_digest"] == function["contract_revision_digest"]
            ],
            f"Contract revision for Function {function['id']}",
            pack_id,
        )
        operations: list[dict[str, Any]] = []
        for operation_id in function["operations"]:
            operation = _one(
                [
                    item
                    for item in contract["operations"]
                    if item["operation_id"] == operation_id
                ],
                f"Operation {operation_id}",
                pack_id,
            )
            schemas = contract["schema_catalog"]
            operations.append(
                {
                    "contract_id": contract["contract_id"],
                    "contract_version": contract["version"],
                    "revision_digest": contract["revision_digest"],
                    "operation_id": operation_id,
                    "input_schema": schemas[operation["input_schema_digest"]],
                    "output_schema": schemas[operation["output_schema_digest"]],
                    "error_schema": schemas[operation["error_schema_digest"]],
                    "effect_class": _effect_class(operation),
                    "timeout_default_ms": 30_000,
                    "timeout_hard_max_ms": 300_000,
                    "idempotency": operation["idempotency"]["mode"],
                }
            )
        variants.append(
            {
                "variant_id": f"{function['id']}.python",
                "function_id": function["id"],
                "implementation_path": implementation_path,
                "implementation_digest": digest,
                **_execution_metadata(manifest, function),
                "operations": operations,
            }
        )
    unsigned = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "pack_id": pack_id,
        "source_identity": manifest["integrity"]["source_identity"],
        "variants": variants,
    }
    document = {**unsigned, "catalog_digest": canonical_digest(unsigned)}
    return validate_document(document, "executable_catalog")


def _render(pack_id: str) -> dict[str, Any]:
    root = ECOSYSTEM / pack_id
    manifest = json.loads((root / "pack.v4.json").read_text(encoding="utf-8"))
    contracts = json.loads((root / "contracts.v4.json").read_text(encoding="utf-8"))
    index = json.loads((root / "artifact-index.v4.json").read_text(encoding="utf-8"))
    return _render_document(pack_id, root, manifest, contracts, index)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    pack_ids = sorted(
        path.parent.name for path in ECOSYSTEM.glob("*/pack.v4.json")
    )
    stale: list[Path] = []
    for pack_id in pack_ids:
        path = ECOSYSTEM / pack_id / "executables.v4.json"
        text = _text(_render(pack_id))
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            stale.append(path)
            if not args.check:
                path.write_text(text, encoding="utf-8")
    if args.check and stale:
        for path in stale:
            print(path.relative_to(ROOT))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
