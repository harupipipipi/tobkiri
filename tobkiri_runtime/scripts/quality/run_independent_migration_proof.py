#!/usr/bin/env python3
"""Generate repository migration evidence without overstating attestation.

The Profile collection/workspace transaction and Pack artifact migration are
different claims.  This command proves the former with a real transactional
import.  For each Pack it records only Pack-specific legacy-source and v4-target
integrity that can be reproduced from the checkout.  A Pack remains
``generated-draft`` until separately authored semantic mappings and comparison
receipts exist; repository-generated hashes are not independent attestation.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

ROOT = Path(__file__).resolve().parents[2]
REPOSITORY_ROOT = ROOT.parent
ECOSYSTEM = ROOT / "ecosystem"
PROFILE_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_profile_bundle.v1.json"
PROFILE_WORKSPACE_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_profile_bundle"
EXECUTABLE_SOURCE_FIXTURE = ROOT / "tests" / "fixtures" / "legacy_executable_sources.v1.json"
EXECUTABLE_SOURCE_REGISTRY = ROOT / "schemas" / "executable_sources.v1.json"
PACK_CATALOG = ROOT / "schemas" / "pack_v4_catalog.v1.json"
DEFAULT_OUTPUT = ROOT / "scripts" / "quality" / "evidence" / "pack_migration_proof.v1.json"
PACK_ARTIFACTS = (
    "artifact-index.v4.json",
    "pack.v4.json",
    "contracts.v4.json",
    "executables.v4.json",
)
RUNNER_ID = "tobkiri.quality.migration-proof-generator"
RUNNER_VERSION = "2.1.0"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.profile_definition_store_v4 import (  # noqa: E402
    ProfileDefinitionStore,
    ProfileDefinitionStoreError,
    ProfileDefinitionStoreIntegrityError,
)
from tobkiri_protocol.canonical import canonical_digest  # noqa: E402


class IndependentMigrationProofError(RuntimeError):
    """Raised when independent migration or artifact verification fails."""


def _production_pack_roots() -> list[Path]:
    """Return the finite Pack roots declared by the canonical v4 catalog."""

    catalog = _load_json(PACK_CATALOG)
    pack_ids = catalog.get("pack_ids")
    if not isinstance(pack_ids, list) or not all(
        isinstance(pack_id, str) and pack_id for pack_id in pack_ids
    ):
        raise IndependentMigrationProofError("canonical Pack catalog is invalid")
    if len(pack_ids) != len(set(pack_ids)):
        raise IndependentMigrationProofError("canonical Pack catalog has duplicate IDs")
    roots = [ECOSYSTEM / pack_id for pack_id in pack_ids]
    missing = [root.name for root in roots if not root.is_dir()]
    if missing:
        raise IndependentMigrationProofError(
            f"canonical Pack roots are missing: {', '.join(sorted(missing))}"
        )
    return sorted(roots)


def _load_json(path: Path) -> Mapping[str, Any]:
    """Load one JSON object for independent proof inspection."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndependentMigrationProofError(f"cannot read JSON input: {path}") from exc
    if not isinstance(value, Mapping):
        raise IndependentMigrationProofError(f"JSON input must be an object: {path}")
    return value


def _file_digest(path: Path) -> str:
    """Return the raw SHA-256 digest for one on-disk input."""

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _label(path: Path) -> str:
    """Return a stable repository-relative path for evidence.

    Absolute/external labels are deliberately rejected because embedding them
    makes an otherwise identical proof differ between checkout locations.
    """

    try:
        return path.resolve().relative_to(REPOSITORY_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise IndependentMigrationProofError(
            f"proof input is outside the repository: {path}"
        ) from exc


def _head_sha() -> str:
    """Read the exact checkout commit used for this proof run."""

    value = subprocess.check_output(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=REPOSITORY_ROOT,
        text=True,
    ).strip()
    if len(value) != 40 or any(character not in "0123456789abcdef" for character in value):
        raise IndependentMigrationProofError("git HEAD is not a full lowercase commit SHA")
    return value


def _identity_proof(source: Mapping[str, Any]) -> dict[str, Any]:
    """Extract and digest all named Profile-scoped identities in the fixture."""

    profiles = source.get("profiles")
    if not isinstance(profiles, list) or len(profiles) < 3:
        raise IndependentMigrationProofError(
            "legacy proof input must contain at least three Profiles"
        )
    identity_sets: dict[str, list[str]] = {
        "profile_ids": [],
        "workspace_ids": [],
        "conversation_ids": [],
        "settings_ids": [],
        "credential_ids": [],
    }
    profile_names: dict[str, str] = {}
    for profile in profiles:
        if not isinstance(profile, Mapping):
            raise IndependentMigrationProofError("legacy Profile is not an object")
        profile_id = profile.get("profile_id")
        display_name = profile.get("display_name")
        workspace = profile.get("workspace")
        settings = profile.get("settings")
        conversations = profile.get("conversation_records")
        credentials = profile.get("credential_refs")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise IndependentMigrationProofError("legacy Profile ID is missing")
        if not isinstance(display_name, str) or not display_name.strip():
            raise IndependentMigrationProofError(f"Profile name is missing: {profile_id}")
        if not isinstance(workspace, Mapping) or not isinstance(
            workspace.get("workspace_id"), str
        ):
            raise IndependentMigrationProofError(f"workspace identity is missing: {profile_id}")
        if not isinstance(settings, Mapping) or not isinstance(
            settings.get("settings_id"), str
        ):
            raise IndependentMigrationProofError(f"settings identity is missing: {profile_id}")
        if not isinstance(conversations, list) or len(conversations) != 1:
            raise IndependentMigrationProofError(
                f"exactly one conversation identity is required: {profile_id}"
            )
        if not isinstance(credentials, list) or len(credentials) != 1:
            raise IndependentMigrationProofError(
                f"exactly one credential identity is required: {profile_id}"
            )
        conversation = conversations[0]
        credential = credentials[0]
        if not isinstance(conversation, Mapping) or not isinstance(
            conversation.get("conversation_id"), str
        ):
            raise IndependentMigrationProofError(
                f"conversation identity is missing: {profile_id}"
            )
        if not isinstance(credential, Mapping) or not isinstance(
            credential.get("credential_id"), str
        ):
            raise IndependentMigrationProofError(
                f"credential identity is missing: {profile_id}"
            )
        credential_ref = credential.get("secret_ref")
        if not isinstance(credential_ref, str) or not credential_ref.startswith("vault://"):
            raise IndependentMigrationProofError(
                f"credential must remain an opaque vault reference: {profile_id}"
            )
        values = (
            ("profile_ids", profile_id),
            ("workspace_ids", workspace["workspace_id"]),
            ("conversation_ids", conversation["conversation_id"]),
            ("settings_ids", settings["settings_id"]),
            ("credential_ids", credential["credential_id"]),
        )
        if any(not isinstance(value, str) or not value.strip() for _, value in values):
            raise IndependentMigrationProofError(f"named identity is empty: {profile_id}")
        for field, value in values:
            if value.casefold() == "defaults" or value.casefold().startswith("defaults-"):
                raise IndependentMigrationProofError(
                    f"legacy identity is collapsed into Defaults: {field}:{value}"
                )
            identity_sets[field].append(value)
        profile_names[profile_id] = display_name

    for field, values in identity_sets.items():
        if len(values) != len(set(values)):
            raise IndependentMigrationProofError(f"duplicate named identity: {field}")
    active_profile_id = source.get("active_profile_id")
    last_launched_profile_id = source.get("last_launched_profile_id")
    if active_profile_id not in identity_sets["profile_ids"]:
        raise IndependentMigrationProofError("active Profile identity is not in the source set")
    if last_launched_profile_id not in identity_sets["profile_ids"]:
        raise IndependentMigrationProofError(
            "last-launched Profile identity is not in the source set"
        )
    unsigned = {
        **identity_sets,
        "profile_names": profile_names,
        "active_profile_id": active_profile_id,
        "last_launched_profile_id": last_launched_profile_id,
        "all_ids_distinct": len(
            {value for values in identity_sets.values() for value in values}
        ) == sum(len(values) for values in identity_sets.values()),
        "defaults_collapsed": False,
    }
    if not unsigned["all_ids_distinct"]:
        raise IndependentMigrationProofError("Profile-scoped identities are not globally distinct")
    return {**unsigned, "digest": canonical_digest(unsigned)}


def _workspace_snapshot(root: Path) -> dict[str, str]:
    """Hash every regular file in one legacy workspace without following links."""

    if root.is_symlink() or not root.is_dir():
        raise IndependentMigrationProofError(f"workspace is not a regular directory: {root}")
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise IndependentMigrationProofError(f"workspace contains a symlink: {path}")
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _file_digest(path)
    return result


class _FailingProfileStore(ProfileDefinitionStore):
    """Inject a state-write failure to prove the migration leaves no commit."""

    def _write_state(self, state: Mapping[str, Any]) -> None:
        del state
        raise ProfileDefinitionStoreError("injected migration state-write failure")


def _assert_uncommitted(root: Path) -> None:
    """Require that an injected transaction created no visible state."""

    if (root / "profiles" / "index.json").exists():
        raise IndependentMigrationProofError("failed migration left a Profile index")
    workspaces = root / "workspaces"
    if workspaces.exists() and any(workspaces.iterdir()):
        raise IndependentMigrationProofError("failed migration left a workspace")


def _copy_broken_workspace_fixture(source: Path, destination: Path) -> Path:
    """Copy the fixture and add one unsafe link for preflight rollback proof."""

    shutil.copytree(source, destination, symlinks=True)
    outside = destination.parent / "outside-proof-target.txt"
    outside.write_text("outside", encoding="utf-8")
    link = destination / "profiles" / "profile-cleo" / "notes" / "escape"
    link.symlink_to(outside)
    return destination


def _run_profile_transaction_proof(
    source: Mapping[str, Any],
    identity: Mapping[str, Any],
    temporary_root: Path,
    *,
    workspace_root: Path = PROFILE_WORKSPACE_FIXTURE,
) -> dict[str, Any]:
    """Exercise failure, commit, restart, and replay paths of Profile import.

    The parsed document is passed to the store instead of its absolute path.
    Workspace discovery is still explicit.  This keeps the committed receipt
    identical after relocating the checkout while exercising the same bytes.
    """

    broken_workspace = _copy_broken_workspace_fixture(
        workspace_root,
        temporary_root / "broken-legacy",
    )
    broken_destination = temporary_root / "broken-destination"
    try:
        ProfileDefinitionStore(broken_destination).import_legacy_collection(
            source,
            legacy_workspace_root=broken_workspace,
        )
    except ProfileDefinitionStoreIntegrityError:
        pass
    else:
        raise IndependentMigrationProofError("symlink preflight unexpectedly committed")
    _assert_uncommitted(broken_destination)

    injected_destination = temporary_root / "injected-destination"
    try:
        _FailingProfileStore(injected_destination).import_legacy_collection(
            source,
            legacy_workspace_root=workspace_root,
        )
    except ProfileDefinitionStoreError:
        pass
    else:
        raise IndependentMigrationProofError("injected transaction unexpectedly committed")
    _assert_uncommitted(injected_destination)

    committed_destination = temporary_root / "committed-destination"
    store = ProfileDefinitionStore(committed_destination, clock=lambda: 1700000300)
    result = store.import_legacy_collection(
        source,
        legacy_workspace_root=workspace_root,
    )
    expected_profile_ids = tuple(str(item["profile_id"]) for item in source["profiles"])
    if result.profile_ids != expected_profile_ids:
        raise IndependentMigrationProofError("Profile order or identity changed during import")
    if result.active_profile_id != source["active_profile_id"]:
        raise IndependentMigrationProofError("active Profile identity changed during import")
    if result.last_launched_profile_id != source["last_launched_profile_id"]:
        raise IndependentMigrationProofError(
            "last-launched Profile identity changed during import"
        )
    if store.legacy_state().get("source_document") != dict(source):
        raise IndependentMigrationProofError("legacy source document was not preserved")

    source_by_id = {str(item["profile_id"]): item for item in source["profiles"]}
    workspace_digests: dict[str, str] = {}
    for profile_id in expected_profile_ids:
        stored = store.get_profile(profile_id)
        if stored is None:
            raise IndependentMigrationProofError(f"imported Profile is missing: {profile_id}")
        actual_profile = copy.deepcopy(dict(stored.profile))
        actual_profile.pop("legacy_migration", None)
        if actual_profile != source_by_id[profile_id]:
            raise IndependentMigrationProofError(
                f"Profile payload is not lossless: {profile_id}"
            )
        source_workspace = workspace_root / "profiles" / profile_id
        migrated_workspace = committed_destination / "workspaces" / profile_id
        source_files = _workspace_snapshot(source_workspace)
        migrated_files = _workspace_snapshot(migrated_workspace)
        if source_files != migrated_files:
            raise IndependentMigrationProofError(
                f"workspace payload is not lossless: {profile_id}"
            )
        workspace_digests[profile_id] = canonical_digest(source_files)

    committed_snapshot = store.snapshot()
    restarted = ProfileDefinitionStore(committed_destination).snapshot()
    if restarted != committed_snapshot:
        raise IndependentMigrationProofError("Profile migration did not survive restart")
    restarted_source = ProfileDefinitionStore(committed_destination).legacy_state()
    if restarted_source.get("source_document") != dict(source):
        raise IndependentMigrationProofError("restart lost the legacy source document")

    before_replay = copy.deepcopy(committed_snapshot)
    try:
        store.import_legacy_collection(
            source,
            legacy_workspace_root=workspace_root,
        )
    except ProfileDefinitionStoreError:
        replay_rejected = True
    else:
        replay_rejected = False
    if not replay_rejected or store.snapshot() != before_replay:
        raise IndependentMigrationProofError("replay changed a committed migration")

    unsigned = {
        "algorithm": "profile-definition-store.import_legacy_collection.v1",
        "source_digest": result.source_digest,
        "committed_store_digest": committed_snapshot["store_digest"],
        "identity_proof_digest": identity["digest"],
        "workspace_digests": workspace_digests,
        "lossless": True,
        "restart_verified": True,
        "replay_rejected_without_mutation": True,
        "failure_injection": {
            "symlink_preflight": {
                "raised": True,
                "committed_state": False,
            },
            "state_write": {
                "raised": True,
                "committed_state": False,
            },
        },
    }
    return {**unsigned, "receipt_digest": canonical_digest(unsigned)}


def _safe_artifact_path(pack_root: Path, relative: str) -> Path:
    """Resolve one v4 artifact path while rejecting traversal."""

    candidate = Path(relative)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise IndependentMigrationProofError(f"artifact path escapes Pack: {pack_root.name}:{relative}")
    resolved = (pack_root / candidate).resolve()
    try:
        resolved.relative_to(pack_root.resolve())
    except ValueError as exc:
        raise IndependentMigrationProofError(
            f"artifact path escapes Pack: {pack_root.name}:{relative}"
        ) from exc
    if not resolved.is_file():
        raise IndependentMigrationProofError(f"artifact is missing: {resolved}")
    return resolved


def _verify_pack_artifacts(pack_root: Path) -> dict[str, Any]:
    """Verify v4 quartet bytes, seals, and digest relationships."""

    documents = {
        name: _load_json(pack_root / name)
        for name in PACK_ARTIFACTS
    }
    manifest = documents["pack.v4.json"]
    contracts = documents["contracts.v4.json"]
    index = documents["artifact-index.v4.json"]
    executable = documents["executables.v4.json"]
    pack_id = manifest.get("pack", {}).get("id") if isinstance(manifest.get("pack"), Mapping) else None
    if not isinstance(pack_id, str) or pack_id != pack_root.name:
        raise IndependentMigrationProofError(f"Pack identity is invalid: {pack_root.name}")
    if not (
        contracts.get("pack_id") == pack_id
        and index.get("pack_id") == pack_id
        and executable.get("pack_id") == pack_id
    ):
        raise IndependentMigrationProofError(f"artifact Pack identities disagree: {pack_id}")
    source_identities = {
        value
        for document in (manifest, contracts, index, executable)
        for value in [document.get("integrity", {}).get("source_identity") if document is manifest else document.get("source_identity")]
        if isinstance(value, str)
    }
    if len(source_identities) != 1:
        raise IndependentMigrationProofError(f"artifact source identities disagree: {pack_id}")

    manifest_artifacts = manifest.get("artifacts")
    index_artifacts = index.get("artifacts")
    if not isinstance(manifest_artifacts, list) or not isinstance(index_artifacts, list):
        raise IndependentMigrationProofError(f"artifact lists are invalid: {pack_id}")
    expected_artifact_digest = canonical_digest(manifest_artifacts)
    if not (
        manifest.get("pack", {}).get("artifact_digest") == expected_artifact_digest
        and manifest.get("integrity", {}).get("artifact_set_digest") == expected_artifact_digest
        and index.get("artifact_set_digest") == expected_artifact_digest
    ):
        raise IndependentMigrationProofError(f"artifact set digest is stale: {pack_id}")
    manifest_by_path = {
        str(item.get("path")): item
        for item in manifest_artifacts
        if isinstance(item, Mapping) and item.get("path")
    }
    index_by_path = {
        str(item.get("path")): item
        for item in index_artifacts
        if isinstance(item, Mapping) and item.get("path")
    }
    if len(manifest_by_path) != len(manifest_artifacts):
        raise IndependentMigrationProofError(f"Pack artifact paths are duplicated: {pack_id}")
    if len(index_by_path) != len(index_artifacts):
        raise IndependentMigrationProofError(f"index artifact paths are duplicated: {pack_id}")
    for relative, item in index_by_path.items():
        expected_digest = item.get("digest")
        if not isinstance(expected_digest, str):
            raise IndependentMigrationProofError(
                f"artifact digest is missing: {pack_id}:{relative}"
            )
        if _file_digest(_safe_artifact_path(pack_root, relative)) != expected_digest:
            raise IndependentMigrationProofError(f"artifact bytes are stale: {pack_id}:{relative}")
        if (
            relative in manifest_by_path
            and manifest_by_path[relative].get("digest") != expected_digest
        ):
            raise IndependentMigrationProofError(
                f"manifest/index digest mismatch: {pack_id}:{relative}"
            )
    for relative in manifest_by_path:
        if relative not in index_by_path:
            raise IndependentMigrationProofError(
                f"manifest artifact is not indexed: {pack_id}:{relative}"
            )
    if index.get("integrity_seal", {}).get("signed_digest") != canonical_digest(
        {key: value for key, value in index.items() if key != "integrity_seal"}
    ):
        raise IndependentMigrationProofError(f"artifact index seal is stale: {pack_id}")
    if manifest.get("integrity", {}).get("contract_catalog_digest") != index_by_path.get(
        "contracts.v4.json", {}
    ).get("digest"):
        raise IndependentMigrationProofError(f"contract catalog digest is stale: {pack_id}")
    if executable.get("catalog_digest") != canonical_digest(
        {key: value for key, value in executable.items() if key != "catalog_digest"}
    ):
        raise IndependentMigrationProofError(f"executable catalog digest is stale: {pack_id}")
    for contract in contracts.get("contracts", []):
        if not isinstance(contract, Mapping):
            raise IndependentMigrationProofError(f"contract record is invalid: {pack_id}")
        unsigned_contract = {
            key: value
            for key, value in contract.items()
            if key not in {"revision_digest", "provenance"}
        }
        if contract.get("revision_digest") != canonical_digest(unsigned_contract):
            raise IndependentMigrationProofError(
                f"contract revision is stale: {pack_id}:{contract.get('contract_id')}"
            )
    return {
        "pack_id": pack_id,
        "artifact_set_digest": expected_artifact_digest,
        "artifact_count": len(index_by_path),
        "artifact_index_digest": _file_digest(pack_root / "artifact-index.v4.json"),
        "executable_catalog_digest": executable["catalog_digest"],
        "quartet": {name: _file_digest(pack_root / name) for name in PACK_ARTIFACTS},
    }


def _source_inputs() -> list[dict[str, str]]:
    """Collect every repository file read by the two proof sections."""

    paths: list[tuple[str, Path]] = [
        ("proof-algorithm", Path(__file__)),
        (
            "profile-import-algorithm",
            ROOT / "core_runtime" / "profile_definition_store_v4.py",
        ),
        ("canonical-digest-algorithm", ROOT / "tobkiri_protocol" / "canonical.py"),
        ("legacy-profile-bundle", PROFILE_FIXTURE),
    ]
    paths.append(("legacy-executable-source-fixture", EXECUTABLE_SOURCE_FIXTURE))
    paths.append(("hardened-executable-source-registry", EXECUTABLE_SOURCE_REGISTRY))
    for workspace_file in sorted(PROFILE_WORKSPACE_FIXTURE.rglob("*")):
        if workspace_file.is_file() and not workspace_file.is_symlink():
            paths.append(("legacy-workspace-file", workspace_file))
    for v3_path in sorted(ECOSYSTEM.glob("*/rumi.pack.v3.json")):
        paths.append(("legacy-v3-manifest", v3_path))
        artifact_manifest = v3_path.parent / "artifact-manifest.json"
        if artifact_manifest.is_file():
            paths.append(("legacy-artifact-manifest", artifact_manifest))
    for pack_root in _production_pack_roots():
        for name in PACK_ARTIFACTS:
            paths.append(("v4-artifact", pack_root / name))
        index = _load_json(pack_root / "artifact-index.v4.json")
        artifacts = index.get("artifacts")
        if not isinstance(artifacts, list):
            raise IndependentMigrationProofError(
                f"artifact index entries are invalid: {pack_root.name}"
            )
        for artifact in artifacts:
            if not isinstance(artifact, Mapping) or not isinstance(
                artifact.get("path"), str
            ):
                raise IndependentMigrationProofError(
                    f"artifact index entry is invalid: {pack_root.name}"
                )
            paths.append(("v4-indexed-artifact", pack_root / artifact["path"]))
    unique: dict[str, tuple[str, Path]] = {}
    for kind, path in paths:
        label = _label(path)
        previous = unique.get(label)
        if previous is not None and previous[0] != kind:
            continue
        unique[label] = (kind, path)
    return [
        {"kind": kind, "path": label, "digest": _file_digest(path)}
        for label, (kind, path) in sorted(unique.items())
    ]


def _semantic_document(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return proof content without informational or self-digest fields."""

    normalized = copy.deepcopy(dict(document))
    source = normalized.get("source")
    if isinstance(source, dict):
        source.pop("observed_head_sha", None)
        source.pop("content_digest", None)
    return normalized


def _pack_source_record(
    pack_root: Path,
    explicit_packs: Mapping[str, Any],
    registry_records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Return a Pack-specific legacy source record, or an honest missing record."""

    pack_id = pack_root.name
    v3_path = pack_root / "rumi.pack.v3.json"
    if v3_path.is_file():
        paths = [v3_path]
        artifact_manifest = pack_root / "artifact-manifest.json"
        if artifact_manifest.is_file():
            paths.append(artifact_manifest)
        files = [{"path": _label(path), "digest": _file_digest(path)} for path in paths]
        source = {
            "status": "available",
            "pack_id": pack_id,
            "format": "rumi.pack.v3",
            "files": files,
            "digest": canonical_digest(files),
        }
        if registry_records:
            source["executable_source_registry"] = {
                "path": _label(EXECUTABLE_SOURCE_REGISTRY),
                "digest": canonical_digest(registry_records),
                "record_count": len(registry_records),
            }
        return source

    explicit = explicit_packs.get(pack_id)
    if isinstance(explicit, Mapping):
        payload = copy.deepcopy(dict(explicit))
        return {
            "status": "available",
            "pack_id": pack_id,
            "format": "legacy-entrypoint-map",
            "files": [
                {
                    "path": _label(EXECUTABLE_SOURCE_FIXTURE),
                    "json_pointer": f"/packs/{pack_id}",
                    "digest": canonical_digest(payload),
                }
            ],
            "digest": canonical_digest(payload),
            "executable_source_registry": {
                "path": _label(EXECUTABLE_SOURCE_REGISTRY),
                "digest": canonical_digest(registry_records),
                "record_count": len(registry_records),
            },
        }

    return {
        "status": "missing",
        "pack_id": pack_id,
        "format": None,
        "files": [],
        "digest": None,
        "reason": "no Pack-specific legacy source record is present",
    }


def _v4_executable_records(pack_root: Path) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[tuple[str, str], Mapping[str, Any]],
]:
    """Index v4 variants and operations by their exact identities."""

    executable = _load_json(pack_root / "executables.v4.json")
    variants: dict[str, Mapping[str, Any]] = {}
    operations: dict[tuple[str, str], Mapping[str, Any]] = {}
    for variant in executable.get("variants", []):
        if not isinstance(variant, Mapping):
            continue
        function_id = variant.get("function_id")
        if not isinstance(function_id, str):
            continue
        variants[function_id] = variant
        for operation in variant.get("operations", []):
            if isinstance(operation, Mapping) and isinstance(
                operation.get("operation_id"), str
            ):
                operations[(function_id, operation["operation_id"])] = operation
    return variants, operations


def _v4_contract_records(pack_root: Path) -> dict[str, Mapping[str, Any]]:
    """Index v4 Contracts by their exact identities."""

    catalog = _load_json(pack_root / "contracts.v4.json")
    return {
        str(contract["contract_id"]): contract
        for contract in catalog.get("contracts", [])
        if isinstance(contract, Mapping) and isinstance(contract.get("contract_id"), str)
    }


def _legacy_authority_records(pack_root: Path) -> tuple[
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
    dict[str, Mapping[str, Any]],
]:
    """Index legacy entrypoints, provided Contracts, and artifact roles."""

    v3 = _load_json(pack_root / "rumi.pack.v3.json")
    entrypoints = {
        str(entrypoint["id"]): entrypoint
        for entrypoint in v3.get("entrypoints", [])
        if isinstance(entrypoint, Mapping) and isinstance(entrypoint.get("id"), str)
    }
    contracts = v3.get("contracts")
    provides = (
        {
            str(contract["id"]): contract
            for contract in contracts.get("provides", [])
            if isinstance(contract, Mapping) and isinstance(contract.get("id"), str)
        }
        if isinstance(contracts, Mapping)
        else {}
    )
    artifact_manifest_path = pack_root / "artifact-manifest.json"
    artifacts: dict[str, Mapping[str, Any]] = {}
    if artifact_manifest_path.is_file():
        artifact_manifest = _load_json(artifact_manifest_path)
        artifacts = {
            str(artifact["path"]): artifact
            for artifact in artifact_manifest.get("artifacts", [])
            if isinstance(artifact, Mapping) and isinstance(artifact.get("path"), str)
        }
    return entrypoints, provides, artifacts


def _semantic_pack_comparison(
    pack_root: Path,
    registry_records: list[Mapping[str, Any]],
) -> tuple[str, dict[str, Any]]:
    """Compare legacy-shaped operation semantics with the v4 target."""

    pack_id = pack_root.name
    variants, v4_operations = _v4_executable_records(pack_root)
    inventory = {
        "legacy_count": sum(
            len(record.get("operations", [])) for record in registry_records
        ),
        "v4_count": len(v4_operations),
    }
    if not v4_operations:
        categories = ["non_executable_pack_semantics_unmodeled"]
        if not (pack_root / "rumi.pack.v3.json").is_file() and not registry_records:
            categories.append("pack_specific_legacy_source_missing")
        return "generated-draft", {
            "status": "unverified",
            "equivalent": None,
            "method": None,
            "operation_inventory": inventory,
            "operation_mappings": [],
            "missing_categories": categories,
            "reason": (
                "The committed legacy-shaped sources contain no executable operation "
                "model for this Pack's non-executable behavior"
            ),
        }
    if not registry_records:
        return "generated-draft", {
            "status": "unverified",
            "equivalent": None,
            "method": None,
            "operation_inventory": inventory,
            "operation_mappings": [],
            "missing_categories": ["pack_specific_executable_source_missing"],
            "reason": "No hardened legacy executable source records cover the v4 operations",
        }
    if not (pack_root / "rumi.pack.v3.json").is_file():
        return "generated-draft", {
            "status": "unverified",
            "equivalent": None,
            "method": None,
            "operation_inventory": inventory,
            "operation_mappings": [],
            "missing_categories": ["pack_specific_authority_source_missing"],
            "reason": (
                "Explicit legacy operation/schema records exist, but no independent "
                "legacy Pack authority/role manifest is committed"
            ),
        }

    entrypoints, legacy_contracts, legacy_artifacts = _legacy_authority_records(pack_root)
    v4_contracts = _v4_contract_records(pack_root)
    mappings: list[dict[str, Any]] = []
    observed_keys: set[tuple[str, str]] = set()
    errors: list[str] = []
    for record in registry_records:
        function_id = record.get("function_id")
        variant = variants.get(str(function_id))
        legacy_operation_sources = [
            operation
            for operation in record.get("operations", [])
            if isinstance(operation, Mapping)
            and operation.get("kind") == "legacy-v3-entrypoint"
        ]
        if variant is None or len(legacy_operation_sources) != 1:
            errors.append(f"function_mapping:{function_id}")
            continue
        legacy_entrypoint = entrypoints.get(
            str(legacy_operation_sources[0].get("entrypoint_id"))
        )
        legacy_contract = (
            legacy_contracts.get(str(legacy_entrypoint.get("contract_id")))
            if isinstance(legacy_entrypoint, Mapping)
            else None
        )
        v4_contract = v4_contracts.get(str(record.get("contract_id")))
        provider = (
            v4_contract.get("provider_semantics")
            if isinstance(v4_contract, Mapping)
            else None
        )
        if not isinstance(legacy_contract, Mapping) or not isinstance(provider, Mapping):
            errors.append(f"authority_mapping:{function_id}")
            continue
        legacy_authority = {
            key: legacy_contract.get(key)
            for key in (
                "cardinality",
                "failure",
                "isolation",
                "lifecycle",
                "required_capabilities",
                "security",
            )
        }
        expected_provider = {
            **legacy_authority,
            "provider_id": f"{pack_id}.{legacy_contract.get('provider_instance_id')}",
        }
        if any(provider.get(key) != value for key, value in expected_provider.items()):
            errors.append(f"authority_semantics:{function_id}")
            continue
        artifact = legacy_artifacts.get(str(record.get("implementation_path")))
        if not isinstance(artifact, Mapping):
            errors.append(f"legacy_artifact_record_missing:{function_id}")
            continue
        if artifact.get("role") != "runtime":
            errors.append(f"legacy_artifact_role_missing:{function_id}")
            continue
        artifact_digest = str(artifact.get("sha256"))
        if not artifact_digest.startswith("sha256:"):
            artifact_digest = f"sha256:{artifact_digest}"
        if artifact_digest != record.get("implementation_digest"):
            errors.append(f"artifact_digest_mapping_mismatch:{function_id}")
            continue
        if (
            variant.get("implementation_path") != record.get("implementation_path")
            or variant.get("implementation_digest") != record.get("implementation_digest")
        ):
            errors.append(f"implementation_mapping_mismatch:{function_id}")
            continue
        for legacy_operation in record.get("operations", []):
            if not isinstance(legacy_operation, Mapping):
                continue
            operation_id = legacy_operation.get("operation_id")
            key = (str(function_id), str(operation_id))
            target_operation = v4_operations.get(key)
            if target_operation is None:
                errors.append(f"operation_mapping_missing:{function_id}:{operation_id}")
                continue
            schema_fields = ("input_schema", "output_schema")
            if any(
                canonical_digest(legacy_operation.get(field))
                != canonical_digest(target_operation.get(field))
                for field in schema_fields
            ) or any(
                legacy_operation.get(field) != target_operation.get(field)
                for field in ("contract_id", "contract_version")
            ):
                errors.append(f"parameter_schema_mapping_mismatch:{function_id}:{operation_id}")
                continue
            observed_keys.add(key)
            mappings.append(
                {
                    "legacy_operation_id": str(legacy_operation.get("entrypoint_id")),
                    "legacy_operation_kind": legacy_operation.get("kind"),
                    "v4_contract_id": target_operation.get("contract_id"),
                    "v4_operation_id": operation_id,
                    "function_id": function_id,
                    "parameter_mapping": {
                        "status": "verified",
                        "method": "canonical-json-schema-equality",
                        "legacy_schema_digest": canonical_digest(
                            legacy_operation.get("input_schema")
                        ),
                        "v4_schema_digest": canonical_digest(
                            target_operation.get("input_schema")
                        ),
                        "output_schema_digest": canonical_digest(
                            legacy_operation.get("output_schema")
                        ),
                        "rules": ["input:identity", "output:identity"],
                    },
                    "authority_mapping": {
                        "status": "verified",
                        "legacy": {
                            "contract_id": legacy_entrypoint.get("contract_id"),
                            "provider_instance_id": legacy_contract.get(
                                "provider_instance_id"
                            ),
                            **legacy_authority,
                            "artifact_role": artifact.get("role"),
                        },
                        "v4": {
                            "contract_id": target_operation.get("contract_id"),
                            **dict(provider),
                            "function_role": next(
                                (
                                    function.get("role")
                                    for function in _load_json(
                                        pack_root / "pack.v4.json"
                                    ).get("functions", [])
                                    if isinstance(function, Mapping)
                                    and function.get("id") == function_id
                                ),
                                None,
                            ),
                        },
                    },
                }
            )
    if errors or observed_keys != set(v4_operations):
        if not errors and observed_keys != set(v4_operations):
            errors.append("operation_coverage_incomplete")
        categories = sorted({error.split(":", 1)[0] for error in errors})
        return "generated-draft", {
            "status": "unverified",
            "equivalent": False,
            "method": "legacy-to-v4-semantic-comparator.v1",
            "operation_inventory": inventory,
            "operation_mappings": mappings,
            "missing_categories": categories,
            "errors": sorted(set(errors)),
            "reason": "Pack-specific legacy and v4 semantic records do not compare exactly",
        }
    comparison = {
        "status": "verified",
        "equivalent": True,
        "method": "legacy-to-v4-semantic-comparator.v1",
        "operation_inventory": inventory,
        "operation_mappings": mappings,
        "missing_categories": [],
    }
    return "semantically-reviewed", comparison


def _pack_record(
    pack_root: Path,
    explicit_packs: Mapping[str, Any],
    registry_records: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """Build the strongest reproducible Pack migration record available."""

    artifact_evidence = _verify_pack_artifacts(pack_root)
    source = _pack_source_record(pack_root, explicit_packs, registry_records)
    status, semantic_comparison = _semantic_pack_comparison(
        pack_root,
        registry_records,
    )
    record = {
        "status": status,
        "source": source,
        "target": {
            "status": "artifact-integrity-verified",
            "pack_id": pack_root.name,
            "format": "tobkiri-pack-v4-quartet",
            "digest": artifact_evidence["artifact_set_digest"],
            "artifact_verification": artifact_evidence,
        },
        "semantic_comparison": semantic_comparison,
    }
    if status == "semantically-reviewed":
        record["migration_receipt_digest"] = canonical_digest(
            {
                "pack_id": pack_root.name,
                "source_digest": source.get("digest"),
                "target_digest": artifact_evidence["artifact_set_digest"],
                "semantic_comparison": semantic_comparison,
            }
        )
    return record


def build_proof(*, observed_head_sha: str | None = None) -> dict[str, Any]:
    """Run Profile migration and Pack integrity checks and return proof JSON."""

    source = _load_json(PROFILE_FIXTURE)
    if source.get("schema") != "io.tobkiri.legacy-profile-bundle.v1":
        raise IndependentMigrationProofError("legacy Profile fixture schema is invalid")
    identity = _identity_proof(source)
    fixture = _load_json(EXECUTABLE_SOURCE_FIXTURE)
    registry = _load_json(EXECUTABLE_SOURCE_REGISTRY)
    registry_by_pack: dict[str, list[Mapping[str, Any]]] = {}
    for record in registry.get("packs", {}).values():
        if isinstance(record, Mapping) and isinstance(record.get("pack_id"), str):
            registry_by_pack.setdefault(record["pack_id"], []).append(record)
    for records in registry_by_pack.values():
        records.sort(key=lambda item: str(item.get("function_id")))
    source_inputs = _source_inputs()
    with tempfile.TemporaryDirectory(prefix="tobkiri-independent-migration-") as temporary:
        transaction = _run_profile_transaction_proof(
            source,
            identity,
            Path(temporary),
        )
    pack_dirs = _production_pack_roots()
    if not pack_dirs:
        raise IndependentMigrationProofError("no production Pack directories were found")
    explicit_packs = fixture.get("packs") if isinstance(fixture.get("packs"), Mapping) else {}
    pack_records: dict[str, dict[str, Any]] = {}
    for pack_root in pack_dirs:
        pack_records[pack_root.name] = _pack_record(
            pack_root,
            explicit_packs,
            registry_by_pack.get(pack_root.name, []),
        )
    status_counts: dict[str, int] = {}
    missing_by_category: dict[str, list[str]] = {}
    for pack_id, record in pack_records.items():
        status = str(record["status"])
        status_counts[status] = status_counts.get(status, 0) + 1
        semantic = record["semantic_comparison"]
        for category in semantic.get("missing_categories", []):
            missing_by_category.setdefault(str(category), []).append(pack_id)
    feasibility_audit = {
        "semantically_reviewed": {
            "count": status_counts.get("semantically-reviewed", 0),
            "pack_ids": sorted(
                pack_id
                for pack_id, record in pack_records.items()
                if record["status"] == "semantically-reviewed"
            ),
        },
        "unresolved": {
            category: {
                "count": len(pack_ids),
                "pack_ids": sorted(pack_ids),
            }
            for category, pack_ids in sorted(missing_by_category.items())
        },
    }
    source_payload: dict[str, Any] = {
        "kind": "repository-generated-evidence",
        "generator_id": RUNNER_ID,
        "generator_version": RUNNER_VERSION,
        "authority": "evidence-only",
        "attestation": "none",
        "freshness_basis": "exact-input-digests-and-deterministic-recomputation",
        "observed_head_sha": observed_head_sha or _head_sha(),
        "input_digest": canonical_digest(source_inputs),
        "inputs": source_inputs,
        "input_paths": [item["path"] for item in source_inputs],
        "profile_collection_proof": {
            "identity_proof": identity,
            "transaction": transaction,
        },
        "pack_count": len(pack_records),
        "migration_status_counts": {
            **dict(sorted(status_counts.items())),
            "release-verified": 0,
        },
        "feasibility_audit": feasibility_audit,
        "unproved_pack_count": len(pack_records),
        "semantic_unproved_pack_count": (
            len(pack_records) - status_counts.get("semantically-reviewed", 0)
        ),
        "content_digest": "",
    }
    document = {
        "schema": "io.tobkiri.quality.pack-migration-proof.v2",
        "source": source_payload,
        "packs": pack_records,
    }
    document["source"]["content_digest"] = canonical_digest(
        _semantic_document(document)
    )
    return document


def write_proof(
    output: Path = DEFAULT_OUTPUT,
    *,
    observed_head_sha: str | None = None,
    check: bool = False,
) -> dict[str, Any]:
    """Write or check the independent migration proof document."""

    proof = build_proof(observed_head_sha=observed_head_sha)
    text = json.dumps(proof, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    output = Path(output)
    if check:
        try:
            tracked = _load_json(output)
        except IndependentMigrationProofError:
            tracked = {}
        if _semantic_document(tracked) != _semantic_document(proof):
            raise IndependentMigrationProofError(f"migration proof drift: {output}")
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(text, encoding="utf-8")
    return proof


def main() -> int:
    """Run the independent migration proof command."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--head-sha", help="override observed HEAD for deterministic checks")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = args.output if args.output.is_absolute() else ROOT / args.output
    try:
        proof = write_proof(
            output,
            observed_head_sha=args.head_sha,
            check=args.check,
        )
    except (IndependentMigrationProofError, OSError, subprocess.CalledProcessError) as exc:
        print(f"RED: {exc}", file=sys.stderr)
        return 1
    unproved = proof["source"]["unproved_pack_count"]
    print(
        "GREEN: deterministic evidence is fresh; "
        f"release proof remains missing for {unproved} Packs"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
