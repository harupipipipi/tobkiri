"""Reproducible architecture inventory generation and drift checking."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Mapping

from .canonical import strict_loads
from .provenance import repository_commit, repository_tree_digest, sha256_file
from .scanners import (
    SCANNER_VERSION,
    scan_duplicate_ids,
    scan_fallbacks,
    scan_repository_legacy_inventory,
    scan_v4_scope,
)
from .validation import validate_document

INVENTORY_SCHEMA = "io.tobkiri.architecture.inventory.v1"
INVENTORY_RELATIVE_PATH = "tobkiri_runtime/generated/architecture/architecture_inventory.json"
DESIGN_INPUTS_RELATIVE_PATH = "tobkiri_runtime/docs/PACK_ARCHITECTURE_DESIGN_INPUTS.json"
NORMATIVE_DOCUMENTS = (
    "tobkiri_runtime/docs/TOBKIRI_PACK_ARCHITECTURE_IMPLEMENTATION_PLAN.txt",
    "tobkiri_runtime/docs/ADR-014_BOUNDARY_CAPABILITY_GRANTS.txt",
    "tobkiri_runtime/docs/ADR-015_RUNTIME_SECURITY_LIFECYCLE.txt",
    "tobkiri_runtime/docs/ADR-016_BASE_SHELL_APPLICATION_MODEL.txt",
)


def generate_inventory(root: Path) -> dict[str, Any]:
    """Generate the complete deterministic inventory for one repository tree."""
    root = root.resolve()
    included_paths = _included_paths(root)
    design_inputs = _read_json(root / DESIGN_INPUTS_RELATIVE_PATH)
    manifest_records, artifact_records, state_owners = _manifest_records(root)
    v4_findings = scan_v4_scope(root)
    legacy_findings = scan_repository_legacy_inventory(root)
    duplicate_findings = scan_duplicate_ids(root)
    fallback_findings = scan_fallbacks(root)

    inventory: dict[str, Any] = {
        "schema": INVENTORY_SCHEMA,
        "scanner_version": SCANNER_VERSION,
        "repository": {
            "commit": repository_commit(root),
            "tree_digest": repository_tree_digest(root, included_paths),
        },
        "inputs": {
            "included_paths": [
                path.relative_to(root).as_posix() for path in included_paths
            ],
            "excluded_paths": [INVENTORY_RELATIVE_PATH],
            "normative_documents": _normative_records(root),
            "external_inputs": _external_inputs(design_inputs),
        },
        "records": {
            "artifacts": artifact_records,
            "manifests": manifest_records,
            "execution_routes": [
                {
                    "path": finding.path,
                    "rule_id": finding.rule_id,
                    "line": finding.line,
                    "evidence_digest": sha256_file(root / finding.path)
                    if (root / finding.path).is_file()
                    else "sha256:" + "0" * 64,
                }
                for finding in legacy_findings
            ],
            "state_owners": state_owners,
        },
        "findings": {
            "v4": [finding.to_dict() for finding in v4_findings],
            "legacy": [finding.to_dict() for finding in legacy_findings],
            "duplicates": [finding.to_dict() for finding in duplicate_findings],
            "fallbacks": [finding.to_dict() for finding in fallback_findings],
        },
    }
    # Validate the generated shape before a caller writes it.  This catches a
    # scanner/schema drift immediately instead of producing an invalid artifact.
    validate_document(inventory, "inventory")
    return inventory


def write_inventory(root: Path, output: Path | None = None) -> Path:
    """Write a pretty, stable inventory JSON file and return its path."""
    target = output or (root / INVENTORY_RELATIVE_PATH)
    payload = generate_inventory(root)
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def inventory_drift(root: Path, path: Path | None = None) -> bool:
    """Return whether a tracked inventory differs from regenerated data."""
    target = path or (root / INVENTORY_RELATIVE_PATH)
    if not target.is_file():
        return True
    try:
        existing = strict_loads(target.read_bytes())
    except Exception:
        return True
    try:
        generated = generate_inventory(root)
    except Exception:
        return True
    # The commit is informative provenance and necessarily changes after the
    # inventory itself is committed.  The content tree digest remains the
    # deterministic drift guard.
    if isinstance(existing, dict) and isinstance(generated, dict):
        existing = _without_commit(existing)
        generated = _without_commit(generated)
    return existing != generated


def _without_commit(value: dict[str, Any]) -> dict[str, Any]:
    copied = json.loads(json.dumps(value))
    repository = copied.get("repository")
    if isinstance(repository, dict):
        repository.pop("commit", None)
    return copied


def _included_paths(root: Path) -> list[Path]:
    candidates: set[Path] = set()
    protocol_root = root / "tobkiri_runtime" / "tobkiri_protocol"
    if protocol_root.is_dir():
        candidates.update(
            path
            for path in protocol_root.rglob("*")
            if path.is_file()
            and "generated" not in path.parts
            and "__pycache__" not in path.parts
            and path.suffix != ".pyc"
        )
    for relative in NORMATIVE_DOCUMENTS + (DESIGN_INPUTS_RELATIVE_PATH,):
        path = root / relative
        if path.is_file():
            candidates.add(path)
    for base in (
        root / "tobkiri_runtime" / "ecosystem",
        root / "tobkiri_runtime" / "profiles_v4",
        root / "tobkiri_runtime" / "packs_v4",
        root / "tobkiri_runtime" / "distributions",
    ):
        if not base.is_dir():
            continue
        candidates.update(
            path
            for path in base.rglob("*")
            if path.is_file()
            and (
                path.name in {"ecosystem.json", "rumi.pack.v3.json"}
                or path.suffix.lower() in {".profile.yaml", ".profile.yml"}
            )
        )
    return sorted(candidates)


def _manifest_records(root: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    manifests: list[dict[str, Any]] = []
    artifacts: list[dict[str, Any]] = []
    owners: list[dict[str, Any]] = []
    paths = sorted(
        set(
            list((root / "tobkiri_runtime" / "ecosystem").glob("*/ecosystem.json"))
            + list((root / "tobkiri_runtime" / "ecosystem").glob("*/rumi.pack.v3.json"))
            + list((root / "tobkiri_runtime" / "packs_v4").glob("**/*manifest*.json"))
        )
    )
    for path in paths:
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix()
        payload = _read_json(path)
        if not isinstance(payload, Mapping):
            continue
        pack = payload.get("pack") if isinstance(payload.get("pack"), Mapping) else payload
        pack_id = str(pack.get("id") or pack.get("pack_id") or path.parent.name)
        version = pack.get("version")
        legacy = payload.get("pack_api_version") == "rumi.pack.v3" or path.name == "ecosystem.json"
        digest = sha256_file(path)
        manifests.append(
            {
                "path": relative,
                "pack_id": pack_id,
                "version": str(version) if version is not None else None,
                "manifest_digest": digest,
                "legacy": legacy,
            }
        )
        artifacts.append(
            {
                "path": relative,
                "kind": "manifest",
                "digest": digest,
                "identity": pack_id,
                "version": str(version) if version is not None else None,
                "legacy": legacy,
            }
        )
        for storage in _storage_records(payload):
            owners.append(
                {
                    "path": relative,
                    "owner": pack_id,
                    "namespace": storage[0],
                    "schema_revision": storage[1],
                }
            )
    return manifests, artifacts, sorted(owners, key=lambda item: tuple(item.values()))


def _storage_records(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, Mapping):
        storage = value.get("storage")
        if isinstance(storage, list):
            for item in storage:
                if isinstance(item, Mapping):
                    namespace = item.get("namespace")
                    revision = item.get("schema_version") or item.get("schema_revision")
                    if isinstance(namespace, str) and isinstance(revision, str):
                        yield namespace, revision
        for child in value.values():
            yield from _storage_records(child)
    elif isinstance(value, list):
        for child in value:
            yield from _storage_records(child)


def _normative_records(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": relative, "digest": sha256_file(root / relative), "normative": True}
        for relative in NORMATIVE_DOCUMENTS
        if (root / relative).is_file()
    ]


def _external_inputs(design_inputs: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = design_inputs.get("informative_external_inputs", [])
    if not isinstance(raw, list):
        return []
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        result.append(
            {
                "name": str(item.get("name", "")),
                "tracked": False,
                "normative": False,
                "reason": str(item.get("reason", "")),
            }
        )
    return result


def _read_json(path: Path) -> Mapping[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = strict_loads(path.read_bytes())
    except Exception:
        return {}
    return value if isinstance(value, Mapping) else {}
