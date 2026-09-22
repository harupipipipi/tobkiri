"""Import and generate canonical production Pack v4 artifacts.

``--import-legacy`` is the one-way migration boundary.  Normal generation and
runtime validation consume only ``schemas/pack_v4_catalog.v1.json``; v3 and
legacy manifests are never fallback authority inputs.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tobkiri_protocol.canonical import canonical_json  # noqa: E402
from tobkiri_protocol.validation import validate_document  # noqa: E402

ECOSYSTEM = ROOT / "ecosystem"
CATALOG = ROOT / "schemas" / "pack_v4_catalog.v1.json"
AUTHORITY = ROOT / "schemas" / "manifest_authority.v1.json"
EXECUTABLE_SOURCES = ROOT / "schemas" / "executable_sources.v1.json"
EXCLUDED_PACKS: frozenset[str] = frozenset()
GENERATOR = "tobkiri.scripts.migrate_pack_artifacts_v4"
GENERATOR_VERSION = "1.0.0"
START_COMMIT = "1329f300cd2a8e15170edb1accce8d7c3167882b"
EMPTY_SCHEMA: dict[str, Any] = {"type": "object"}
VERSION_SUFFIX = re.compile(r"\.v[1-9][0-9]*$")


class PackV4MigrationError(ValueError):
    """Raised when a source or generated Pack artifact is ambiguous or stale."""


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(value)).hexdigest()


def _file_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _file_digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _canonical_id(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9._-]+", "-", value.lower()).strip("._-")
    normalized = re.sub(r"[-_.]{2,}", ".", normalized)
    if not normalized or not normalized[0].isalpha():
        normalized = f"id.{normalized or 'unknown'}"
    return normalized


def _contract_id(value: str) -> str:
    """Map a classified legacy Contract ID into the canonical v4 namespace."""
    if value.startswith("rumi."):
        return "tobkiri." + value.removeprefix("rumi.")
    if value.startswith(("rumiai.", "viewer.", "legacy.")):
        return "tobkiri.migrated." + value.split(".", 1)[1]
    return value


def _pack_kind(source_kind: str, *, host_execution: bool) -> str:
    if source_kind in {"ui", "bundle", "content"}:
        return "application"
    if host_execution:
        return "host_extension"
    return "normal_sandbox"


def _repository_tree_identity() -> str:
    try:
        tree = subprocess.run(
            ["git", "rev-parse", f"{START_COMMIT}^{{tree}}"],
            cwd=ROOT,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise PackV4MigrationError(f"cannot resolve migration tree: {exc}") from exc
    return hashlib.sha256(tree.encode("ascii")).hexdigest()


def _effect_ceiling(
    capabilities: Iterable[str],
    network: Mapping[str, Any],
    secrets: Iterable[Any],
    *,
    host_execution: bool,
) -> list[str]:
    effects = {f"capability:{item}" for item in capabilities if str(item).strip()}
    for domain in network.get("allowed_domains", []):
        effects.add(f"network:{domain}")
    for secret in secrets:
        if isinstance(secret, str):
            effects.add(f"secret:{secret}")
        elif isinstance(secret, Mapping):
            name = secret.get("name") or secret.get("id") or secret.get("key")
            if name:
                effects.add(f"secret:{name}")
    if host_execution:
        effects.add("host:brokered-execution")
    return sorted(effects)


def _workspace_boundary(capabilities: Iterable[str], host_execution: bool) -> str:
    if host_execution:
        return "host_brokered"
    values = " ".join(capabilities).lower()
    if any(token in values for token in ("workspace", "file", "shell", "git")):
        return "workspace_brokered"
    return "pack_local"


def _approval_policy(
    permissions: Iterable[Mapping[str, Any]],
    legacy: Mapping[str, Any],
) -> str:
    if any(bool(item.get("approval_required")) for item in permissions):
        return "capability_gated"
    annotations = (legacy.get("metadata") or {}).get("legacy_annotations") or {}
    declared = annotations.get("permissions_required") or {}
    if isinstance(declared, Mapping) and any(
        isinstance(item, Mapping) and item.get("requires_user_grant") for item in declared.values()
    ):
        return "capability_gated"
    return "none"


def _component_operations(legacy: Mapping[str, Any]) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    top = legacy.get("connectivity") or {}
    for operation in top.get("provides", []):
        if isinstance(operation, str) and operation.strip():
            result.append({"id": operation, "source": "legacy_connectivity"})
    components = legacy.get("components") or {}
    if isinstance(components, Mapping):
        for key, component in sorted(components.items()):
            if not isinstance(component, Mapping):
                continue
            connectivity = component.get("connectivity") or {}
            for operation in connectivity.get("provides", []):
                if isinstance(operation, str) and operation.strip():
                    result.append(
                        {
                            "id": operation,
                            "source": "legacy_component",
                            "component_id": str(component.get("id") or key),
                        }
                    )
    identities = [item["id"] for item in result]
    if len(identities) != len(set(identities)):
        raise PackV4MigrationError("duplicate legacy operation identity")
    return result


def _runtime_artifacts(pack_root: Path) -> list[dict[str, str]]:
    old_index = pack_root / "artifact-manifest.json"
    if not old_index.is_file():
        return []
    payload = json.loads(old_index.read_text(encoding="utf-8"))
    result: list[dict[str, str]] = []
    for item in payload.get("artifacts", []):
        if not isinstance(item, Mapping) or not item.get("path"):
            continue
        relative = Path(str(item["path"]))
        candidate = (pack_root / relative).resolve()
        try:
            candidate.relative_to(pack_root.resolve())
        except ValueError as exc:
            raise PackV4MigrationError(f"artifact escapes Pack: {candidate}") from exc
        if not candidate.is_file():
            raise PackV4MigrationError(f"declared artifact is missing: {candidate}")
        role = str(item.get("role") or "runtime")
        kind = (
            "schema" if candidate.suffix == ".json" and "schema" in candidate.name else "executable"
        )
        if role not in {"runtime", "executable"} and kind != "schema":
            kind = "sidecar"
        result.append(
            {"path": relative.as_posix(), "digest": _file_digest(candidate), "kind": kind}
        )
    return sorted(result, key=lambda item: item["path"])


def _migration_source_view(path: Path) -> dict[str, Any]:
    """Return the immutable semantic view used by the one-way importer.

    Legacy and v3 documents are compatibility projections.  Their authority
    generator rewrites provenance and canonical-v4 pointers whenever the v4
    artifact set changes.  Hashing those generated envelopes made
    ``--import-legacy --check`` feed v4 output back into its own source
    identity, so authority → v4 → authority never reached a fixed point.
    Keep the source evidence bound to the semantic input and exclude only the
    projection-owned fields.  Runtime entrypoint/artifact bytes remain in the
    view and therefore still cause an intentional migration when they change.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise PackV4MigrationError(f"migration source must be an object: {path}")
    payload = copy.deepcopy(payload)
    if path.name == "ecosystem.json":
        payload.pop("provenance", None)
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in (
                "canonical_v4",
                "format",
                "generated",
                "generated_from",
                "manifest_authority",
                "projection_owner",
                "read_only_projection",
            ):
                metadata.pop(key, None)
    elif path.name == "rumi.pack.v3.json":
        payload.pop("provenance", None)
        extensions = payload.get("extensions")
        if isinstance(extensions, dict):
            extensions.pop("rumi.legacy_projection", None)
            extensions.pop("tobkiri.offline_projection", None)
    return payload


def _source_evidence(pack_root: Path, paths: Iterable[Path]) -> list[dict[str, str]]:
    return [
        {
            "path": path.relative_to(ROOT).as_posix(),
            "rule_id": "pack-v4-one-way-migration-input",
            "digest": _digest(_migration_source_view(path)),
        }
        for path in paths
        if path.is_file() and path.is_relative_to(pack_root)
    ]


def _entrypoint_implementation_digest(entrypoint: Mapping[str, Any]) -> str | None:
    """Hash executable entrypoint bytes instead of trusting a v3 projection."""
    module = str(entrypoint.get("module") or "").strip()
    if not module:
        declared = str(entrypoint.get("artifact_hash") or "").strip()
        return declared or None
    candidate = ROOT.joinpath(*module.split(".")).with_suffix(".py")
    if not candidate.is_file():
        raise PackV4MigrationError(f"v3 entrypoint module is missing: {candidate}")
    return _file_digest(candidate)


def _import_record(pack_root: Path) -> dict[str, Any]:
    legacy_path = pack_root / "ecosystem.json"
    v3_path = pack_root / "rumi.pack.v3.json"
    legacy = json.loads(legacy_path.read_text(encoding="utf-8"))
    v3 = json.loads(v3_path.read_text(encoding="utf-8")) if v3_path.is_file() else None
    pack_data = v3["pack"] if v3 else legacy
    dependencies = legacy.get("dependencies") or {}
    if isinstance(dependencies, list):
        dependencies = {
            str(item["pack_id"]): str(item.get("version") or ">=0.0.0")
            for item in dependencies
            if isinstance(item, Mapping) and item.get("pack_id")
        }
    if not isinstance(dependencies, Mapping):
        raise PackV4MigrationError(f"invalid dependencies: {pack_root.name}")
    permissions = [item for item in (v3 or {}).get("permissions", []) if isinstance(item, Mapping)]
    capabilities = {
        str(value)
        for value in legacy.get("required_capabilities", legacy.get("capabilities", []))
        if str(value).strip()
    }
    capabilities.update(
        str(item["capability"]) for item in permissions if isinstance(item.get("capability"), str)
    )
    network = legacy.get("required_network") or {
        "allowed_domains": [],
        "allowed_ports": [],
    }
    secrets = legacy.get("required_secrets") or []
    host_execution = bool(legacy.get("host_execution", False))
    contracts = (v3 or {}).get("contracts") or {"provides": [], "requires": []}
    entrypoints = (v3 or {}).get("entrypoints") or []
    provided: list[dict[str, Any]] = []
    for contract in contracts.get("provides", []):
        matches = [item for item in entrypoints if item.get("contract_id") == contract["id"]]
        operation_sources = matches or [
            {"id": f"{contract['provider_instance_id']}.invoke", "artifact_hash": None}
        ]
        operations = [
            {
                "id": _canonical_id(f"{pack_root.name}.{item['id']}"),
                "entrypoint_id": str(item["id"]),
                "implementation_digest": _entrypoint_implementation_digest(item),
            }
            for item in operation_sources
        ]
        provided.append(
            {
                "contract_id": _contract_id(contract["id"]),
                "version": contract["version"],
                "provider_id": _canonical_id(
                    f"{pack_root.name}.{contract['provider_instance_id']}"
                ),
                "operations": operations,
                "schemas": contract.get("schemas") or {},
                "cardinality": contract["cardinality"],
                "security": contract["security"],
                "failure": contract["failure"],
                "isolation": contract["isolation"],
                "required_capabilities": sorted(contract.get("required_capabilities", [])),
                "lifecycle": contract["lifecycle"],
                **{
                    key: contract[key]
                    for key in ("routing_keys", "instance_key", "priority", "before", "after")
                    if key in contract
                },
            }
        )
    executable_sources = json.loads(EXECUTABLE_SOURCES.read_text(encoding="utf-8"))["packs"]
    executable_source = executable_sources.get(pack_root.name)
    if executable_source is not None:
        matches = [
            item for item in provided if item["contract_id"] == executable_source["contract_id"]
        ]
        if len(matches) != 1:
            raise PackV4MigrationError(f"canonical executable Contract mismatch: {pack_root.name}")
        matches[0]["schemas"] = {
            "input": executable_source["input_schema"],
            "output": executable_source["output_schema"],
            "error": executable_source["error_schema"],
        }
        implementation_digest = _file_digest(pack_root / executable_source["implementation_path"])
        for operation in matches[0]["operations"]:
            operation["implementation_digest"] = implementation_digest
    required = []
    for item in contracts.get("requires", []):
        required.append(
            {
                "contract_id": _contract_id(item["id"]),
                "version_range": item["version_range"],
                "cardinality": item["cardinality"],
                "optional": item["optional"],
                **({"instance_key": item["instance_key"]} if item.get("instance_key") else {}),
            }
        )
    source_paths = [legacy_path] + ([v3_path] if v3_path.is_file() else [])
    record = {
        "pack_id": pack_root.name,
        "version": str(pack_data.get("version") or legacy["version"]),
        "kind": _pack_kind(str(pack_data.get("kind") or "content"), host_execution=host_execution),
        "display_name": str(
            pack_data.get("display_name") or legacy.get("display_name") or pack_root.name
        ),
        "description": str(pack_data.get("description") or legacy.get("description") or ""),
        "authority": "v4-authoritative",
        "source_provenance": {
            "owner": pack_root.name,
            "mode": "offline-one-way-import",
            "source_format": "rumi.pack.v3.json" if v3 is not None else "ecosystem.json",
            "historical_classification": (
                "v3-authoritative" if v3 is not None else "legacy-authoritative"
            ),
        },
        "dependencies": dict(sorted((str(key), str(value)) for key, value in dependencies.items())),
        "required_contracts": sorted(required, key=lambda item: item["contract_id"]),
        "capabilities": sorted(capabilities),
        "network": network,
        "secrets": secrets,
        "execution_boundary": "host_brokered"
        if host_execution
        else ("declarative_only" if not v3_path.is_file() else "sandbox"),
        "approval_policy": _approval_policy(permissions, legacy),
        "workspace_boundary": _workspace_boundary(capabilities, host_execution),
        "provided_contracts": sorted(provided, key=lambda item: item["contract_id"]),
        # A v3 Pack's ecosystem connectivity is its generated compatibility
        # view, not a second operation source. Only legacy-only Packs import
        # component/connectivity operations.
        "legacy_operations": _component_operations(legacy) if v3 is None else [],
        "runtime_artifacts": _runtime_artifacts(pack_root),
        "legacy_ids": sorted(
            {
                pack_root.name,
                str(legacy.get("pack_identity") or f"rumi:ecosystem/{pack_root.name}"),
                *(
                    item["id"]
                    for item in contracts.get("provides", [])
                    if isinstance(item, Mapping) and isinstance(item.get("id"), str)
                ),
            }
        ),
        "migration": {
            "removal_wave": int((v3 or {}).get("migration", {}).get("removal_wave", 10)),
            "sunset_at": str((v3 or {}).get("migration", {}).get("sunset_at", "2099-01-01")),
        },
        "source_evidence": _source_evidence(pack_root, source_paths),
    }
    return record


def _import_bundled_record(pack_id: str) -> dict[str, Any]:
    """Import the two finite Defaults v4 sources without legacy authority."""
    exact_source_path = (
        ECOSYSTEM
        / "defaultspack"
        / "v4"
        / "packs"
        / f"{pack_id.replace('_', '-')}.pack.v4.json"
    )
    if exact_source_path.is_file():
        source_path = exact_source_path
        source = json.loads(source_path.read_text(encoding="utf-8"))
        executable = json.loads(
            (ECOSYSTEM / pack_id / "executables.v4.json").read_text(encoding="utf-8")
        )["variants"][0]
        operation = executable["operations"][0]
        function = source["functions"][0]
        contract = source["contracts"][0]
        return {
            "pack_id": pack_id,
            "version": source["pack"]["version"],
            "kind": source["pack"]["kind"],
            "display_name": source["pack"]["display_name"],
            "description": (
                "Finite read-only Pack catalog provider for the selected control-panel UI."
            ),
            "authority": "v4-authoritative",
            "source_provenance": {
                "owner": pack_id,
                "mode": "canonical-v4",
                "source_format": "pack.v4.json",
                "historical_classification": "modern-only",
            },
            "dependencies": {},
            "required_contracts": [],
            "capabilities": ["pack.catalog.read"],
            "network": {"allowed_domains": [], "allowed_ports": []},
            "secrets": [],
            "execution_boundary": "host_brokered",
            "approval_policy": "capability_gated",
            "workspace_boundary": "host_brokered",
            "provided_contracts": [
                {
                    "contract_id": contract["contract_id"],
                    "version": "1.0.0",
                    "provider_id": function["id"],
                    "operations": [
                        {
                            "id": operation["operation_id"],
                            "entrypoint_id": operation["operation_id"],
                            "implementation_digest": function["implementation_digest"],
                        }
                    ],
                    "schemas": {
                        "input": operation["input_schema"],
                        "output": operation["output_schema"],
                        "error": operation["error_schema"],
                    },
                    "cardinality": "one",
                    "security": "sensitive",
                    "failure": "fail_closed",
                    "isolation": "in_process",
                    "required_capabilities": ["pack.catalog.read"],
                    "lifecycle": {
                        "introduced": "1.0.0",
                        "deprecated": False,
                    },
                }
            ],
            "legacy_operations": [],
            "runtime_artifacts": list(source["artifacts"]),
            "legacy_ids": [],
            "migration": {
                "compatibility": "none",
                "removal_wave": 0,
                "sunset_at": "2026-08-05",
            },
            "source_evidence": [],
        }
    source_name = (
        "defaults-basepack.pack.v4.json" if pack_id == "defaults" else "defaultspack.pack.v4.json"
    )
    source_path = ECOSYSTEM / "defaultspack" / "v4" / "packs" / source_name
    source = json.loads(source_path.read_text(encoding="utf-8"))
    pack = source["pack"]
    provided: list[dict[str, Any]] = []
    executable_source: dict[str, Any] | None = None
    implementation_digest: str | None = None
    if pack_id == "defaultspack":
        source_path = EXECUTABLE_SOURCES
        pack = {
            "id": "defaultspack",
            "version": "4.0.0",
            "kind": "normal_sandbox",
            "display_name": "Tobkiri Defaults Providers",
        }
        executable_source = json.loads(EXECUTABLE_SOURCES.read_text(encoding="utf-8"))["packs"][
            pack_id
        ]
        implementation_digest = _file_digest(
            ECOSYSTEM / pack_id / executable_source["implementation_path"]
        )
        provided.append(
            {
                "contract_id": executable_source["contract_id"],
                "version": "1.0.0",
                "provider_id": executable_source["function_id"],
                "operations": [
                    {
                        "id": executable_source["operation_id"],
                        "entrypoint_id": executable_source["operation_id"],
                        "implementation_digest": implementation_digest,
                    }
                ],
                "schemas": {
                    "input": executable_source["input_schema"],
                    "output": executable_source["output_schema"],
                    "error": executable_source["error_schema"],
                },
                "cardinality": "one",
                "security": "restricted",
                "failure": "fail_closed",
                "isolation": "sandbox",
                "required_capabilities": [],
                "lifecycle": {
                    "introduced": "4.0.0",
                    "deprecated": False,
                },
            }
        )
    return {
        "pack_id": pack_id,
        "version": pack["version"],
        "kind": "base" if pack_id == "defaults" else pack["kind"],
        "display_name": pack["display_name"],
        "description": "Canonical Defaults v4 composition artifact.",
        "authority": "v4-authoritative",
        "source_provenance": {
            "owner": pack_id,
            "mode": "canonical-v4",
            "source_format": "pack.v4.json",
            "historical_classification": "modern-only",
        },
        "dependencies": {},
        "required_contracts": [],
        "capabilities": [],
        "network": {"allowed_domains": [], "allowed_ports": []},
        "secrets": [],
        "execution_boundary": ("declarative_only" if pack_id == "defaults" else "sandbox"),
        "approval_policy": "none",
        "workspace_boundary": "pack_local",
        "provided_contracts": provided,
        "legacy_operations": [],
        "runtime_artifacts": (
            [
                {
                    "path": executable_source["implementation_path"],
                    "digest": implementation_digest,
                    "kind": "executable",
                }
            ]
            if executable_source is not None and implementation_digest is not None
            else []
        ),
        "legacy_ids": [],
        "migration": {
            "compatibility": "none",
            "removal_wave": 0,
            "sunset_at": "2026-08-05",
        },
        "source_evidence": [
            {
                "path": source_path.relative_to(ROOT).as_posix(),
                "rule_id": "canonical-bundled-v4-source",
                "digest": _file_digest(source_path),
            }
        ],
    }


def _assign_contract_owners(records: list[dict[str, Any]]) -> None:
    """Record one deterministic owner for every multiply-provided contract."""
    providers: dict[str, list[str]] = {}
    for record in records:
        for contract in record["provided_contracts"]:
            providers.setdefault(contract["contract_id"], []).append(record["pack_id"])
    owners = {contract_id: sorted(pack_ids)[0] for contract_id, pack_ids in providers.items()}
    for record in records:
        for contract in record["provided_contracts"]:
            contract["owner"] = owners[contract["contract_id"]]


def import_legacy(*, check: bool) -> None:
    authority_payload = json.loads(AUTHORITY.read_text(encoding="utf-8"))
    authorities = authority_payload["packs"]
    pack_names = sorted(set(authorities) - EXCLUDED_PACKS)
    records = [
        (
            _import_bundled_record(name)
            if name
            in {
                "defaults",
                "defaultspack",
                "tobkiri_host_pack_control",
            }
            else _import_record(ECOSYSTEM / name)
        )
        for name in pack_names
    ]
    _assign_contract_owners(records)
    payload = {
        "catalog_api_version": "io.tobkiri.pack-source-catalog.v1",
        "migration_commit": START_COMMIT,
        "excluded_packs": sorted(EXCLUDED_PACKS),
        "pack_ids": pack_names,
        "packs": records,
    }
    text = _json_text(payload)
    if check:
        if not CATALOG.is_file() or CATALOG.read_text(encoding="utf-8") != text:
            raise PackV4MigrationError("canonical Pack v4 source catalog drift")
        return
    CATALOG.write_text(text, encoding="utf-8")


def _provenance(record: Mapping[str, Any], source_identity: str) -> dict[str, Any]:
    return {
        "schema": "io.tobkiri.provenance.v1",
        "source_kind": "migration",
        "source_path": f"schemas/pack_v4_catalog.v1.json#/packs/{record['pack_id']}",
        "source_digest": source_identity,
        "repository_commit": START_COMMIT,
        "repository_tree": _repository_tree_identity(),
        "generator": GENERATOR,
        "generator_version": GENERATOR_VERSION,
        "normative": True,
        "evidence": [
            {
                "path": "schemas/pack_v4_catalog.v1.json",
                "rule_id": "canonical-pack-v4-source-record",
                "digest": source_identity,
            }
        ],
    }


def _contract_document(
    record: Mapping[str, Any],
    source_identity: str,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    schemas = source["schemas"]
    input_schema = schemas.get("input", EMPTY_SCHEMA)
    output_schema = schemas.get("output", schemas.get("event", EMPTY_SCHEMA))
    error_schema = schemas.get("error", EMPTY_SCHEMA)
    schema_catalog = {
        _digest(value): value for value in (input_schema, output_schema, error_schema)
    }
    effects = _effect_ceiling(
        source["required_capabilities"],
        record["network"],
        record["secrets"],
        host_execution=record["execution_boundary"] == "host_brokered",
    )
    operations = [
        {
            "operation_id": item["id"],
            "input_schema_digest": _digest(input_schema),
            "output_schema_digest": _digest(output_schema),
            "error_schema_digest": _digest(error_schema),
            "effect_ceiling": effects,
            "scope_semantics": (
                "host_broker" if record["execution_boundary"] == "host_brokered" else "declarative"
            ),
            "idempotency": {"mode": "none"},
        }
        for item in source["operations"]
    ]
    semantics = {
        key: copy.deepcopy(source[key])
        for key in (
            "provider_id",
            "cardinality",
            "security",
            "failure",
            "isolation",
            "required_capabilities",
            "lifecycle",
            "routing_keys",
            "instance_key",
            "priority",
            "before",
            "after",
        )
        if key in source
    }
    unsigned = {
        "contract_api_version": "io.tobkiri.contract.v4",
        "contract_id": source["contract_id"],
        "version": source["version"],
        "owner": source["owner"],
        "status": "deprecated" if source["lifecycle"].get("deprecated") else "accepted",
        "operations": operations,
        "schema_catalog": schema_catalog,
        "provider_semantics": semantics,
    }
    revision = _digest(unsigned)
    return {
        **unsigned,
        "revision_digest": revision,
        "provenance": _provenance(record, source_identity),
    }


def _artifact_set_digest(artifacts: list[Mapping[str, Any]]) -> str:
    return _digest(artifacts)


def _manifest_document(
    record: Mapping[str, Any],
    source_identity: str,
    contracts: list[Mapping[str, Any]],
    contract_catalog_digest: str,
    executable_catalog_artifact_digest: str | None = None,
) -> dict[str, Any]:
    contract_by_id = {item["contract_id"]: item for item in contracts}
    artifact_set = [
        {key: value for key, value in item.items() if key != "index_role"}
        for item in record["runtime_artifacts"]
    ]
    if executable_catalog_artifact_digest is not None:
        artifact_set.append(
            {
                "path": "executables.v4.json",
                "digest": executable_catalog_artifact_digest,
                "kind": "sidecar",
            }
        )
    artifact_set.sort(key=lambda item: item["path"])
    artifact_set_digest = _artifact_set_digest(artifact_set)
    functions = []
    operation_catalog = []
    provider_catalog = []
    for source in record["provided_contracts"]:
        contract = contract_by_id[source["contract_id"]]
        operations = [item["id"] for item in source["operations"]]
        implementation = next(
            (
                item["implementation_digest"]
                for item in source["operations"]
                if item.get("implementation_digest")
            ),
            artifact_set_digest,
        )
        isolation = source["isolation"]
        role = (
            "host_capability_provider"
            if record["execution_boundary"] == "host_brokered"
            else "brokered"
        )
        functions.append(
            {
                "id": source["provider_id"],
                "implementation_digest": implementation,
                "contract_revision_digest": contract["revision_digest"],
                "operations": operations,
                "role": role,
                "isolation": {
                    "in_process": "dedicated_process",
                    "process": "dedicated_process",
                    "sandbox": "pack_vm",
                    "remote": "remote",
                }[isolation],
            }
        )
        provider_catalog.append(
            {
                "provider_id": source["provider_id"],
                "owner": record["pack_id"],
                "contract_reference": source["contract_id"],
                "operations": operations,
            }
        )
        effects = next(iter(contract["operations"]), {}).get("effect_ceiling", [])
        operation_catalog.extend(
            {
                "operation_id": operation,
                "owner": record["pack_id"],
                "contract_reference": source["contract_id"],
                "provider_id": source["provider_id"],
                "source_kind": "canonical_v4_contract",
                "effect_ceiling": effects,
            }
            for operation in operations
        )
    # Legacy component/connectivity operations are migration evidence only.
    # They are deliberately not projected into the v4 authority catalog: a
    # declarative Pack must not acquire an executable Operation/Function
    # principal merely because an old compatibility manifest named one.
    return {
        "pack_api_version": "io.tobkiri.pack.v4",
        "pack": {
            "id": record["pack_id"],
            "version": record["version"],
            "kind": record["kind"],
            "artifact_digest": artifact_set_digest,
            "display_name": record["display_name"],
        },
        "functions": sorted(functions, key=lambda item: item["id"]),
        "contracts": [
            {
                "contract_id": item["contract_id"],
                "revision_digest": item["revision_digest"],
                "operations": [operation["operation_id"] for operation in item["operations"]],
            }
            for item in contracts
        ],
        "artifacts": artifact_set,
        "requirements": {
            "pack_dependencies": record["dependencies"],
            "contract_dependencies": record["required_contracts"],
            "capabilities": record["capabilities"],
            "network": record["network"],
            "secrets": record["secrets"],
            "execution_boundary": record["execution_boundary"],
            "approval_policy": record["approval_policy"],
            "workspace_boundary": record["workspace_boundary"],
        },
        "operation_catalog": sorted(operation_catalog, key=lambda item: item["operation_id"]),
        "provider_catalog": sorted(provider_catalog, key=lambda item: item["provider_id"]),
        "integrity": {
            "source_identity": source_identity,
            "artifact_set_digest": artifact_set_digest,
            "contract_catalog_digest": contract_catalog_digest,
        },
        "provenance": _provenance(record, source_identity),
        "migration": {
            "compatibility": record["migration"].get("compatibility", "read_only"),
            "legacy_ids": record["legacy_ids"],
            "removal_wave": record["migration"]["removal_wave"],
            "sunset_at": record["migration"]["sunset_at"],
        },
    }


def _render_record(record: Mapping[str, Any]) -> dict[str, str]:
    from scripts.generate_executable_catalogs_v4 import _render_document

    source_identity = _digest(record)
    contracts = [
        _contract_document(record, source_identity, source)
        for source in record["provided_contracts"]
    ]
    contract_catalog = {
        "catalog_api_version": "io.tobkiri.pack-contract-catalog.v4",
        "pack_id": record["pack_id"],
        "source_identity": source_identity,
        "contracts": contracts,
    }
    contract_text = _json_text(contract_catalog)
    provisional_manifest = _manifest_document(
        record,
        source_identity,
        contracts,
        "sha256:" + hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
    )
    provisional_manifest_text = _json_text(provisional_manifest)
    unsigned_index = {
        "index_api_version": "io.tobkiri.pack-artifact-index.v4",
        "pack_id": record["pack_id"],
        "source_identity": source_identity,
        "artifacts": [
            {
                "path": "pack.v4.json",
                "digest": "sha256:"
                + hashlib.sha256(provisional_manifest_text.encode("utf-8")).hexdigest(),
                "role": "canonical_manifest",
            },
            {
                "path": "contracts.v4.json",
                "digest": "sha256:" + hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
                "role": "contract_catalog",
            },
            *[
                {
                    "path": item["path"],
                    "digest": item["digest"],
                    "role": item.get("index_role", "runtime"),
                }
                for item in record["runtime_artifacts"]
            ],
        ],
        "artifact_set_digest": provisional_manifest["integrity"]["artifact_set_digest"],
    }
    provisional_index = {
        **unsigned_index,
        "integrity_seal": {
            "algorithm": "sha256-canonical-v1",
            "signed_digest": _digest(unsigned_index),
        },
    }
    executable = _render_document(
        str(record["pack_id"]),
        ECOSYSTEM / str(record["pack_id"]),
        provisional_manifest,
        contract_catalog,
        provisional_index,
    )
    executable_text = _json_text(executable)
    executable_artifact_digest = (
        "sha256:" + hashlib.sha256(executable_text.encode("utf-8")).hexdigest()
    )
    manifest = _manifest_document(
        record,
        source_identity,
        contracts,
        "sha256:" + hashlib.sha256(contract_text.encode("utf-8")).hexdigest(),
        executable_artifact_digest,
    )
    manifest_text = _json_text(manifest)
    unsigned_index["artifacts"] = [
        {
            **item,
            "digest": (
                _file_digest_text(manifest_text)
                if item["path"] == "pack.v4.json"
                else item["digest"]
            ),
        }
        for item in unsigned_index["artifacts"]
    ]
    unsigned_index["artifacts"].append(
        {
            "path": "executables.v4.json",
            "digest": executable_artifact_digest,
            "role": "sidecar",
        }
    )
    unsigned_index["artifacts"].sort(key=lambda item: item["path"])
    unsigned_index["artifact_set_digest"] = manifest["integrity"]["artifact_set_digest"]
    index = {
        **unsigned_index,
        "integrity_seal": {
            "algorithm": "sha256-canonical-v1",
            "signed_digest": _digest(unsigned_index),
        },
    }
    validate_document(manifest, "pack")
    validate_document(contract_catalog, "pack_contract_catalog")
    validate_document(index, "pack_artifact_index")
    return {
        "pack.v4.json": manifest_text,
        "contracts.v4.json": contract_text,
        "artifact-index.v4.json": _json_text(index),
        "executables.v4.json": executable_text,
    }


def verify_rendered_artifacts(files: Mapping[str, str]) -> None:
    """Fail closed on a malformed, tampered, or internally stale artifact set."""
    try:
        manifest = validate_document(files["pack.v4.json"], "pack")
        contracts = validate_document(files["contracts.v4.json"], "pack_contract_catalog")
        index = validate_document(files["artifact-index.v4.json"], "pack_artifact_index")
        executable = validate_document(files["executables.v4.json"], "executable_catalog")
    except KeyError as exc:
        raise PackV4MigrationError(f"missing generated artifact: {exc.args[0]}") from exc
    if not (
        manifest["pack"]["id"]
        == contracts["pack_id"]
        == index["pack_id"]
        == executable["pack_id"]
    ):
        raise PackV4MigrationError("generated Pack identities disagree")
    if not (
        manifest["integrity"]["source_identity"]
        == contracts["source_identity"]
        == index["source_identity"]
        == executable["source_identity"]
    ):
        raise PackV4MigrationError("generated source identities disagree")
    _verify_function_operation_principals(manifest)
    entries = {item["path"]: item for item in index["artifacts"]}
    for name in ("pack.v4.json", "contracts.v4.json"):
        expected = "sha256:" + hashlib.sha256(files[name].encode("utf-8")).hexdigest()
        if entries.get(name, {}).get("digest") != expected:
            raise PackV4MigrationError(f"artifact index digest mismatch: {name}")
    if manifest["integrity"]["contract_catalog_digest"] != entries["contracts.v4.json"]["digest"]:
        raise PackV4MigrationError("manifest contract catalog digest is stale")
    expected_artifact_digest = _artifact_set_digest(manifest["artifacts"])
    if (
        manifest["pack"]["artifact_digest"] != expected_artifact_digest
        or manifest["integrity"]["artifact_set_digest"] != expected_artifact_digest
        or index["artifact_set_digest"] != expected_artifact_digest
    ):
        raise PackV4MigrationError("generated Pack artifact set digest is stale")
    manifest_entries = {item["path"]: item["digest"] for item in manifest["artifacts"]}
    for path, digest in manifest_entries.items():
        if entries.get(path, {}).get("digest") != digest:
            raise PackV4MigrationError(f"artifact index digest mismatch: {path}")
    executable_raw_digest = _file_digest_text(files["executables.v4.json"])
    if entries.get("executables.v4.json", {}).get("role") != "sidecar":
        raise PackV4MigrationError("artifact index does not pin executable catalog")
    if entries["executables.v4.json"]["digest"] != executable_raw_digest:
        raise PackV4MigrationError("executable catalog artifact digest is stale")
    unsigned_executable = {
        key: value for key, value in executable.items() if key != "catalog_digest"
    }
    if executable["catalog_digest"] != _digest(unsigned_executable):
        raise PackV4MigrationError("executable catalog digest is stale")
    unsigned = {key: value for key, value in index.items() if key != "integrity_seal"}
    if index["integrity_seal"]["signed_digest"] != _digest(unsigned):
        raise PackV4MigrationError("artifact index integrity seal is invalid")
    for contract in contracts["contracts"]:
        unsigned_contract = {
            key: value
            for key, value in contract.items()
            if key not in {"revision_digest", "provenance"}
        }
        if contract["revision_digest"] != _digest(unsigned_contract):
            raise PackV4MigrationError(f"contract revision is stale: {contract['contract_id']}")
        for operation in contract["operations"]:
            for key in (
                "input_schema_digest",
                "output_schema_digest",
                "error_schema_digest",
            ):
                digest = operation[key]
                schema = contract["schema_catalog"].get(digest)
                if schema is None or _digest(schema) != digest:
                    raise PackV4MigrationError(
                        f"operation schema digest is invalid: {operation['operation_id']}"
                    )


def _verify_function_operation_principals(manifest: Mapping[str, Any]) -> None:
    """Require one finite v4 principal path for every executable operation.

    The source catalog may retain legacy operations for offline migration
    evidence, but generated authority artifacts contain only canonical
    Contract/Operation/Function relationships.  Declarative Packs therefore
    have no executable catalog at all.
    """

    pack = manifest.get("pack")
    requirements = manifest.get("requirements")
    functions = manifest.get("functions")
    contracts = manifest.get("contracts")
    operation_catalog = manifest.get("operation_catalog")
    provider_catalog = manifest.get("provider_catalog")
    if not all(
        isinstance(value, list)
        for value in (functions, contracts, operation_catalog, provider_catalog)
    ) or not isinstance(pack, Mapping) or not isinstance(requirements, Mapping):
        raise PackV4MigrationError("generated Pack v4 principal fields are malformed")

    pack_id = str(pack.get("id") or "")
    boundary = requirements.get("execution_boundary")
    if boundary == "host_brokered":
        expected_role = "host_capability_provider"
    elif boundary == "sandbox":
        expected_role = "brokered"
    elif boundary in {"declarative_only", "remote"}:
        expected_role = None
    else:
        raise PackV4MigrationError(f"unknown Pack execution boundary: {pack_id}")

    if len({item.get("id") for item in functions if isinstance(item, Mapping)}) != len(functions):
        raise PackV4MigrationError(f"duplicate Function principal in Pack: {pack_id}")
    contract_by_revision = {
        item.get("revision_digest"): item
        for item in contracts
        if isinstance(item, Mapping)
    }
    expected_operations: set[tuple[str, str, str]] = set()
    expected_providers: set[tuple[str, str, tuple[str, ...]]] = set()
    for function in functions:
        if not isinstance(function, Mapping):
            raise PackV4MigrationError(f"malformed Function principal in Pack: {pack_id}")
        function_id = str(function.get("id") or "")
        operations = function.get("operations")
        if not isinstance(operations, list) or not operations:
            raise PackV4MigrationError(f"Function has no Operations: {pack_id}/{function_id}")
        if expected_role is not None and function.get("role") != expected_role:
            raise PackV4MigrationError(
                f"Function role does not match Pack boundary: {pack_id}/{function_id}"
            )
        revision = function.get("contract_revision_digest")
        contract = contract_by_revision.get(revision)
        if not isinstance(contract, Mapping):
            raise PackV4MigrationError(
                f"Function Contract revision is missing: {pack_id}/{function_id}"
            )
        contract_operations = {
            str(item.get("operation_id") if isinstance(item, Mapping) else item)
            for item in contract.get("operations", [])
        }
        if set(operations) - contract_operations:
            raise PackV4MigrationError(
                f"Function references an unknown Operation: {pack_id}/{function_id}"
            )
        implementation_digest = function.get("implementation_digest")
        artifact_digests = {
            item.get("digest")
            for item in manifest.get("artifacts", [])
            if isinstance(item, Mapping)
        }
        if implementation_digest not in artifact_digests:
            raise PackV4MigrationError(
                f"Function implementation is not in the Pack artifact set: {pack_id}/{function_id}"
            )
        expected_providers.add(
            (function_id, str(contract.get("contract_id")), tuple(operations))
        )
        for operation_id in operations:
            expected_operations.add(
                (str(operation_id), str(contract.get("contract_id")), function_id)
            )

    if functions:
        if any(
            not isinstance(item, Mapping)
            or item.get("source_kind") != "canonical_v4_contract"
            for item in operation_catalog
        ):
            raise PackV4MigrationError(
                f"legacy Operation source leaked into Pack v4 authority: {pack_id}"
            )
    elif contracts or operation_catalog or provider_catalog:
        raise PackV4MigrationError(
            f"declarative Pack has executable catalog entries: {pack_id}"
        )

    actual_operations = {
        (
            str(item.get("operation_id")),
            str(item.get("contract_reference")),
            str(item.get("provider_id")),
        )
        for item in operation_catalog
        if isinstance(item, Mapping)
    }
    actual_providers = {
        (
            str(item.get("provider_id")),
            str(item.get("contract_reference")),
            tuple(item.get("operations", ())),
        )
        for item in provider_catalog
        if isinstance(item, Mapping)
    }
    if actual_operations != expected_operations:
        raise PackV4MigrationError(f"Operation catalog does not match Functions: {pack_id}")
    if actual_providers != expected_providers:
        raise PackV4MigrationError(f"Provider catalog does not match Functions: {pack_id}")


def _validate_catalog_payload(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    if payload.get("catalog_api_version") != "io.tobkiri.pack-source-catalog.v1":
        raise PackV4MigrationError("unknown canonical Pack source catalog version")
    if payload.get("excluded_packs") != sorted(EXCLUDED_PACKS):
        raise PackV4MigrationError("canonical catalog exclusion drift")
    pack_ids = payload.get("pack_ids")
    records = payload.get("packs")
    if not isinstance(pack_ids, list) or not isinstance(records, list):
        raise PackV4MigrationError("canonical catalog Pack inventory is malformed")
    record_ids = [record.get("pack_id") for record in records if isinstance(record, Mapping)]
    if len(record_ids) != len(records):
        raise PackV4MigrationError("canonical catalog contains a malformed Pack record")
    if len(record_ids) != len(set(record_ids)):
        raise PackV4MigrationError("canonical catalog contains duplicate Pack IDs")
    if pack_ids != sorted(pack_ids) or record_ids != pack_ids:
        raise PackV4MigrationError("canonical catalog has missing or unknown Pack IDs")
    if len(records) != 143:
        raise PackV4MigrationError("canonical catalog must contain exactly 143 Packs")
    required = {
        "version",
        "kind",
        "display_name",
        "dependencies",
        "required_contracts",
        "capabilities",
        "network",
        "secrets",
        "execution_boundary",
        "approval_policy",
        "workspace_boundary",
        "provided_contracts",
        "legacy_operations",
        "runtime_artifacts",
        "legacy_ids",
        "migration",
        "source_evidence",
        "authority",
        "source_provenance",
    }
    for record in records:
        missing = required - set(record)
        if missing:
            raise PackV4MigrationError(
                f"malformed canonical Pack record {record['pack_id']}: {sorted(missing)}"
            )
        provenance = record.get("source_provenance")
        if (
            record.get("authority") != "v4-authoritative"
            or not isinstance(provenance, Mapping)
            or provenance.get("owner") != record["pack_id"]
            or provenance.get("mode") not in {"offline-one-way-import", "canonical-v4"}
        ):
            raise PackV4MigrationError(
                f"canonical v4 authority provenance is invalid: {record['pack_id']}"
            )
    return records


def _verify_global_uniqueness(rendered: Mapping[str, Mapping[str, str]]) -> None:
    owners: dict[str, str] = {}
    providers: dict[str, str] = {}
    operations: dict[str, str] = {}
    for pack_id, files in rendered.items():
        verify_rendered_artifacts(files)
        manifest = json.loads(files["pack.v4.json"])
        contract_catalog = json.loads(files["contracts.v4.json"])
        for contract in contract_catalog["contracts"]:
            owner = contract["owner"]
            prior = owners.setdefault(contract["contract_id"], owner)
            if prior != owner:
                raise PackV4MigrationError(
                    f"duplicate contract owner {contract['contract_id']}: {prior}, {owner}"
                )
        for provider in manifest["provider_catalog"]:
            prior = providers.setdefault(provider["provider_id"], pack_id)
            if prior != pack_id:
                raise PackV4MigrationError(f"duplicate provider {provider['provider_id']}")
        for operation in manifest["operation_catalog"]:
            prior = operations.setdefault(operation["operation_id"], pack_id)
            if prior != pack_id:
                raise PackV4MigrationError(f"duplicate operation {operation['operation_id']}")


def generate(*, check: bool) -> dict[str, int]:
    if not CATALOG.is_file():
        raise PackV4MigrationError("missing canonical Pack v4 source catalog")
    payload = json.loads(CATALOG.read_text(encoding="utf-8"))
    records = _validate_catalog_payload(payload)
    rendered = {record["pack_id"]: _render_record(record) for record in records}
    _verify_global_uniqueness(rendered)
    for pack_id, files in rendered.items():
        pack_root = ECOSYSTEM / pack_id
        if not check:
            pack_root.mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            path = pack_root / name
            if check:
                if not path.is_file() or path.read_text(encoding="utf-8") != text:
                    raise PackV4MigrationError(f"generated Pack v4 artifact drift: {path}")
            else:
                path.write_text(text, encoding="utf-8")
    return {
        "packs": len(rendered),
        "valid": len(rendered),
        "contracts": sum(
            len(json.loads(files["pack.v4.json"])["contracts"]) for files in rendered.values()
        ),
        "operations": sum(
            len(json.loads(files["pack.v4.json"])["operation_catalog"])
            for files in rendered.values()
        ),
    }


def main() -> None:
    """Run the one-way importer or deterministic v4 artifact generator."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--import-legacy", action="store_true")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.import_legacy:
        import_legacy(check=args.check)
        if args.check:
            generate(check=True)
        return
    print(json.dumps(generate(check=args.check), sort_keys=True))


if __name__ == "__main__":
    main()
