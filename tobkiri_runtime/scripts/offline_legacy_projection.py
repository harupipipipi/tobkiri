"""Offline-only one-way projections generated from ``rumi.pack.v3`` data."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from core_runtime.global_contracts.canonical import content_identity

LEGACY_ECOSYSTEM_FORMAT = "rumi.ecosystem.v1"
PROJECTION_GENERATOR = "tobkiri.core_runtime.manifest_projection/v2"
PROJECTION_OWNER = "scripts/offline_legacy_projection.py"
PROJECTION_SOURCE = "rumi.pack.v3.json"
RUNTIME_EXECUTABLE = False


class ManifestProjectionError(ValueError):
    """Raised when a generated compatibility projection is unsafe or stale."""


def source_manifest_identity(manifest: Mapping[str, Any]) -> str:
    """Return the stable identity of canonical input without self-reference.

    A generated artifact records this value as provenance.  The declared Pack
    content hash remains the Pack author's integrity assertion; it is not
    rewritten by a compatibility generator.
    """
    payload = json.loads(json.dumps(manifest, ensure_ascii=False))
    payload.pop("content_identity", None)
    provenance = payload.get("provenance")
    if isinstance(provenance, dict):
        provenance.pop("content_hash", None)
    return content_identity(payload)


def project_legacy_ecosystem(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """Project a v3 manifest into the read-only legacy ``ecosystem.json`` form.

    Only data with an unambiguous legacy representation is emitted.  In
    particular, a contract requirement is never guessed to be a Pack
    dependency, and no host execution authority is inferred from a v3
    permission.  This makes the projection suitable for compatibility reads,
    never for authority escalation or a second source of truth.
    """
    pack = _mapping(manifest.get("pack"), "pack")
    contracts = _mapping(manifest.get("contracts"), "contracts")
    provenance = _mapping(manifest.get("provenance"), "provenance")
    extensions = manifest.get("extensions")
    extensions = extensions if isinstance(extensions, Mapping) else {}
    legacy_options = extensions.get("rumi.legacy_projection")
    legacy_options = (
        legacy_options if isinstance(legacy_options, Mapping) else {}
    )

    pack_id = str(legacy_options.get("pack_id") or "").strip()
    if not pack_id:
        pack_id = str(pack["id"]).replace(".", "_") + "_pack"
    dependencies = legacy_options.get("dependencies", {})
    if not isinstance(dependencies, (dict, list)):
        raise ManifestProjectionError(
            "extensions.rumi.legacy_projection.dependencies must be an object or list"
        )
    host_execution = legacy_options.get("host_execution", False)
    if not isinstance(host_execution, bool):
        raise ManifestProjectionError(
            "extensions.rumi.legacy_projection.host_execution must be a boolean"
        )

    compatibility_manifest = legacy_options.get("manifest", {})
    if not isinstance(compatibility_manifest, Mapping):
        raise ManifestProjectionError(
            "extensions.rumi.legacy_projection.manifest must be an object"
        )
    projection = json.loads(
        json.dumps(dict(compatibility_manifest), ensure_ascii=False)
    )
    if not isinstance(projection, dict):
        raise ManifestProjectionError("legacy compatibility manifest must be an object")

    provides = _contract_ids(contracts.get("provides"))
    requires = _contract_ids(contracts.get("requires"))
    capabilities = sorted(
        {
            str(item)
            for contract in _list_of_mappings(contracts.get("provides"))
            for item in contract.get("required_capabilities", [])
            if isinstance(item, str) and item.strip()
        }
        | {
            str(permission["capability"])
            for permission in _list_of_mappings(manifest.get("permissions"))
            if isinstance(permission.get("capability"), str)
            and str(permission["capability"]).strip()
        }
    )
    source_identity = source_manifest_identity(manifest)
    metadata = projection.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    metadata.update({
        "manifest_authority": "v3-authoritative",
        "format": LEGACY_ECOSYSTEM_FORMAT,
        "generated": True,
        "read_only_projection": True,
        "generated_from": {
            "source": "rumi.pack.v3.json",
            "source_content_hash": source_identity,
            "generator": PROJECTION_GENERATOR,
        },
    })
    projection.update({
        "pack_id": pack_id,
        "pack_identity": f"rumi:ecosystem/{pack_id}",
        "display_name": str(pack["display_name"]),
        "version": str(pack["version"]),
        "description": str(pack.get("description") or ""),
        "dependencies": dependencies,
        "connectivity": {"requires": requires, "provides": provides},
        "required_secrets": [],
        "required_capabilities": capabilities,
        "required_network": {"allowed_domains": [], "allowed_ports": []},
        # This is an explicit compatibility declaration, not authority inferred
        # from a loader or a requested capability. Host approval remains the
        # separate runtime gate for in-process Python activation.
        "host_execution": host_execution,
        "resources": [
            str(resource["id"])
            for resource in _list_of_mappings(manifest.get("resources"))
            if isinstance(resource.get("id"), str) and resource["id"].strip()
        ],
        "provenance": dict(provenance),
        "metadata": metadata,
    })
    projection.setdefault("vocabulary", {"types": ["service"]})
    return projection


def render_legacy_ecosystem(manifest: Mapping[str, Any]) -> str:
    """Render a deterministic legacy projection from the canonical manifest."""
    return json.dumps(
        project_legacy_ecosystem(manifest),
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"


def generate_legacy_ecosystem_projection(
    manifest_path: Path,
    output_path: Path,
    *,
    check: bool = False,
) -> str:
    """Write or verify a legacy projection and return its source identity.

    ``check=True`` is intended for CI.  It performs no writes and rejects a
    missing, hand-edited, or stale projection with an actionable error.
    """
    manifest = _read_canonical_manifest(manifest_path)
    expected = render_legacy_ecosystem(manifest)
    if check:
        if not output_path.is_file() or output_path.read_text(
            encoding="utf-8"
        ) != expected:
            raise ManifestProjectionError(
                f"legacy manifest projection drift detected: {output_path}"
            )
        return source_manifest_identity(manifest)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(expected, encoding="utf-8")
    return source_manifest_identity(manifest)


def _read_canonical_manifest(path: Path) -> Mapping[str, Any]:
    """Load a schema-valid v3 input before producing any compatibility data."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ManifestProjectionError(
            f"cannot read canonical manifest {path}: {exc}"
        ) from exc
    if not isinstance(value, Mapping):
        raise ManifestProjectionError("canonical manifest root must be an object")
    if value.get("pack_api_version") != "rumi.pack.v3":
        raise ManifestProjectionError("canonical manifest must use rumi.pack.v3")
    from core_runtime.pack_sdk import PackSdkError, validate_pack_manifest

    schema_path = Path(__file__).parents[1] / "schemas" / "pack_manifest_v3.schema.json"
    try:
        return validate_pack_manifest(path, schema_path=schema_path)
    except PackSdkError as exc:
        raise ManifestProjectionError(
            f"canonical manifest is invalid: {exc}"
        ) from exc


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ManifestProjectionError(f"{field} must be an object")
    return value


def _list_of_mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def _contract_ids(value: Any) -> list[str]:
    return sorted(
        {
            str(contract["id"])
            for contract in _list_of_mappings(value)
            if isinstance(contract.get("id"), str) and contract["id"].strip()
        }
    )
