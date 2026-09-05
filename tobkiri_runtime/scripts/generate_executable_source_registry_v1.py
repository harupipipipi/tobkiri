#!/usr/bin/env python3
"""Generate the executable source registry from legacy-shaped inputs.

The v4 executable catalogs are intentionally not inputs to this generator.
Legacy ``rumi.pack.v3.json`` entrypoints provide the base Function/Operation
mapping, while the small checked-in fixture records semantic additions that
did not exist in v3.  Runtime bytes and their legacy artifact manifests are
hashed independently so an operation cannot be admitted from a stale digest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[1]
ECOSYSTEM = ROOT / "ecosystem"
DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_executable_sources.v1.json"
DEFAULT_OUTPUT = ROOT / "schemas" / "executable_sources.v1.json"
GENERATOR = "tobkiri.scripts.generate_executable_source_registry_v1"
GENERATOR_VERSION = "1.0.0"


class ExecutableSourceRegistryError(ValueError):
    """Raised when legacy executable source inputs are incomplete or ambiguous."""


def _load_json(path: Path) -> Mapping[str, Any]:
    """Load one JSON object and retain the input path in raised errors."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutableSourceRegistryError(f"cannot read JSON input: {path}") from exc
    if not isinstance(value, Mapping):
        raise ExecutableSourceRegistryError(f"JSON input must be an object: {path}")
    return value


def _canonical_id(value: str) -> str:
    """Apply the stable legacy-to-v4 identifier normalization."""

    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("._-")
    normalized = re.sub(r"[-_.]{2,}", ".", normalized)
    if not normalized or not normalized[0].isalpha():
        normalized = f"id.{normalized or 'unknown'}"
    return normalized


def _contract_id(value: str) -> str:
    """Map a legacy Contract namespace into its v4 compatibility namespace."""

    if value.startswith("rumi."):
        return "tobkiri." + value.removeprefix("rumi.")
    if value.startswith(("rumiai.", "viewer.", "legacy.")):
        return "tobkiri.migrated." + value.split(".", 1)[1]
    return value


def _file_digest(path: Path) -> str:
    """Return the raw SHA-256 digest used by the executable registry."""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: Any) -> str:
    """Return a deterministic digest for registry provenance metadata."""

    encoded = json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _json_text(value: Mapping[str, Any]) -> str:
    """Render one deterministic registry document."""

    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _label(path: Path, repository_root: Path) -> str:
    """Return a stable repository-relative input label."""

    try:
        return path.resolve().relative_to(repository_root.resolve()).as_posix()
    except ValueError:
        return f"external:{path.resolve().as_posix()}"


def _resolve_pack_file(
    pack_root: Path,
    relative_path: Path,
    *,
    source_label: str,
) -> Path:
    """Resolve a regular Pack file while rejecting every symlink component."""

    pack_root = Path(pack_root)
    if pack_root.is_symlink():
        raise ExecutableSourceRegistryError(
            f"{source_label} uses a symlink Pack root: {pack_root}"
        )
    try:
        resolved_pack_root = pack_root.resolve(strict=True)
    except OSError as exc:
        raise ExecutableSourceRegistryError(
            f"{source_label} Pack root cannot be resolved: {pack_root}"
        ) from exc
    if not resolved_pack_root.is_dir():
        raise ExecutableSourceRegistryError(
            f"{source_label} Pack root is not a directory: {pack_root}"
        )

    relative_path = Path(relative_path)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ExecutableSourceRegistryError(
            f"{source_label} escapes its Pack: {pack_root.name}:{relative_path}"
        )

    candidate = resolved_pack_root
    for part in relative_path.parts:
        candidate /= part
        if candidate.is_symlink():
            raise ExecutableSourceRegistryError(
                f"{source_label} contains a symlink: {pack_root.name}:{relative_path}"
            )
    try:
        resolved_candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ExecutableSourceRegistryError(
            f"{source_label} is missing: {candidate}"
        ) from exc
    try:
        resolved_candidate.relative_to(resolved_pack_root)
    except ValueError as exc:
        raise ExecutableSourceRegistryError(
            f"{source_label} escapes its Pack: {pack_root.name}:{relative_path}"
        ) from exc
    if not resolved_candidate.is_file():
        raise ExecutableSourceRegistryError(
            f"{source_label} is not a regular file: {resolved_candidate}"
        )
    return resolved_candidate


def _module_path(pack_root: Path, module: str) -> Path:
    """Resolve one v3 Python module without allowing it to escape its Pack."""

    prefix = f"ecosystem.{pack_root.name}."
    if not module.startswith(prefix):
        raise ExecutableSourceRegistryError(
            f"v3 entrypoint module is outside its Pack: {pack_root.name}:{module}"
        )
    relative_module = module.removeprefix(prefix)
    relative_path = Path(*relative_module.split(".")).with_suffix(".py")
    return _resolve_pack_file(
        pack_root,
        relative_path,
        source_label="v3 entrypoint module",
    )


def _relative_runtime_path(pack_root: Path, value: str) -> Path:
    """Resolve an explicit fixture runtime path under one Pack."""

    relative = Path(value)
    return _resolve_pack_file(
        pack_root,
        relative,
        source_label="explicit implementation path",
    )


def _legacy_contracts(v3: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Index explicitly provided v3 Contracts by their legacy IDs."""

    contracts_value = v3.get("contracts")
    contracts = contracts_value.get("provides", []) if isinstance(contracts_value, Mapping) else []
    if not isinstance(contracts, list):
        raise ExecutableSourceRegistryError("v3 provided Contracts must be a list")
    result: dict[str, Mapping[str, Any]] = {}
    for contract in contracts:
        if not isinstance(contract, Mapping):
            raise ExecutableSourceRegistryError("v3 provided Contract is not an object")
        contract_id = contract.get("id")
        if not isinstance(contract_id, str) or not contract_id.strip():
            raise ExecutableSourceRegistryError("v3 provided Contract lacks an ID")
        if contract_id in result:
            raise ExecutableSourceRegistryError(f"duplicate v3 Contract: {contract_id}")
        result[contract_id] = contract
    return result


def _schema_fields(contract: Mapping[str, Any]) -> dict[str, Any]:
    """Return the legacy Contract schemas in the registry compatibility shape."""

    schemas = contract.get("schemas")
    schemas = schemas if isinstance(schemas, Mapping) else {}
    return {
        "input_schema": schemas.get("input", {"type": "object"}),
        "output_schema": schemas.get("output", schemas.get("event", {"type": "object"})),
        "error_schema": schemas.get("error", {"type": "object"}),
    }


def _new_record(
    *,
    pack_id: str,
    function_id: str,
    contract_id: str,
    contract_version: str,
    implementation_path: str,
    implementation_digest: str,
    schemas: Mapping[str, Any],
    source: Mapping[str, Any],
) -> dict[str, Any]:
    """Create one mutable Function source record."""

    return {
        "pack_id": pack_id,
        "owner": pack_id,
        "function_id": function_id,
        "contract_id": contract_id,
        "contract_version": contract_version,
        "implementation_path": implementation_path,
        "implementation_digest": implementation_digest,
        "variant_id": f"{function_id}.python",
        "execution_kind": "source-verified",
        "source": [dict(source)],
        **dict(schemas),
        "operations": [],
    }


def _add_operation(
    records: dict[str, dict[str, Any]],
    *,
    pack_id: str,
    function_id: str,
    operation_id: str,
    contract_id: str,
    contract_version: str,
    implementation_path: Path,
    pack_root: Path,
    schemas: Mapping[str, Any],
    source: Mapping[str, Any],
) -> None:
    """Add one operation and reject all ambiguous identity collisions."""

    if not operation_id.strip():
        raise ExecutableSourceRegistryError("operation ID must not be empty")
    implementation_digest = _file_digest(implementation_path)
    relative_path = implementation_path.relative_to(pack_root).as_posix()
    record = records.get(function_id)
    if record is None:
        record = _new_record(
            pack_id=pack_id,
            function_id=function_id,
            contract_id=contract_id,
            contract_version=contract_version,
            implementation_path=relative_path,
            implementation_digest=implementation_digest,
            schemas=schemas,
            source=source,
        )
        records[function_id] = record
    else:
        if record["pack_id"] != pack_id or record["owner"] != pack_id:
            raise ExecutableSourceRegistryError(f"Function owner conflict: {function_id}")
        if record["implementation_path"] != relative_path or record["implementation_digest"] != implementation_digest:
            raise ExecutableSourceRegistryError(f"Function implementation conflict: {function_id}")
        if record["contract_id"] != contract_id or record["contract_version"] != contract_version:
            raise ExecutableSourceRegistryError(f"Function Contract conflict: {function_id}")
        if any(record.get(key) != value for key, value in schemas.items()):
            raise ExecutableSourceRegistryError(f"Function schema conflict: {function_id}")
        record["source"].append(dict(source))

    existing = {
        str(operation.get("operation_id"))
        for operation in record["operations"]
        if isinstance(operation, Mapping)
    }
    if operation_id in existing:
        raise ExecutableSourceRegistryError(
            f"duplicate executable Operation: {function_id}:{operation_id}"
        )
    record["operations"].append(
        {
            "operation_id": operation_id,
            "contract_id": contract_id,
            "contract_version": contract_version,
            "implementation_path": relative_path,
            "implementation_digest": implementation_digest,
            **dict(schemas),
            **dict(source),
        }
    )


def _v3_records(
    pack_root: Path,
    *,
    operation_overrides: Mapping[tuple[str, str], str],
    function_overrides: Mapping[tuple[str, str], str],
    repository_root: Path,
) -> dict[str, dict[str, Any]]:
    """Build Function records from one legacy v3 manifest and its bytes."""

    v3_path = pack_root / "rumi.pack.v3.json"
    v3 = _load_json(v3_path)
    contracts = _legacy_contracts(v3)
    entrypoints = v3.get("entrypoints", [])
    if not isinstance(entrypoints, list):
        raise ExecutableSourceRegistryError(f"v3 entrypoints must be a list: {v3_path}")
    artifact_manifest_path = pack_root / "artifact-manifest.json"
    artifact_manifest = _load_json(artifact_manifest_path)
    artifacts = artifact_manifest.get("artifacts", [])
    declared_digests = {
        str(item.get("path")): (
            str(item.get("sha256"))
            if str(item.get("sha256")).startswith("sha256:")
            else "sha256:" + str(item.get("sha256"))
        )
        for item in artifacts
        if isinstance(item, Mapping) and item.get("path") and item.get("sha256")
    }
    records: dict[str, dict[str, Any]] = {}
    seen_entrypoints: set[str] = set()
    for entrypoint in entrypoints:
        if not isinstance(entrypoint, Mapping):
            raise ExecutableSourceRegistryError(f"v3 entrypoint is not an object: {v3_path}")
        entrypoint_id = entrypoint.get("id")
        contract_id = entrypoint.get("contract_id")
        module = entrypoint.get("module")
        if not all(isinstance(value, str) and value.strip() for value in (entrypoint_id, contract_id, module)):
            raise ExecutableSourceRegistryError(f"v3 entrypoint identity is incomplete: {v3_path}")
        if entrypoint_id in seen_entrypoints:
            raise ExecutableSourceRegistryError(
                f"duplicate v3 entrypoint: {pack_root.name}:{entrypoint_id}"
            )
        seen_entrypoints.add(entrypoint_id)
        contract = contracts.get(contract_id)
        if contract is None:
            raise ExecutableSourceRegistryError(
                f"v3 entrypoint Contract is not provided: {pack_root.name}:{contract_id}"
            )
        provider_instance = contract.get("provider_instance_id") or contract.get("provider_instance")
        if not isinstance(provider_instance, str) or not provider_instance.strip():
            raise ExecutableSourceRegistryError(
                f"v3 Contract lacks provider instance: {pack_root.name}:{contract_id}"
            )
        implementation = _module_path(pack_root, module)
        relative_path = implementation.relative_to(pack_root).as_posix()
        actual_digest = _file_digest(implementation)
        declared_digest = declared_digests.get(relative_path)
        if declared_digest != actual_digest:
            raise ExecutableSourceRegistryError(
                f"legacy artifact digest is stale: {pack_root.name}:{relative_path}"
            )
        function_id = function_overrides.get(
            (pack_root.name, entrypoint_id),
            _canonical_id(f"{pack_root.name}.{provider_instance}"),
        )
        operation_id = operation_overrides.get(
            (pack_root.name, entrypoint_id),
            _canonical_id(f"{pack_root.name}.{entrypoint_id}"),
        )
        canonical_contract_id = _contract_id(contract_id)
        _add_operation(
            records,
            pack_id=pack_root.name,
            function_id=function_id,
            operation_id=operation_id,
            contract_id=canonical_contract_id,
            contract_version=str(contract.get("version") or "1.0.0"),
            implementation_path=implementation,
            pack_root=pack_root,
            schemas=_schema_fields(contract),
            source={
                "kind": "legacy-v3-entrypoint",
                "path": _label(v3_path, repository_root),
                "entrypoint_id": entrypoint_id,
                "module": module,
                "symbol": str(entrypoint.get("symbol") or ""),
            },
        )
    return records


def _fixture_contract(
    entry: Mapping[str, Any],
    *,
    pack_root: Path,
    repository_root: Path,
) -> tuple[str, str, dict[str, Any]]:
    """Resolve an explicit fixture Contract without consulting v4 artifacts."""

    raw_contract_id = entry.get("contract_id")
    if not isinstance(raw_contract_id, str) or not raw_contract_id.strip():
        raise ExecutableSourceRegistryError("explicit source entry lacks contract_id")
    v3_path = pack_root / "rumi.pack.v3.json"
    contract: Mapping[str, Any] | None = None
    if v3_path.is_file():
        contract = _legacy_contracts(_load_json(v3_path)).get(raw_contract_id)
        if contract is None:
            contract = next(
                (
                    value
                    for key, value in _legacy_contracts(_load_json(v3_path)).items()
                    if _contract_id(key) == raw_contract_id
                ),
                None,
            )
    schemas = entry.get("schemas")
    if isinstance(schemas, Mapping):
        schema_fields = {
            "input_schema": schemas.get("input", {"type": "object"}),
            "output_schema": schemas.get("output", {"type": "object"}),
            "error_schema": schemas.get("error", {"type": "object"}),
        }
    elif contract is not None:
        schema_fields = _schema_fields(contract)
    else:
        schema_fields = _schema_fields({})
    version = str(entry.get("contract_version") or (contract or {}).get("version") or "1.0.0")
    del repository_root
    return _contract_id(raw_contract_id), version, schema_fields


def _fixture_records(
    ecosystem_root: Path,
    fixture_path: Path,
    *,
    repository_root: Path,
) -> tuple[
    dict[str, dict[str, Any]],
    dict[tuple[str, str], str],
    dict[tuple[str, str], str],
]:
    """Build explicit additions and legacy entrypoint overrides from the fixture."""

    fixture = _load_json(fixture_path)
    if (
        fixture.get("schema") != "io.tobkiri.legacy-executable-source-input.v1"
        or fixture.get("source_format") != "legacy-entrypoint-map"
    ):
        raise ExecutableSourceRegistryError(f"legacy source fixture schema is invalid: {fixture_path}")
    overrides_value = fixture.get("operation_id_overrides", [])
    if not isinstance(overrides_value, list):
        raise ExecutableSourceRegistryError("operation_id_overrides must be a list")
    overrides: dict[tuple[str, str], str] = {}
    for item in overrides_value:
        if not isinstance(item, Mapping):
            raise ExecutableSourceRegistryError("operation ID override is not an object")
        pack_id = item.get("pack_id")
        entrypoint_id = item.get("entrypoint_id")
        operation_id = item.get("operation_id")
        if not all(isinstance(value, str) and value.strip() for value in (pack_id, entrypoint_id, operation_id)):
            raise ExecutableSourceRegistryError("operation ID override is incomplete")
        key = (pack_id, entrypoint_id)
        if key in overrides:
            raise ExecutableSourceRegistryError(f"duplicate operation ID override: {key}")
        overrides[key] = operation_id

    function_overrides_value = fixture.get("function_id_overrides", [])
    if not isinstance(function_overrides_value, list):
        raise ExecutableSourceRegistryError("function_id_overrides must be a list")
    function_overrides: dict[tuple[str, str], str] = {}
    for item in function_overrides_value:
        if not isinstance(item, Mapping):
            raise ExecutableSourceRegistryError("function ID override is not an object")
        pack_id = item.get("pack_id")
        entrypoint_id = item.get("entrypoint_id")
        function_id = item.get("function_id")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (pack_id, entrypoint_id, function_id)
        ):
            raise ExecutableSourceRegistryError("function ID override is incomplete")
        assert isinstance(pack_id, str)
        assert isinstance(entrypoint_id, str)
        assert isinstance(function_id, str)
        canonical_pack_id = _canonical_id(pack_id)
        if (
            function_id != _canonical_id(function_id)
            or not function_id.startswith(f"{canonical_pack_id}.")
        ):
            raise ExecutableSourceRegistryError(
                f"function ID override is not owned by Pack: {pack_id}:{function_id}"
            )
        key = (pack_id, entrypoint_id)
        if key in function_overrides:
            raise ExecutableSourceRegistryError(
                f"duplicate function ID override: {key}"
            )
        function_overrides[key] = function_id

    packs_value = fixture.get("packs")
    if not isinstance(packs_value, Mapping):
        raise ExecutableSourceRegistryError("legacy source fixture packs must be an object")
    records: dict[str, dict[str, Any]] = {}
    for pack_id, pack_source in packs_value.items():
        if not isinstance(pack_id, str) or not pack_id.strip() or not isinstance(pack_source, Mapping):
            raise ExecutableSourceRegistryError("legacy source fixture Pack entry is invalid")
        pack_root = ecosystem_root / pack_id
        if not pack_root.is_dir():
            raise ExecutableSourceRegistryError(f"fixture Pack does not exist: {pack_id}")
        entries = pack_source.get("entries")
        if not isinstance(entries, list) or not entries:
            raise ExecutableSourceRegistryError(f"fixture Pack has no entries: {pack_id}")
        for entry in entries:
            if not isinstance(entry, Mapping):
                raise ExecutableSourceRegistryError(f"fixture entry is not an object: {pack_id}")
            function_id = entry.get("function_id")
            operation_ids = entry.get("operation_ids")
            implementation_path = entry.get("implementation_path")
            if not isinstance(function_id, str) or not function_id.strip():
                raise ExecutableSourceRegistryError(f"fixture Function ID is missing: {pack_id}")
            if not isinstance(operation_ids, list) or not operation_ids:
                raise ExecutableSourceRegistryError(f"fixture Operation IDs are missing: {function_id}")
            if len(operation_ids) != len(set(operation_ids)) or any(
                not isinstance(value, str) or not value.strip() for value in operation_ids
            ):
                raise ExecutableSourceRegistryError(f"fixture Operation IDs are invalid: {function_id}")
            if not isinstance(implementation_path, str) or not implementation_path.strip():
                raise ExecutableSourceRegistryError(f"fixture implementation path is missing: {function_id}")
            implementation = _relative_runtime_path(pack_root, implementation_path)
            contract_id, contract_version, schemas = _fixture_contract(
                entry,
                pack_root=pack_root,
                repository_root=repository_root,
            )
            for operation_id in operation_ids:
                _add_operation(
                    records,
                    pack_id=pack_id,
                    function_id=function_id,
                    operation_id=operation_id,
                    contract_id=contract_id,
                    contract_version=contract_version,
                    implementation_path=implementation,
                    pack_root=pack_root,
                    schemas=schemas,
                    source={
                        "kind": "legacy-explicit-entrypoint",
                        "path": _label(fixture_path, repository_root),
                        "pack_source_id": pack_id,
                        "entrypoint_id": operation_id,
                    },
                )
    return records, overrides, function_overrides


def _merge_records(
    base: dict[str, dict[str, Any]],
    explicit: Mapping[str, Mapping[str, Any]],
    *,
    repository_root: Path,
) -> None:
    """Merge explicit additions into v3 records with strict identity checks."""

    for function_id, explicit_record in explicit.items():
        target = base.get(function_id)
        if target is None:
            base[function_id] = dict(explicit_record)
            continue
        if target["pack_id"] != explicit_record["pack_id"]:
            raise ExecutableSourceRegistryError(f"explicit Function owner conflict: {function_id}")
        if target["implementation_path"] != explicit_record["implementation_path"] or target[
            "implementation_digest"
        ] != explicit_record["implementation_digest"]:
            raise ExecutableSourceRegistryError(f"explicit Function implementation conflict: {function_id}")
        if target["contract_id"] != explicit_record["contract_id"]:
            raise ExecutableSourceRegistryError(f"explicit Function Contract conflict: {function_id}")
        target["operations"].extend(explicit_record["operations"])
        target["source"].extend(explicit_record["source"])
        operation_ids = [str(item["operation_id"]) for item in target["operations"]]
        if len(operation_ids) != len(set(operation_ids)):
            raise ExecutableSourceRegistryError(f"explicit Operation duplicates a legacy Operation: {function_id}")
    del repository_root


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Sort nested source evidence and operations for reproducible output."""

    record["operations"] = sorted(
        record["operations"], key=lambda item: str(item["operation_id"])
    )
    record["source"] = sorted(
        record["source"],
        key=lambda item: (str(item.get("path")), str(item.get("entrypoint_id"))),
    )
    if len(record["operations"]) == 1:
        record["operation_id"] = record["operations"][0]["operation_id"]
    return record


def build_registry(
    ecosystem_root: Path = ECOSYSTEM,
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
) -> dict[str, Any]:
    """Build the complete registry without reading any v4 executable catalog."""

    ecosystem_root = Path(ecosystem_root).resolve()
    repository_root = ecosystem_root.parent.parent
    explicit, operation_overrides, function_overrides = _fixture_records(
        ecosystem_root,
        Path(fixture_path).resolve(),
        repository_root=repository_root,
    )
    records: dict[str, dict[str, Any]] = {}
    v3_paths: list[Path] = []
    for pack_root in sorted(path for path in ecosystem_root.iterdir() if path.is_dir() and path.name != "setup_pack"):
        v3_path = pack_root / "rumi.pack.v3.json"
        if v3_path.is_file():
            v3_paths.append(v3_path)
            _merge_records(
                records,
                _v3_records(
                    pack_root,
                    operation_overrides=operation_overrides,
                    function_overrides=function_overrides,
                    repository_root=repository_root,
                ),
                repository_root=repository_root,
            )
    _merge_records(records, explicit, repository_root=repository_root)
    for (pack_id, entrypoint_id), operation_id in operation_overrides.items():
        if not any(
            source.get("path") == _label(pack_id_path / "rumi.pack.v3.json", repository_root)
            and source.get("entrypoint_id") == entrypoint_id
            for pack_id_path in (ecosystem_root / pack_id,)
            for record in records.values()
            for source in record.get("source", [])
        ):
            raise ExecutableSourceRegistryError(
                f"operation ID override does not match a legacy entrypoint: {pack_id}:{entrypoint_id}"
            )
        if not operation_id.strip():
            raise ExecutableSourceRegistryError(f"operation ID override is empty: {pack_id}:{entrypoint_id}")
    for (pack_id, entrypoint_id), function_id in function_overrides.items():
        if not any(
            source.get("path")
            == _label(pack_id_path / "rumi.pack.v3.json", repository_root)
            and source.get("entrypoint_id") == entrypoint_id
            for pack_id_path in (ecosystem_root / pack_id,)
            for record in records.values()
            for source in record.get("source", [])
        ):
            raise ExecutableSourceRegistryError(
                "function ID override does not match a legacy entrypoint: "
                f"{pack_id}:{entrypoint_id}"
            )
        if not function_id.strip():
            raise ExecutableSourceRegistryError(
                f"function ID override is empty: {pack_id}:{entrypoint_id}"
            )
    fixture_resolved = Path(fixture_path).resolve()
    source_inputs: list[dict[str, str]] = []
    for path in v3_paths:
        source_inputs.append(
            {"kind": "legacy-v3-manifest", "path": _label(path, repository_root), "digest": _file_digest(path)}
        )
        artifact_manifest = path.parent / "artifact-manifest.json"
        if artifact_manifest.is_file():
            source_inputs.append(
                {
                    "kind": "legacy-artifact-manifest",
                    "path": _label(artifact_manifest, repository_root),
                    "digest": _file_digest(artifact_manifest),
                }
            )
    source_inputs.append(
        {"kind": "legacy-explicit-fixture", "path": _label(fixture_resolved, repository_root), "digest": _file_digest(fixture_resolved)}
    )
    source_inputs.sort(key=lambda item: (item["kind"], item["path"]))
    for record in records.values():
        _normalize_record(record)
    return {
        "schema": "io.tobkiri.executable-sources.v1",
        "source": {
            "kind": "legacy-shaped",
            "generator": GENERATOR,
            "generator_version": GENERATOR_VERSION,
            "input_count": len(source_inputs),
            "input_digest": _canonical_digest(source_inputs),
            "inputs": source_inputs,
            "input_paths": [item["path"] for item in source_inputs],
        },
        "packs": {key: records[key] for key in sorted(records)},
    }


def generate(
    output: Path = DEFAULT_OUTPUT,
    *,
    fixture_path: Path = DEFAULT_FIXTURE,
    check: bool = False,
) -> dict[str, Any]:
    """Generate or check the executable source registry."""

    payload = build_registry(ECOSYSTEM, fixture_path=fixture_path)
    text = _json_text(payload)
    output = Path(output)
    if check:
        if not output.is_file() or output.read_text(encoding="utf-8") != text:
            raise ExecutableSourceRegistryError(f"executable source registry drift: {output}")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return payload


def main() -> int:
    """Run the executable source registry generator from the command line."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    fixture = args.fixture if args.fixture.is_absolute() else ROOT / args.fixture
    try:
        payload = generate(output, fixture_path=fixture, check=args.check)
    except ExecutableSourceRegistryError as exc:
        print(f"RED: {exc}", file=sys.stderr)
        return 1
    records = payload["packs"]
    print(
        f"GREEN: executable source registry has {len(records)} Functions and "
        f"{sum(len(record['operations']) for record in records.values())} Operations"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
