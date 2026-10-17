"""Generate the finite v4 artifacts for the PackVM sandbox QA Pack."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from tobkiri_protocol.canonical import canonical_digest


PACK_ID = "tobkiri_packvm_sandbox_qa_pack"
CONTRACT_ID = "tobkiri.acceptance.packvm.sandbox.v1"
PROVIDER_ID = f"{PACK_ID}.probe"
FUNCTION_ID = f"{PACK_ID}.probe"
OPERATIONS = (
    "probe_isolation",
    "stdin_overflow",
    "stdout_overflow",
    "stderr_overflow",
    "deadline_hold",
    "cancel_hold",
    "abnormal_exit",
)
SOURCE_PATH = "acceptance/packvm_sandbox_qa_pack"
GENERATOR = "tobkiri.scripts.generate_packvm_sandbox_qa_pack"


def _json_bytes(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _provenance(source_identity: str, runtime_digest: str) -> dict[str, Any]:
    return {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "generated",
        "source_path": SOURCE_PATH,
        "source_digest": source_identity,
        "repository_commit": "working-tree",
        "repository_tree": "0" * 64,
        "generator": GENERATOR,
        "generator_version": "1.0.0",
        "normative": False,
        "evidence": [
            {
                "path": "runtime/probe.py",
                "rule_id": "packvm-sandbox-qa-runtime",
                "digest": runtime_digest,
            }
        ],
    }


def _operation_schema(operation: str) -> dict[str, Any]:
    properties: dict[str, Any] = {
        "nonce": {"type": "string", "pattern": "^[0-9a-f]{64}$"}
    }
    required = ["nonce"]
    if operation == "stdin_overflow":
        properties["fill"] = {"type": "string", "minLength": 1048577}
        required.append("fill")
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties,
        "required": required,
    }


def build_documents(root: Path) -> dict[str, dict[str, Any]]:
    """Build all generated v4 documents from the checked-in probe runtime."""

    runtime_digest = _file_digest(root / "runtime" / "probe.py")
    source_identity = canonical_digest(
        {
            "pack_id": PACK_ID,
            "contract_id": CONTRACT_ID,
            "operations": list(OPERATIONS),
            "runtime_digest": runtime_digest,
        }
    )
    revision_digest = canonical_digest(
        {
            "contract_id": CONTRACT_ID,
            "version": "1.0.0",
            "operations": list(OPERATIONS),
        }
    )
    error_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "code": {"type": "string"},
            "message": {"type": "string"},
        },
        "required": ["code", "message"],
    }
    output_schema = {"type": "object"}
    schema_catalog: dict[str, Any] = {}
    contract_operations = []
    executable_operations = []
    for operation in OPERATIONS:
        input_schema = _operation_schema(operation)
        input_digest = canonical_digest(input_schema)
        output_digest = canonical_digest(output_schema)
        error_digest = canonical_digest(error_schema)
        schema_catalog[input_digest] = input_schema
        schema_catalog[output_digest] = output_schema
        schema_catalog[error_digest] = error_schema
        # The Host deadline bounds the whole invoke, including guest-domain
        # materialization.  A cold PackVM boot takes tens of seconds, so the
        # budget must exceed it by a wide margin for dispatch to ever start.
        # ``deadline_hold`` children hold forever, so the original deadline
        # still fires deterministically while the guest holds the operation.
        timeout_default_ms = 120000
        timeout_hard_max_ms = timeout_default_ms
        operation_id = f"{PACK_ID}.{operation}"
        contract_operations.append(
            {
                "operation_id": operation_id,
                "input_schema_digest": input_digest,
                "output_schema_digest": output_digest,
                "error_schema_digest": error_digest,
                "effect_ceiling": ["operation.invoke"],
                "scope_semantics": "host_broker",
                "idempotency": {"mode": "none"},
                "timeout_default_ms": timeout_default_ms,
                "timeout_hard_max_ms": timeout_hard_max_ms,
            }
        )
        executable_operations.append(
            {
                "contract_id": CONTRACT_ID,
                "contract_version": "1.0.0",
                "revision_digest": revision_digest,
                "operation_id": operation_id,
                "input_schema": input_schema,
                "output_schema": output_schema,
                "error_schema": error_schema,
                "effect_class": "pure",
                "timeout_default_ms": timeout_default_ms,
                "timeout_hard_max_ms": timeout_hard_max_ms,
                "idempotency": "none",
            }
        )

    provenance = _provenance(source_identity, runtime_digest)
    contracts = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": PACK_ID,
        "source_identity": source_identity,
        "contracts": [
            {
                "contract_api_version": "io.tobkiri.contract.v4",
                "contract_id": CONTRACT_ID,
                "version": "1.0.0",
                "revision_digest": revision_digest,
                "owner": PACK_ID,
                "status": "accepted",
                "operations": contract_operations,
                "schema_catalog": schema_catalog,
                "provider_semantics": {
                    "provider_id": PROVIDER_ID,
                    "cardinality": "one",
                    "security": "restricted",
                    "failure": "fail_closed",
                    "isolation": "sandbox",
                    "required_capabilities": [],
                    "lifecycle": {"deprecated": False, "introduced": "1.0.0"},
                },
                "provenance": provenance,
            }
        ],
    }
    executables: dict[str, Any] = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "catalog_digest": "",
        "pack_id": PACK_ID,
        "source_identity": source_identity,
        "variants": [
            {
                "variant_id": f"{FUNCTION_ID}.python",
                "function_id": FUNCTION_ID,
                "implementation_path": "runtime/probe.py",
                "implementation_digest": runtime_digest,
                "execution_kind": "pack_vm",
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": "tobkiri.python-pack-v4",
                "materialization_mode": "on_demand",
                "execution_domain_profile": "sandbox.default.v1",
                "operations": executable_operations,
            }
        ],
    }
    executables["catalog_digest"] = canonical_digest(
        {key: value for key, value in executables.items() if key != "catalog_digest"}
    )
    executable_digest = "sha256:" + hashlib.sha256(_json_bytes(executables)).hexdigest()
    contract_digest = "sha256:" + hashlib.sha256(_json_bytes(contracts)).hexdigest()
    artifacts = [
        {
            "path": "executables.v4.json",
            "digest": executable_digest,
            "kind": "sidecar",
        },
        {
            "path": "runtime/probe.py",
            "digest": runtime_digest,
            "kind": "executable",
            "platform": "any",
            "entrypoint": "tobkiri_packvm_invoke",
        },
    ]
    artifact_set_digest = canonical_digest(artifacts)
    operation_ids = [f"{PACK_ID}.{operation}" for operation in OPERATIONS]
    pack = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": PACK_ID,
            "version": "1.0.0",
            "kind": "normal_sandbox",
            "artifact_digest": artifact_set_digest,
            "display_name": "Tobkiri PackVM Sandbox QA",
            "publisher_id": "dev.tobkiri.acceptance",
        },
        "functions": [
            {
                "id": FUNCTION_ID,
                "implementation_digest": runtime_digest,
                "contract_revision_digest": revision_digest,
                "operations": operation_ids,
                "role": "pure",
                "isolation": "dedicated_process",
            }
        ],
        "contracts": [
            {
                "contract_id": CONTRACT_ID,
                "revision_digest": revision_digest,
                "operations": operation_ids,
            }
        ],
        "artifacts": artifacts,
        "requirements": {
            "pack_dependencies": {},
            "contract_dependencies": [],
            "capabilities": [],
            "network": {"allowed_domains": [], "allowed_ports": []},
            "secrets": [],
            "execution_boundary": "sandbox",
            "approval_policy": "none",
            "workspace_boundary": "pack_local",
        },
        "operation_catalog": [
            {
                "operation_id": operation_id,
                "owner": PACK_ID,
                "contract_reference": CONTRACT_ID,
                "provider_id": PROVIDER_ID,
                "source_kind": "canonical_v4_contract",
                "effect_ceiling": ["operation.invoke"],
            }
            for operation_id in operation_ids
        ],
        "provider_catalog": [
            {
                "provider_id": PROVIDER_ID,
                "owner": PACK_ID,
                "contract_reference": CONTRACT_ID,
                "operations": operation_ids,
            }
        ],
        "integrity": {
            "source_identity": source_identity,
            "artifact_set_digest": artifact_set_digest,
            "contract_catalog_digest": contract_digest,
        },
        "provenance": provenance,
        "migration": {
            "compatibility": "none",
            "legacy_ids": [],
            "removal_wave": 0,
            "sunset_at": "2099-12-31",
        },
    }
    pack_digest = "sha256:" + hashlib.sha256(_json_bytes(pack)).hexdigest()
    index: dict[str, Any] = {
        "index_api_version": "io.tobkiri.pack-artifact-index.v4",
        "pack_id": PACK_ID,
        "source_identity": source_identity,
        "artifacts": [
            {"path": "pack.v4.json", "digest": pack_digest, "role": "canonical_manifest"},
            {"path": "contracts.v4.json", "digest": contract_digest, "role": "contract_catalog"},
            {"path": "executables.v4.json", "digest": executable_digest, "role": "sidecar"},
            {"path": "runtime/probe.py", "digest": runtime_digest, "role": "runtime"},
        ],
        "artifact_set_digest": artifact_set_digest,
        "integrity_seal": {"algorithm": "sha256-canonical-v1", "signed_digest": ""},
    }
    unsigned_index = {key: value for key, value in index.items() if key != "integrity_seal"}
    index["integrity_seal"]["signed_digest"] = canonical_digest(unsigned_index)
    return {
        "pack.v4.json": pack,
        "contracts.v4.json": contracts,
        "executables.v4.json": executables,
        "artifact-index.v4.json": index,
    }


def generate(root: Path, *, check: bool) -> None:
    """Write generated artifacts, or verify that checked-in bytes match."""

    documents = build_documents(root)
    mismatches = []
    for name, document in documents.items():
        path = root / name
        expected = _json_bytes(document)
        if check:
            if not path.is_file() or path.read_bytes() != expected:
                mismatches.append(name)
        else:
            path.write_bytes(expected)
    if mismatches:
        raise SystemExit(f"PackVM QA generated artifacts are stale: {', '.join(mismatches)}")


def main() -> int:
    """Generate or check the PackVM acceptance Pack artifacts."""

    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1]
        / "acceptance"
        / "packvm_sandbox_qa_pack",
    )
    args = parser.parse_args()
    generate(args.root.resolve(strict=True), check=args.check)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
