"""Public, offline authoring of capability-free Python Normal Packs.

This producer creates unsigned artifacts. It never imports Pack code, installs
a Pack, changes publisher policy, or creates runtime/activation authority.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import tempfile
from typing import Any, Mapping, Sequence

from tobkiri_host.artifact_compiler import compile_pack_root
from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_document


class PackAuthoringError(ValueError):
    """An authoring input cannot produce a closed capability-free Pack."""


@dataclass(frozen=True)
class PythonPackFunction:
    """An explicit Contract/Operation binding to captured UTF-8 Python bytes."""

    function_id: str
    contract_id: str
    operation_ids: tuple[str, ...]
    implementation_path: str
    source: bytes


_GENERATED = {
    "pack.v4.json",
    "contracts.v4.json",
    "artifact-index.v4.json",
    "executables.v4.json",
    "authoring-source.v1.json",
}
_MAX_FILE_BYTES = 1024 * 1024
_MAX_TOTAL_BYTES = 16 * _MAX_FILE_BYTES


def build_python_pack(
    target: Path,
    *,
    pack_id: str,
    version: str,
    display_name: str,
    contracts: Sequence[Mapping[str, Any]],
    functions: Sequence[PythonPackFunction],
    assets: Mapping[str, bytes] | None = None,
) -> Path:
    """Create a new unsigned Normal Pack and run the real artifact compiler.

    Contracts are canonical ``io.tobkiri.contract.v4`` documents. Every declared
    Operation must have exactly one explicit Function binding. This minimal
    producer supports pure PackVM Python Functions with no capabilities,
    network, secrets, external dependencies, or Host bridge requests.

    Inputs are captured bytes, never source paths. Existing destinations and
    symlink ancestors are rejected. No signing, policy, selection, activation,
    or code execution occurs. Schema/compiler errors propagate fail-closed.
    """

    target = Path(target).absolute()
    if ".." in target.parts or target.exists() or target.is_symlink():
        raise PackAuthoringError("target must be a new directory")
    for ancestor in (target.parent, *target.parent.parents):
        if ancestor.is_symlink():
            raise PackAuthoringError("target ancestors must not be symlinks")
    functions = tuple(sorted(functions, key=lambda item: item.function_id))
    captured_contracts = json.loads(_json_bytes(list(contracts)))
    contract_by_id: dict[str, dict[str, Any]] = {}
    for contract in captured_contracts:
        validate_document(contract, "contract")
        identity = contract["contract_id"]
        if identity in contract_by_id:
            raise PackAuthoringError("duplicate Contract identity")
        unsigned = {
            key: value
            for key, value in contract.items()
            if key not in {"revision_digest", "provenance"}
        }
        if canonical_digest(unsigned) != contract["revision_digest"]:
            raise PackAuthoringError("Contract revision digest is stale")
        semantics = contract["provider_semantics"]
        if (
            semantics["required_capabilities"]
            or semantics["isolation"] != "sandbox"
            or semantics["failure"] != "fail_closed"
        ):
            raise PackAuthoringError("producer requires capability-free sandbox Contracts")
        operation_ids = [op["operation_id"] for op in contract["operations"]]
        if len(set(operation_ids)) != len(operation_ids):
            raise PackAuthoringError("duplicate Contract Operation")
        for operation in contract["operations"]:
            if (
                set(operation["effect_ceiling"]) - {"pure"}
                or operation.get("scope_semantics") != "declarative"
            ):
                raise PackAuthoringError("producer requires pure declarative Operations")
        for digest, schema in contract["schema_catalog"].items():
            if canonical_digest(schema) != digest:
                raise PackAuthoringError("Contract schema digest is stale")
        contract_by_id[identity] = contract
    files: dict[str, bytes] = {}
    manifest_functions: list[dict[str, Any]] = []
    variants: list[dict[str, Any]] = []
    bound: set[tuple[str, str]] = set()
    function_ids: set[str] = set()
    implementation_digests: set[str] = set()
    operation_catalog: list[dict[str, Any]] = []
    for function in functions:
        if function.function_id in function_ids:
            raise PackAuthoringError("duplicate Function identity")
        function_ids.add(function.function_id)
        _add_file(files, function.implementation_path, function.source)
        _check_python_entry(function.source)
        digest = _file_digest(function.source)
        if digest in implementation_digests:
            raise PackAuthoringError("Function implementation digest must identify one file")
        implementation_digests.add(digest)
        contract = contract_by_id.get(function.contract_id)
        if contract is None or not function.operation_ids:
            raise PackAuthoringError("Function requires a declared Contract and Operations")
        operation_by_id = {op["operation_id"]: op for op in contract["operations"]}
        executable_operations = []
        for operation_id in function.operation_ids:
            key = (function.contract_id, operation_id)
            if key in bound or operation_id not in operation_by_id:
                raise PackAuthoringError("Function Operation binding is duplicate or unknown")
            bound.add(key)
            operation = operation_by_id[operation_id]
            operation_catalog.append(
                {
                    "operation_id": operation_id,
                    "owner": pack_id,
                    "contract_reference": function.contract_id,
                    "provider_id": function.function_id,
                    "source_kind": "canonical_v4_contract",
                    "effect_ceiling": operation["effect_ceiling"],
                }
            )
            default_ms = operation.get("timeout_default_ms", 30_000)
            hard_ms = operation.get("timeout_hard_max_ms", 300_000)
            if not 1 <= default_ms <= hard_ms <= 300_000:
                raise PackAuthoringError("Operation timeout exceeds Python Pack limits")
            schemas = contract["schema_catalog"]
            executable_operations.append(
                {
                    "contract_id": function.contract_id,
                    "contract_version": contract["version"],
                    "revision_digest": contract["revision_digest"],
                    "operation_id": operation_id,
                    **{
                        name: schemas[operation[name + "_digest"]]
                        for name in ("input_schema", "output_schema", "error_schema")
                    },
                    "effect_class": "pure",
                    "timeout_default_ms": default_ms,
                    "timeout_hard_max_ms": hard_ms,
                    "idempotency": operation.get("idempotency", {"mode": "none"})["mode"],
                }
            )
        manifest_functions.append(
            {
                "id": function.function_id,
                "implementation_digest": digest,
                "contract_revision_digest": contract["revision_digest"],
                "operations": list(function.operation_ids),
                "role": "pure",
                "isolation": "pack_vm",
            }
        )
        variants.append(
            {
                "variant_id": function.function_id + ".python",
                "function_id": function.function_id,
                "implementation_path": function.implementation_path,
                "implementation_digest": digest,
                "execution_kind": "pack_vm",
                "platform": "any",
                "architecture": "any",
                "runtime_abi": "python3.13",
                "backend": "tobkiri.python-pack-v4",
                "execution_domain_profile": "sandbox.default.v1",
                "materialization_mode": "on_demand",
                "operations": executable_operations,
            }
        )
    expected = {
        (identity, op["operation_id"])
        for identity, contract in contract_by_id.items()
        for op in contract["operations"]
    }
    if not expected or bound != expected:
        raise PackAuthoringError("Functions must cover all Contract Operations exactly once")
    for relative, content in sorted((assets or {}).items()):
        _add_file(files, relative, content)
    if len(files) > 256 or sum(map(len, files.values())) > _MAX_TOTAL_BYTES:
        raise PackAuthoringError("Pack authoring input exceeds size limits")
    captured_contracts.sort(key=lambda item: item["contract_id"])
    source = {
        "source_api_version": "io.tobkiri.python-pack-source.v1",
        "pack_id": pack_id,
        "version": version,
        "display_name": display_name,
        "contracts": captured_contracts,
        "functions": manifest_functions,
        "files": {name: _file_digest(value) for name, value in sorted(files.items())},
        "authority": "none",
    }
    source_identity = canonical_digest(source)
    files["authoring-source.v1.json"] = _json_bytes(source)
    contracts_document = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "contracts": captured_contracts,
    }
    executable_unsigned = {
        "catalog_api_version": "io.tobkiri.executable-catalog.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "variants": variants,
    }
    executable = {
        **executable_unsigned,
        "catalog_digest": canonical_digest(executable_unsigned),
    }
    files["executables.v4.json"] = _json_bytes(executable)
    runtime_paths = {item.implementation_path for item in functions}
    artifacts = [
        {
            "path": name,
            "digest": _file_digest(value),
            "kind": "executable" if name in runtime_paths else "sidecar",
        }
        for name, value in sorted(files.items())
    ]
    artifact_set_digest = canonical_digest(artifacts)
    provenance = {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "generated",
        "source_path": "authoring-source.v1.json",
        "source_digest": source_identity,
        "repository_commit": "working-tree",
        "repository_tree": source_identity.removeprefix("sha256:"),
        "generator": "tobkiri.core_runtime.pack_authoring",
        "generator_version": "1.0.0",
        "normative": True,
        "evidence": [
            {
                "path": "authoring-source.v1.json",
                "rule_id": "canonical-python-pack-authoring-source",
                "digest": source_identity,
            }
        ],
    }
    manifest = {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": pack_id,
            "version": version,
            "kind": "normal_sandbox",
            "artifact_digest": artifact_set_digest,
            "display_name": display_name,
        },
        "functions": manifest_functions,
        "contracts": [
            {
                "contract_id": item["contract_id"],
                "revision_digest": item["revision_digest"],
                "operations": [op["operation_id"] for op in item["operations"]],
            }
            for item in captured_contracts
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
        "operation_catalog": operation_catalog,
        "provider_catalog": [
            {
                "provider_id": function.function_id,
                "owner": pack_id,
                "contract_reference": function.contract_id,
                "operations": list(function.operation_ids),
            }
            for function in functions
        ],
        "integrity": {
            "source_identity": source_identity,
            "artifact_set_digest": artifact_set_digest,
            "contract_catalog_digest": _file_digest(_json_bytes(contracts_document)),
        },
        "provenance": provenance,
        "migration": {
            "compatibility": "none",
            "legacy_ids": [],
            "removal_wave": 0,
            "sunset_at": "2026-08-05",
        },
    }
    files["pack.v4.json"] = _json_bytes(manifest)
    files["contracts.v4.json"] = _json_bytes(contracts_document)
    unsigned_index = {
        "index_api_version": "io.tobkiri.pack-artifact-index.v4",
        "pack_id": pack_id,
        "source_identity": source_identity,
        "artifacts": [
            {
                "path": name,
                "digest": _file_digest(value),
                "role": (
                    "runtime"
                    if name in runtime_paths
                    else {
                        "pack.v4.json": "canonical_manifest",
                        "contracts.v4.json": "contract_catalog",
                    }.get(name, "sidecar")
                ),
            }
            for name, value in sorted(files.items())
        ],
        "artifact_set_digest": artifact_set_digest,
    }
    index = {
        **unsigned_index,
        "integrity_seal": {
            "algorithm": "sha256-canonical-v1",
            "signed_digest": canonical_digest(unsigned_index),
        },
    }
    files["artifact-index.v4.json"] = _json_bytes(index)
    for document, schema in (
        (manifest, "pack"),
        (contracts_document, "pack_contract_catalog"),
        (executable, "executable_catalog"),
        (index, "pack_artifact_index"),
    ):
        validate_document(document, schema)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".python-pack-", dir=target.parent) as value:
        staging = Path(value)
        for name, content in sorted(files.items()):
            path = staging / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        compile_pack_root(staging)
        # mkdir provides an exclusive destination claim; never replace another
        # author's existing directory (including an empty directory).
        target.mkdir()
        for child in list(staging.iterdir()):
            os.rename(child, target / child.name)
    return target


def _add_file(files: dict[str, bytes], name: str, content: bytes) -> None:
    path = PurePosixPath(name)
    if (
        not name
        or path.is_absolute()
        or path.as_posix() != name
        or any(part in {"..", ".tobkiri"} for part in path.parts)
        or "\\" in name
        or any(ord(char) < 32 for char in name)
        or name in _GENERATED
        or name in files
        or not isinstance(content, bytes)
        or len(content) > _MAX_FILE_BYTES
    ):
        raise PackAuthoringError("artifact path or captured bytes are invalid")
    files[name] = content


def _check_python_entry(source: bytes) -> None:
    module = ast.parse(source.decode("utf-8"))
    entries = [
        node
        for node in module.body
        if isinstance(node, ast.FunctionDef) and node.name == "tobkiri_packvm_invoke"
    ]
    if len(entries) != 1:
        raise PackAuthoringError("Python module must define tobkiri_packvm_invoke")
    entry = entries[0]
    if (
        len(entry.args.posonlyargs) + len(entry.args.args) != 2
        or entry.args.vararg
        or entry.args.kwarg
        or entry.args.kwonlyargs
    ):
        raise PackAuthoringError("Python Pack entry must accept operation_id and payload")


def _file_digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
