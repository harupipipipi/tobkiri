"""Compile verified Pack v4 files into the in-memory Host execution catalog."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from tobkiri_protocol.canonical import canonical_digest
from tobkiri_protocol.validation import validate_file

from .contracts import OperationRoute
from .errors import InvalidArtifactError, ResolutionError
from .models import (
    ArtifactVariant,
    ContractOperation,
    EffectClass,
    ExecutionKind,
    FunctionArtifact,
    OpaqueAuthorityRef,
    PackArtifact,
    PackageKind,
)


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class CompiledPack:
    """One completely verified PackArtifact plus its route metadata."""

    artifact: PackArtifact
    routes: Mapping[tuple[str, str], Mapping[str, Any]]


def _expected_execution_kind(
    manifest: Mapping[str, Any], function: Mapping[str, Any]
) -> ExecutionKind:
    """Return the only execution kind admitted by Pack/function metadata."""
    if manifest["pack"]["kind"] == "host_extension":
        return ExecutionKind.HOST_EXTENSION
    if function.get("isolation") == "wasm_component":
        return ExecutionKind.WASM
    if (
        function.get("isolation") == "remote"
        or manifest["requirements"]["execution_boundary"] == "remote"
    ):
        return ExecutionKind.REMOTE
    return ExecutionKind.PACK_VM


def compile_pack_root(pack_root: Path) -> CompiledPack:
    """Compile one canonical Pack root without imports, aliases, or discovery."""
    root = pack_root.resolve(strict=True)
    manifest = validate_file(root / "pack.v4.json", "pack")
    contracts = validate_file(root / "contracts.v4.json", "pack_contract_catalog")
    index = validate_file(root / "artifact-index.v4.json", "pack_artifact_index")
    executable = validate_file(root / "executables.v4.json", "executable_catalog")
    pack_id = manifest["pack"]["id"]
    if {
        contracts["pack_id"],
        index["pack_id"],
        executable["pack_id"],
    } != {pack_id}:
        raise InvalidArtifactError("v4 artifact documents disagree on Pack identity")
    source_identity = manifest["integrity"]["source_identity"]
    if {
        contracts["source_identity"],
        index["source_identity"],
        executable["source_identity"],
    } != {source_identity}:
        raise InvalidArtifactError("v4 artifact documents disagree on source identity")
    expected_catalog_digest = canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    )
    if executable["catalog_digest"] != expected_catalog_digest:
        raise InvalidArtifactError("executable catalog digest changed")

    expected_artifact_digest = canonical_digest(manifest["artifacts"])
    if (
        manifest["pack"]["artifact_digest"] != expected_artifact_digest
        or manifest["integrity"]["artifact_set_digest"] != expected_artifact_digest
        or index["artifact_set_digest"] != expected_artifact_digest
    ):
        raise InvalidArtifactError("Pack artifact set digest is stale")
    index_entries = [item for item in index["artifacts"] if item["path"]]
    if len({item["path"] for item in index_entries}) != len(index_entries):
        raise InvalidArtifactError("artifact index contains duplicate paths")
    index_by_path = {item["path"]: item for item in index_entries}
    executable_entry = index_by_path.get("executables.v4.json")
    manifest_executable_entries = [
        item for item in manifest["artifacts"] if item["path"] == "executables.v4.json"
    ]
    if executable_entry is None or executable_entry["role"] != "sidecar":
        raise InvalidArtifactError("artifact index does not pin executable catalog")
    if len(manifest_executable_entries) != 1:
        raise InvalidArtifactError("Pack manifest does not pin executable catalog")
    executable_digest = _file_digest(root / "executables.v4.json")
    if (
        executable_entry["digest"] != executable_digest
        or manifest_executable_entries[0]["digest"] != executable_digest
    ):
        raise InvalidArtifactError("executable catalog artifact digest mismatch")
    unsigned_index = {key: value for key, value in index.items() if key != "integrity_seal"}
    if index["integrity_seal"]["signed_digest"] != canonical_digest(unsigned_index):
        raise InvalidArtifactError("artifact index integrity seal is invalid")

    index_runtime = {
        item["path"]: item["digest"]
        for item in index["artifacts"]
        if item["role"] == "runtime"
    }
    declared_functions = {item["id"]: item for item in manifest["functions"]}
    contract_documents = {item["contract_id"]: item for item in contracts["contracts"]}
    functions: list[FunctionArtifact] = []
    variants: list[ArtifactVariant] = []
    route_metadata: dict[tuple[str, str], Mapping[str, Any]] = {}
    seen_functions: set[str] = set()
    seen_variants: set[str] = set()
    for variant in executable["variants"]:
        function_id = variant["function_id"]
        function = declared_functions.get(function_id)
        if function is None or function_id in seen_functions or variant["variant_id"] in seen_variants:
            raise InvalidArtifactError("executable variant has unknown or duplicate Function")
        seen_functions.add(function_id)
        seen_variants.add(variant["variant_id"])
        domain_kind = function.get("isolation", "pack_vm")
        if domain_kind not in {"wasm_component", "pack_vm", "dedicated_process", "remote"}:
            raise InvalidArtifactError("executable variant has invalid domain kind")
        execution_kind = ExecutionKind(variant["execution_kind"])
        if execution_kind != _expected_execution_kind(manifest, function):
            raise InvalidArtifactError("executable execution kind does not match Pack")
        implementation_path = variant["implementation_path"]
        implementation = (root / implementation_path).resolve(strict=True)
        if root not in implementation.parents or not implementation.is_file():
            raise InvalidArtifactError("executable implementation escapes its Pack root")
        digest = _file_digest(implementation)
        if (
            digest != variant["implementation_digest"]
            or digest != function["implementation_digest"]
            or index_runtime.get(implementation_path) != digest
        ):
            raise InvalidArtifactError("executable implementation digest mismatch")

        compiled_operations: list[ContractOperation] = []
        catalog_operation_ids = {
            operation["operation_id"] for operation in variant["operations"]
        }
        if catalog_operation_ids != set(function["operations"]):
            raise InvalidArtifactError("executable variant Operation inventory mismatch")
        for operation in variant["operations"]:
            contract = contract_documents.get(operation["contract_id"])
            if contract is None or contract["revision_digest"] != operation["revision_digest"]:
                raise InvalidArtifactError("executable Contract revision mismatch")
            declared = [
                item
                for item in contract["operations"]
                if item["operation_id"] == operation["operation_id"]
            ]
            if len(declared) != 1 or operation["operation_id"] not in function["operations"]:
                raise InvalidArtifactError("executable Operation is not declared exactly once")
            source = declared[0]
            schemas = contract["schema_catalog"]
            if (
                canonical_digest(operation["input_schema"])
                != source["input_schema_digest"]
                or canonical_digest(operation["output_schema"])
                != source["output_schema_digest"]
                or canonical_digest(operation["error_schema"])
                != source["error_schema_digest"]
                or schemas.get(source["input_schema_digest"]) != operation["input_schema"]
                or schemas.get(source["output_schema_digest"]) != operation["output_schema"]
                or schemas.get(source["error_schema_digest"]) != operation["error_schema"]
            ):
                raise InvalidArtifactError("executable Operation schema digest mismatch")
            compiled_operations.append(
                ContractOperation(
                    contract_id=operation["contract_id"],
                    contract_version=operation["contract_version"],
                    revision_digest=operation["revision_digest"],
                    operation_id=operation["operation_id"],
                    input_schema=operation["input_schema"],
                    output_schema=operation["output_schema"],
                    error_schema=operation["error_schema"],
                    effect_class=EffectClass(operation["effect_class"]),
                    timeout_default_ms=operation["timeout_default_ms"],
                    timeout_hard_max_ms=operation["timeout_hard_max_ms"],
                    idempotency=operation["idempotency"],
                )
            )
            route_key = (operation["contract_id"], operation["operation_id"])
            if route_key in route_metadata:
                raise InvalidArtifactError(
                    "executable Operation mapping is duplicated or unqualified"
                )
            route_metadata[route_key] = {
                "function_id": function_id,
                "variant_id": variant["variant_id"],
                "materialization_mode": variant["materialization_mode"],
                "execution_domain_profile": variant["execution_domain_profile"],
                "catalog_digest": executable["catalog_digest"],
                "platform": variant["platform"],
                "architecture": variant["architecture"],
                "runtime_abi": variant["runtime_abi"],
                "backend": variant["backend"],
                "execution_kind": execution_kind.value,
                "domain_kind": domain_kind,
            }
        functions.append(
            FunctionArtifact(
                function_id=function_id,
                implementation_digest=digest,
                variant_id=variant["variant_id"],
                operations=tuple(compiled_operations),
            )
        )
        variants.append(
            ArtifactVariant(
                variant_id=variant["variant_id"],
                digest=digest,
                execution_kind=execution_kind,
                os=variant["platform"],
                architecture=variant["architecture"],
                runtime_abi=variant["runtime_abi"],
                backend=variant["backend"],
                domain_kind=domain_kind,
            )
        )
    if seen_functions != set(declared_functions):
        raise InvalidArtifactError("not every declared Function has an executable variant")
    return CompiledPack(
        artifact=PackArtifact(
            pack_id=pack_id,
            version=manifest["pack"]["version"],
            digest=manifest["pack"]["artifact_digest"],
            publisher_lineage=manifest["pack"].get("publisher_id", "tobkiri.repository"),
            package_kind=(
                PackageKind.HOST_EXTENSION
                if manifest["pack"]["kind"] == "host_extension"
                else PackageKind.NORMAL
            ),
            functions=tuple(functions),
            variants=tuple(variants),
            catalog_digest=executable["catalog_digest"],
        ),
        routes=route_metadata,
    )


def routes_for_plan(
    plan: Mapping[str, Any], compiled: Sequence[CompiledPack]
) -> tuple[OperationRoute, ...]:
    """Construct routes only for exact bindings already pinned by ResolvedPlan."""
    by_digest = {item.artifact.digest: item for item in compiled}
    if len(by_digest) != len(compiled):
        raise ResolutionError("duplicate compiled artifact digest")
    routes: list[OperationRoute] = []
    for binding in plan["bindings"]:
        item = by_digest.get(binding["artifact_digest"])
        if item is None:
            raise ResolutionError("ResolvedPlan binding lacks a verified artifact")
        if binding["pack_id"] != item.artifact.pack_id:
            raise ResolutionError("ResolvedPlan binding Pack identity is stale")
        key = (binding["contract_id"], binding["operation_id"])
        metadata = item.routes.get(key)
        if metadata is None:
            raise ResolutionError("ResolvedPlan binding lacks executable metadata")
        principal = binding["function_principal"]
        function = item.artifact.function(principal["function_id"])
        if (
            principal["parent_artifact_digest"] != item.artifact.digest
            or principal["function_implementation_digest"] != function.implementation_digest
            or principal["contract_revision_digest"]
            != next(
                (
                    operation.revision_digest
                    for operation in function.operations
                    if operation.contract_id == binding["contract_id"]
                    and operation.operation_id == binding["operation_id"]
                ),
                None,
            )
            or principal["operation_id"] != binding["operation_id"]
        ):
            raise ResolutionError("ResolvedPlan Function principal is stale")
        if principal["function_id"] != metadata["function_id"]:
            raise ResolutionError("ResolvedPlan binding Function is not the catalog route")
        pin_fields = (
            "executable_catalog_digest",
            "variant_id",
            "platform",
            "architecture",
            "runtime_abi",
            "backend",
            "execution_kind",
            "domain_kind",
        )
        expected_pin = {
            "executable_catalog_digest": metadata["catalog_digest"],
            "variant_id": metadata["variant_id"],
            "platform": metadata["platform"],
            "architecture": metadata["architecture"],
            "runtime_abi": metadata["runtime_abi"],
            "backend": metadata["backend"],
            "execution_kind": metadata["execution_kind"],
            "domain_kind": metadata["domain_kind"],
        }
        if any(field not in binding for field in pin_fields) or any(
            binding[field] != value for field, value in expected_pin.items()
        ):
            raise ResolutionError("ResolvedPlan executable variant pin is stale")
        routes.append(
            OperationRoute(
                contract_id=binding["contract_id"],
                operation_id=binding["operation_id"],
                artifact_digest=binding["artifact_digest"],
                function_id=principal["function_id"],
                variant_id=metadata["variant_id"],
                execution_domain_profile=metadata["execution_domain_profile"],
                materialization_mode=metadata["materialization_mode"],
                target_principal_ref=OpaqueAuthorityRef(canonical_digest(principal)),
                catalog_digest=metadata["catalog_digest"],
                platform=metadata["platform"],
                architecture=metadata["architecture"],
                runtime_abi=metadata["runtime_abi"],
                backend=metadata["backend"],
                execution_kind=metadata["execution_kind"],
                domain_kind=metadata["domain_kind"],
            )
        )
    return tuple(routes)


__all__ = ["CompiledPack", "compile_pack_root", "routes_for_plan"]
