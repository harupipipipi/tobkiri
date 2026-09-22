"""Generate the Launcher presentation projection from Protocol v4 sources.

The Launcher catalog is an offline projection.  Its only normative inputs are
the finite Defaultspack v4 bundle, the v4 Pack catalog documents named by that
bundle, and the checked-in catalog's already materialized artifact metadata.
No legacy registry, Pack Architecture implementation, installed discovery, or
development command is consulted here.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Mapping

try:
    from tobkiri_protocol.defaultspack_bundle_order import (  # type: ignore[import-not-found]
        canonical_defaultspack_bundle_entries,
    )
except ModuleNotFoundError:
    _RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "tobkiri_runtime"
    if str(_RUNTIME_ROOT) not in sys.path:
        sys.path.insert(0, str(_RUNTIME_ROOT))
    from tobkiri_protocol.defaultspack_bundle_order import (
        canonical_defaultspack_bundle_entries,
    )


CATALOG_SCHEMA = "io.tobkiri.launcher.presentation-catalog.v1"
CATALOG_GENERATOR = "tobkiri-defaultspack-v4-presentation-catalog"
CATALOG_GENERATOR_VERSION = "2.0.0"
RELEASE_SCHEMA = "io.tobkiri.shell.release.v4"
ARTIFACT_INDEX_PATH = "bundled/shell_artifact_index.v4.json"
PROFILE_LOCK_PATH = "bundled/shell_profile_lock.v4.json"
DEFAULT_CATALOG_RELATIVE = (
    "tobkiri_launcher/src-tauri/bundled/presentation_catalog.json"
)
V4_ROOT_RELATIVE = "tobkiri_runtime/ecosystem/defaultspack/v4"
BUNDLE_LOCK_NAME = "bundle.lock.json"
SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class PresentationCatalogError(RuntimeError):
    """Raised when the canonical v4 presentation inputs are unsafe or invalid."""


@dataclass(frozen=True)
class V4Document:
    """One digest-verified v4 document and its bundle-relative source path."""

    kind: str
    identity: str
    path: Path
    relative_path: str
    digest: str
    value: Mapping[str, Any]


@dataclass(frozen=True)
class V4Bundle:
    """The finite, schema-validated v4 source set used by the generator."""

    root: Path
    packs: Mapping[str, V4Document]
    bases: Mapping[str, V4Document]
    shells: Mapping[str, V4Document]
    profiles: Mapping[str, V4Document]
    executable_catalogs: Mapping[str, V4Document]
    selected_pack_ids: tuple[str, ...] = ()


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _canonical_digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return _sha256_bytes(encoded)


def _relative(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _read_regular_json(path: Path, label: str) -> dict[str, Any]:
    """Read one regular JSON object without following symlinked sources."""
    if path.is_symlink() or not path.is_file():
        raise PresentationCatalogError(f"{label} must be a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PresentationCatalogError(f"{label} is not valid JSON: {path}") from exc
    if not isinstance(value, dict):
        raise PresentationCatalogError(f"{label} must be a JSON object: {path}")
    return value


def _validate_relative_path(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise PresentationCatalogError(f"{label} must be a non-empty relative path")
    path = Path(value)
    windows_path = PureWindowsPath(value)
    if (
        path.is_absolute()
        or windows_path.is_absolute()
        or windows_path.drive
        or ".." in path.parts
        or ".." in windows_path.parts
        or value.startswith("~")
    ):
        raise PresentationCatalogError(f"{label} is unsafe: {value}")
    return value


def _validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise PresentationCatalogError(f"{label} must be an exact sha256 digest")
    return value


def _identity(kind: str, document: Mapping[str, Any]) -> str:
    if kind == "pack":
        source = document.get("pack")
        field = "id"
    elif kind == "base":
        source = document
        field = "pack_id"
    elif kind == "shell":
        source = document
        field = "provider_id"
    elif kind == "profile":
        source = document
        field = "profile_id"
    elif kind == "executable_catalog":
        source = document
        field = "pack_id"
    else:
        raise PresentationCatalogError(f"unsupported v4 bundle kind: {kind}")
    if not isinstance(source, Mapping) or not isinstance(source.get(field), str):
        raise PresentationCatalogError(f"{kind} document has no exact identity")
    identity = str(source[field]).strip()
    if not identity:
        raise PresentationCatalogError(f"{kind} document has an empty identity")
    return identity


def _validate_v4_documents(bundle: V4Bundle) -> tuple[str, ...]:
    """Use the live v4 schema/resolver validation for every bundled document."""
    runtime_root = bundle.root.parents[2]
    if str(runtime_root) not in sys.path:
        sys.path.insert(0, str(runtime_root))
    pack_root = bundle.root.parents[0]
    if str(pack_root) not in sys.path:
        sys.path.insert(0, str(pack_root))
    try:
        from domain.runtime_v4 import BundledCatalog, resolve_default_profile

        # BundledCatalog.load is the canonical schema, source-identity, catalog
        # digest, Pack-sidecar role, and bundle-lock digest validator.  The
        # presentation projection intentionally consumes none of these
        # executable sidecars, but it must still verify them as part of the
        # finite v4 source set before excluding them from the UI catalog.
        catalog = BundledCatalog.load(bundle.root)
        profile = catalog.profiles.get("defaults")
        if profile is None:
            raise PresentationCatalogError("v4 bundle has no defaults profile")
        selected_ids = {
            str(profile["base"]["pack_id"]),
            str(profile["shell"]["pack_id"]),
            *(str(item["pack_id"]) for item in profile["packs"]),
        }
        pending = list(selected_ids)
        while pending:
            manifest = catalog.packs.get(pending.pop())
            if manifest is None:
                raise PresentationCatalogError(
                    "source Profile selects a Pack outside the v4 bundle"
                )
            for dependency_id in manifest["requirements"]["pack_dependencies"]:
                if dependency_id not in selected_ids:
                    selected_ids.add(str(dependency_id))
                    pending.append(str(dependency_id))
        shell = catalog.shells.get(str(profile["shell"]["provider_id"]))
        if shell is not None and shell.get("availability") == "build_required":
            return tuple(sorted(selected_ids))
        authority_bindings = {}
        for index, edge in enumerate(profile.get("requested_edges", ())):
            key = "|".join(
                str(edge.get(field) or "")
                for field in (
                    "caller_function_id",
                    "target_provider_id",
                    "contract_id",
                    "operation_id",
                )
            )
            authority_bindings[key] = f"authority-ref:presentation-catalog.{index}"
        resolved = resolve_default_profile(
            catalog,
            "defaults",
            approved_artifact_digests={
                str(manifest["pack"]["artifact_digest"])
                for manifest in catalog.packs.values()
            },
            authority_snapshot_digest=_sha256_file(bundle.root / BUNDLE_LOCK_NAME),
            authority_bindings=authority_bindings,
            security_epoch=1,
        )
        selected_ids = {
            str(resolved.profile["base"]["pack_id"]),
            str(resolved.profile["shell"]["pack_id"]),
            *(
                str(item["pack_id"])
                for item in resolved.profile["packs"]
            ),
        }
        if not selected_ids.issubset(catalog.packs):
            raise PresentationCatalogError(
                "resolved Profile selected a Pack outside the v4 bundle"
            )
        return tuple(sorted(selected_ids))
    except Exception as exc:
        raise PresentationCatalogError(f"v4 bundle validation failed: {exc}") from exc


def load_v4_bundle(repository_root: Path) -> V4Bundle:
    """Load the exact v4 source set and reject drift, escapes, and symlinks."""
    root = repository_root.resolve()
    bundle_root = root / V4_ROOT_RELATIVE
    if not bundle_root.is_dir() or bundle_root.is_symlink():
        raise PresentationCatalogError(f"v4 source root is missing: {bundle_root}")

    all_files = {
        _relative(bundle_root, path)
        for path in bundle_root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    symlinks = [path for path in bundle_root.rglob("*") if path.is_symlink()]
    if symlinks:
        raise PresentationCatalogError(
            "v4 source set contains a symlink: " + _relative(bundle_root, symlinks[0])
        )

    lock_path = bundle_root / BUNDLE_LOCK_NAME
    lock = _read_regular_json(lock_path, "v4 bundle lock")
    if set(lock) != {"schema", "entries"}:
        raise PresentationCatalogError("v4 bundle lock has unknown or missing fields")
    if lock.get("schema") != "io.tobkiri.defaultspack-bundle-lock.v1":
        raise PresentationCatalogError("unsupported v4 bundle lock schema")
    entries = lock.get("entries")
    if not isinstance(entries, list) or not entries:
        raise PresentationCatalogError("v4 bundle lock entries must be non-empty")
    try:
        canonical_entries = canonical_defaultspack_bundle_entries(entries)
    except (TypeError, ValueError) as exc:
        raise PresentationCatalogError(f"v4 bundle lock entry contract failed: {exc}") from exc
    if entries != canonical_entries:
        raise PresentationCatalogError("v4 bundle lock order is not canonical")

    expected_paths = {BUNDLE_LOCK_NAME}
    documents: dict[str, dict[str, V4Document]] = {
        "pack": {},
        "base": {},
        "shell": {},
        "profile": {},
        "executable_catalog": {},
    }
    seen_paths: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping) or set(entry) != {"path", "kind", "digest"}:
            raise PresentationCatalogError(f"v4 bundle entry {index} is malformed")
        relative = _validate_relative_path(entry.get("path"), f"bundle entry {index} path")
        kind = entry.get("kind")
        if kind not in documents:
            raise PresentationCatalogError(f"unsupported v4 bundle kind: {kind}")
        if relative in seen_paths or relative == BUNDLE_LOCK_NAME:
            raise PresentationCatalogError(f"duplicate v4 bundle path: {relative}")
        expected_digest = _validate_digest(entry.get("digest"), f"bundle entry {relative} digest")
        path = bundle_root / relative
        document = _read_regular_json(path, f"v4 {kind} document")
        actual_digest = _sha256_file(path)
        if actual_digest != expected_digest:
            raise PresentationCatalogError(
                f"v4 bundle digest changed: {relative} "
                f"({actual_digest} != {expected_digest})"
            )
        identity = _identity(kind, document)
        if identity in documents[kind]:
            raise PresentationCatalogError(f"duplicate v4 {kind} identity: {identity}")
        documents[kind][identity] = V4Document(
            kind=kind,
            identity=identity,
            path=path,
            relative_path=relative,
            digest=expected_digest,
            value=document,
        )
        seen_paths.add(relative)
        expected_paths.add(relative)

    if all_files != expected_paths:
        missing = sorted(expected_paths - all_files)
        extra = sorted(all_files - expected_paths)
        raise PresentationCatalogError(
            f"v4 bundle file set mismatch; missing={missing}, extra={extra}"
        )

    bundle = V4Bundle(
        root=bundle_root,
        packs=documents["pack"],
        bases=documents["base"],
        shells=documents["shell"],
        profiles=documents["profile"],
        executable_catalogs=documents["executable_catalog"],
    )
    selected_pack_ids = _validate_v4_documents(bundle)
    return V4Bundle(
        root=bundle.root,
        packs=bundle.packs,
        bases=bundle.bases,
        shells=bundle.shells,
        profiles=bundle.profiles,
        executable_catalogs=bundle.executable_catalogs,
        selected_pack_ids=selected_pack_ids,
    )


def _relative_repository_path(repository_root: Path, path: Path) -> str:
    return path.relative_to(repository_root).as_posix()


def _approval(
    *,
    authority_mode: str,
    execution_domain: str,
    effect_scope: list[str],
    blast_radius: str,
) -> dict[str, Any]:
    return {
        "authority_mode": authority_mode,
        "blast_radius": blast_radius,
        "effect_scope": effect_scope,
        "execution_domain": execution_domain,
        "grant_state": "not_minted",
        "provider_trust": "verified",
        "state": "verified",
    }


def _backend_provider_ids(
    profile: Mapping[str, Any], packs: Mapping[str, V4Document]
) -> list[str]:
    result: list[str] = []
    for reference in profile.get("packs", []):
        if not isinstance(reference, Mapping) or reference.get("role") != "provider":
            continue
        pack_id = reference.get("pack_id")
        manifest = packs.get(str(pack_id))
        if manifest is None:
            raise PresentationCatalogError(f"profile selects an unavailable Pack: {pack_id}")
        boundary = manifest.value.get("requirements", {}).get("execution_boundary")
        if boundary in {"sandbox", "pack_vm", "declarative_only"}:
            result.append(str(pack_id))
    if not result:
        raise PresentationCatalogError("profile does not select a backend Provider Pack")
    return sorted(set(result))


def _base_descriptor(
    repository_root: Path,
    base: V4Document,
    base_pack: V4Document,
    profile: Mapping[str, Any],
    packs: Mapping[str, V4Document],
) -> dict[str, Any]:
    value = base.value
    pack_value = base_pack.value.get("pack")
    requirements = value.get("shell_requirements")
    if not isinstance(pack_value, Mapping) or not isinstance(requirements, Mapping):
        raise PresentationCatalogError("v4 Base document is incomplete")
    pack_id = str(value["pack_id"])
    base_artifact_digest = _validate_digest(
        value.get("artifact_digest"), f"Base {pack_id} artifact_digest"
    )
    manifest_artifact_digest = _validate_digest(
        pack_value.get("artifact_digest"), f"Base {pack_id} manifest artifact_digest"
    )
    if base_artifact_digest != manifest_artifact_digest:
        raise PresentationCatalogError(
            f"Base {pack_id} does not pin its exact Pack artifact"
        )
    provider_ids = _backend_provider_ids(profile, packs)
    state_owners = value.get("state_owners")
    families = requirements.get("presentation_families")
    capabilities = requirements.get("required_capabilities")
    if not isinstance(state_owners, list) or not all(isinstance(item, str) for item in state_owners):
        raise PresentationCatalogError(f"Base {pack_id} has invalid state owners")
    if not isinstance(families, list) or not all(isinstance(item, str) for item in families):
        raise PresentationCatalogError(f"Base {pack_id} has invalid presentation families")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise PresentationCatalogError(f"Base {pack_id} has invalid shell capabilities")
    identity = {
        "provider_ids": provider_ids,
        "state_owners": list(state_owners),
    }
    return {
        "pack_id": pack_id,
        "display_name": str(pack_value.get("display_name") or pack_id),
        "version": str(pack_value.get("version") or ""),
        "artifact_digest": base_artifact_digest,
        "backend_provider_ids": provider_ids,
        "state_owners": list(state_owners),
        "backend_identity_digest": _canonical_digest(identity),
        "required_capabilities": list(capabilities),
        "allowed_families": list(families),
        "approval": _approval(
            authority_mode="none",
            execution_domain=f"base-pack:{pack_id}",
            effect_scope=[],
            blast_radius="Base Pack selection grants no Host authority and does not mint a caller Grant.",
        ),
    }


def _contract_revision_descriptor(
    contract_id: str,
    revision_digest: str,
    source: V4Document,
    repository_root: Path,
) -> dict[str, str]:
    match = re.search(r"\.v(\d+)$", contract_id)
    revision = f"{match.group(1)}.0.0" if match else "1.0.0"
    return {
        "contract_id": contract_id,
        "revision": revision,
        "digest": _validate_digest(revision_digest, f"Contract {contract_id} revision_digest"),
        "source_path": _relative_repository_path(repository_root, source.path),
    }


def _contract_revisions(
    repository_root: Path,
    packs: Mapping[str, V4Document],
    selected_pack_ids: set[str],
) -> list[dict[str, str]]:
    revisions: dict[str, tuple[str, V4Document]] = {}
    for pack_id in sorted(selected_pack_ids):
        manifest = packs.get(pack_id)
        if manifest is None:
            raise PresentationCatalogError(f"selected Pack is missing from v4 bundle: {pack_id}")
        contracts = manifest.value.get("contracts")
        if not isinstance(contracts, list):
            raise PresentationCatalogError(f"Pack {pack_id} contracts are not an array")
        for contract in contracts:
            if not isinstance(contract, Mapping):
                raise PresentationCatalogError(f"Pack {pack_id} has a malformed Contract")
            contract_id = str(contract.get("contract_id") or "")
            digest = str(contract.get("revision_digest") or "")
            if not contract_id or not digest:
                raise PresentationCatalogError(f"Pack {pack_id} has an incomplete Contract")
            previous = revisions.get(contract_id)
            if previous is not None and previous[0] != digest:
                raise PresentationCatalogError(f"Contract revision drift: {contract_id}")
            revisions[contract_id] = (digest, manifest)
    return [
        _contract_revision_descriptor(contract_id, digest, source, repository_root)
        for contract_id, (digest, source) in sorted(revisions.items())
    ]


def _existing_catalog_metadata(target: Path) -> dict[str, Any]:
    if not target.is_file() or target.is_symlink():
        return {}
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise PresentationCatalogError(f"existing catalog is not valid JSON: {target}") from exc
    if not isinstance(value, dict):
        raise PresentationCatalogError(f"existing catalog must be an object: {target}")
    return value


def _preserved_variant_metadata(
    existing: Mapping[str, Any], artifact_id: str, platform: str, architecture: str
) -> dict[str, Any]:
    variants: list[Mapping[str, Any]] = []
    for shell in existing.get("shell_providers", []):
        if not isinstance(shell, Mapping):
            continue
        for variant in shell.get("artifact_variants", []):
            if isinstance(variant, Mapping) and variant.get("artifact_id") == artifact_id:
                variants.append(variant)
    if not variants:
        return {"path": None, "sha256": None, "size": None, "source_identity": None, "source_revision": None}
    if len(variants) != 1:
        raise PresentationCatalogError(f"existing catalog duplicates artifact metadata: {artifact_id}")
    old = variants[0]
    if old.get("platform") != platform or old.get("architecture") != architecture:
        raise PresentationCatalogError(f"existing catalog artifact identity changed: {artifact_id}")
    fields = ("path", "sha256", "size", "source_identity", "source_revision")
    present = [old.get(field) is not None for field in fields]
    if not any(present):
        return {field: None for field in fields}
    if not all(present):
        raise PresentationCatalogError(f"existing catalog has incomplete installed metadata: {artifact_id}")
    path = _validate_relative_path(old.get("path"), f"installed artifact {artifact_id} path")
    digest = _validate_digest(old.get("sha256"), f"installed artifact {artifact_id} digest")
    size = old.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise PresentationCatalogError(f"installed artifact {artifact_id} size is invalid")
    source_identity = old.get("source_identity")
    source_revision = old.get("source_revision")
    if not isinstance(source_identity, str) or not source_identity.strip():
        raise PresentationCatalogError(f"installed artifact {artifact_id} source identity is invalid")
    if not isinstance(source_revision, str) or not source_revision.strip():
        raise PresentationCatalogError(f"installed artifact {artifact_id} source revision is invalid")
    return {
        "path": path,
        "sha256": digest,
        "size": size,
        "source_identity": source_identity,
        "source_revision": source_revision,
    }


def _shell_descriptor(
    repository_root: Path,
    shell: V4Document,
    shell_pack: V4Document,
    contract_revisions: Mapping[str, Mapping[str, Any]],
    profile: Mapping[str, Any],
    existing: Mapping[str, Any],
) -> dict[str, Any]:
    value = shell.value
    pack_value = shell_pack.value.get("pack")
    presentation = value.get("presentation")
    launch = value.get("launch")
    if not isinstance(pack_value, Mapping) or not isinstance(presentation, Mapping) or not isinstance(launch, Mapping):
        raise PresentationCatalogError(f"Shell {shell.identity} is incomplete")
    if shell.value.get("pack_id") != shell_pack.identity:
        raise PresentationCatalogError(f"Shell {shell.identity} Pack identity is inconsistent")
    profile_shell = profile.get("shell")
    if not isinstance(profile_shell, Mapping):
        raise PresentationCatalogError("defaults profile has no exact Shell selection")
    if (
        profile_shell.get("provider_id") != shell.identity
        or profile_shell.get("pack_id") != shell_pack.identity
        or profile_shell.get("contract_id") != value.get("contract_id")
    ):
        raise PresentationCatalogError(
            f"defaults profile Shell selection does not pin {shell.identity}"
        )
    _validate_digest(
        pack_value.get("artifact_digest"),
        f"Shell {shell.identity} manifest artifact_digest",
    )
    contract_id = str(value.get("contract_id") or "")
    revision = contract_revisions.get(contract_id)
    if revision is None:
        raise PresentationCatalogError(f"Shell Contract is not registered: {contract_id}")
    consumed = presentation.get("consumes_contribution_contracts")
    capabilities = presentation.get("capabilities")
    variants = launch.get("variants")
    build_targets = launch.get("build_targets")
    if not isinstance(consumed, list) or not all(isinstance(item, str) for item in consumed):
        raise PresentationCatalogError(f"Shell {shell.identity} has invalid consumed Contracts")
    if not isinstance(capabilities, list) or not all(isinstance(item, str) for item in capabilities):
        raise PresentationCatalogError(f"Shell {shell.identity} has invalid capabilities")
    consumed = list(consumed)
    for edge in profile.get("requested_edges", ()):
        if not isinstance(edge, Mapping):
            continue
        if edge.get("caller_function_id") != shell.identity:
            continue
        edge_contract_id = edge.get("contract_id")
        if isinstance(edge_contract_id, str) and edge_contract_id not in consumed:
            consumed.append(edge_contract_id)
    if (
        launch.get("prebuilt_only") is not True
        or not isinstance(variants, list)
        or not isinstance(build_targets, list)
        or not build_targets
    ):
        raise PresentationCatalogError(f"Shell {shell.identity} must be prebuilt-only")
    if value.get("availability") == "build_required":
        if value.get("artifact_digest") is not None or variants:
            raise PresentationCatalogError(
                f"unavailable Shell {shell.identity} fabricates a launch artifact"
            )
        declared_variants: list[dict[str, Any]] = []
        seen_targets: set[tuple[str, str]] = set()
        for target in build_targets:
            if not isinstance(target, Mapping):
                raise PresentationCatalogError(
                    f"Shell {shell.identity} has a malformed build target"
                )
            platform = str(target.get("platform") or "")
            architecture = str(target.get("architecture") or "")
            artifact_id = str(target.get("artifact_id") or "")
            target_key = (platform, architecture)
            if target_key in seen_targets or artifact_id != (
                f"{shell.identity}.{platform}-{architecture}"
            ):
                raise PresentationCatalogError(
                    f"Shell {shell.identity} has an ambiguous build target"
                )
            seen_targets.add(target_key)
            metadata = _preserved_variant_metadata(
                existing, artifact_id, platform, architecture
            )
            if any(value is not None for value in metadata.values()):
                metadata = {key: None for key in metadata}
            declared_variants.append(
                {
                    "artifact_id": artifact_id,
                    "variant": f"{platform}-{architecture}",
                    "platform": platform,
                    "architecture": architecture,
                    "artifact_ref": str(target["artifact_ref"]),
                    "entrypoint": str(target["entrypoint"]),
                    "artifact_kind": "signed_prebuilt_binary",
                    "descriptor_digest": shell.digest,
                    **metadata,
                    "prebuilt": True,
                    "production": True,
                    "development_command": None,
                    "bundle_identifier": str(target["bundle_identity"]),
                }
            )
        requested_target = (
            profile_shell.get("platform"),
            profile_shell.get("architecture"),
        )
        if requested_target not in seen_targets:
            raise PresentationCatalogError(
                f"defaults profile Shell target is not declared: {requested_target}"
            )
        effect_scope = sorted({contract_id, *consumed})
        return {
            "provider_id": str(value["provider_id"]),
            "display_name": str(pack_value.get("display_name") or value["provider_id"]),
            "contract_id": contract_id,
            "contract_revision_digest": revision["digest"],
            "experience_role": "shell",
            "presentation_kind": str(presentation.get("kind") or ""),
            "presentation_family": str(presentation.get("family") or ""),
            "technology": str(presentation.get("technology") or ""),
            "capabilities": list(capabilities),
            "consumes_contracts": list(consumed),
            "contributions": [],
            "artifact_variants": declared_variants,
            "approval": _approval(
                authority_mode="lease_only",
                execution_domain=f"shell:{value['provider_id']}",
                effect_scope=effect_scope,
                blast_radius=(
                    "Shell requests use the Host Broker; the Shell has no ambient "
                    "Host authority."
                ),
            ),
        }
    shell_artifact_digest = _validate_digest(
        value.get("artifact_digest"), f"Shell {shell.identity} artifact_digest"
    )
    if not variants:
        raise PresentationCatalogError(f"verified Shell {shell.identity} has no variant")
    artifact_variants: list[dict[str, Any]] = []
    seen_artifact_targets: set[tuple[str, str]] = set()
    seen_ids: set[str] = set()
    for variant in variants:
        if not isinstance(variant, Mapping):
            raise PresentationCatalogError(f"Shell {shell.identity} has a malformed launch variant")
        platform = str(variant.get("platform") or "")
        architecture = str(variant.get("architecture") or "")
        target = (platform, architecture)
        if target in seen_artifact_targets:
            raise PresentationCatalogError(f"Shell {shell.identity} duplicates platform variant: {target}")
        seen_artifact_targets.add(target)
        artifact_id = f"{shell.identity}.{platform}-{architecture}"
        if artifact_id in seen_ids:
            raise PresentationCatalogError(f"Shell {shell.identity} duplicates artifact identity")
        seen_ids.add(artifact_id)
        artifact_digest = _validate_digest(
            variant.get("artifact_digest"), f"{artifact_id} artifact_digest"
        )
        if artifact_digest != shell_artifact_digest:
            raise PresentationCatalogError(
                f"{artifact_id} does not pin the Shell Pack artifact"
            )
        relative_path = _validate_relative_path(
            variant.get("relative_path"), f"{artifact_id} relative_path"
        )
        entrypoint = variant.get("entrypoint")
        bundle_identity = variant.get("bundle_identity")
        if not isinstance(entrypoint, str) or not entrypoint.strip():
            raise PresentationCatalogError(f"{artifact_id} entrypoint is missing")
        if not isinstance(bundle_identity, str) or not bundle_identity.strip():
            raise PresentationCatalogError(f"{artifact_id} bundle identity is missing")
        metadata = _preserved_variant_metadata(existing, artifact_id, platform, architecture)
        artifact_variants.append(
            {
                "artifact_id": artifact_id,
                "variant": f"{platform}-{architecture}",
                "platform": platform,
                "architecture": architecture,
                "artifact_ref": relative_path,
                "entrypoint": entrypoint,
                "artifact_kind": "signed_prebuilt_binary",
                "descriptor_digest": shell.digest,
                **metadata,
                "prebuilt": True,
                "production": True,
                "development_command": None,
                "bundle_identifier": bundle_identity,
            }
        )
    requested_target = (
        profile_shell.get("platform"),
        profile_shell.get("architecture"),
    )
    if requested_target not in seen_artifact_targets:
        raise PresentationCatalogError(
            f"defaults profile Shell target is not declared: {requested_target}"
        )
    effect_scope = sorted({contract_id, *consumed})
    return {
        "provider_id": str(value["provider_id"]),
        "display_name": str(pack_value.get("display_name") or value["provider_id"]),
        "contract_id": contract_id,
        "contract_revision_digest": revision["digest"],
        "experience_role": "shell",
        "presentation_kind": str(presentation.get("kind") or ""),
        "presentation_family": str(presentation.get("family") or ""),
        "technology": str(presentation.get("technology") or ""),
        "capabilities": list(capabilities),
        "consumes_contracts": list(consumed),
        "contributions": [],
        "artifact_variants": artifact_variants,
        "approval": _approval(
            authority_mode="lease_only",
            execution_domain=f"shell:{value['provider_id']}",
            effect_scope=effect_scope,
            blast_radius="Shell requests use the Host Broker; the Shell has no ambient Host authority.",
        ),
    }


def _release_binding(
    existing: Mapping[str, Any], variants: list[Mapping[str, Any]]
) -> dict[str, Any] | None:
    value = existing.get("release_binding")
    if not variants:
        return None
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise PresentationCatalogError("existing release binding is not an object")
    required = {
        "schema",
        "artifact_index_path",
        "artifact_index_sha256",
        "profile_lock_path",
        "profile_lock_sha256",
        "catalog_revision",
        "artifact_id",
        "source_identity",
        "source_revision",
        "platform",
        "architecture",
    }
    if set(value) != required or value.get("schema") != RELEASE_SCHEMA:
        raise PresentationCatalogError("existing release binding has unsupported fields")
    if value.get("artifact_index_path") != ARTIFACT_INDEX_PATH:
        raise PresentationCatalogError("existing release binding has a non-canonical index path")
    if value.get("profile_lock_path") != PROFILE_LOCK_PATH:
        raise PresentationCatalogError("existing release binding has a non-canonical lock path")
    for field in (
        "artifact_index_sha256",
        "profile_lock_sha256",
        "catalog_revision",
    ):
        _validate_digest(value.get(field), f"release binding {field}")
    artifact_id = value.get("artifact_id")
    matching = [
        variant for variant in variants if variant.get("artifact_id") == artifact_id
    ]
    if len(matching) != 1:
        raise PresentationCatalogError("existing release binding points to an unavailable artifact")
    variant = matching[0]
    if any(variant.get(field) is None for field in ("path", "sha256", "size")):
        raise PresentationCatalogError("existing release binding points to an uninstalled artifact")
    for field in ("source_identity", "source_revision"):
        if value.get(field) != variant.get(field):
            raise PresentationCatalogError(
                f"existing release binding does not match installed {field}"
            )
    for field in ("platform", "architecture"):
        if value.get(field) != variant.get(field):
            raise PresentationCatalogError(
                f"existing release binding does not match artifact {field}"
            )
    if not isinstance(value.get("source_identity"), str) or not value["source_identity"].strip():
        raise PresentationCatalogError("existing release binding source identity is invalid")
    if not isinstance(value.get("source_revision"), str) or not value["source_revision"].strip():
        raise PresentationCatalogError("existing release binding source revision is invalid")
    return copy.deepcopy(dict(value))


def generate_presentation_catalog(repository_root: Path, output: Path | None = None) -> dict[str, Any]:
    """Generate the deterministic Launcher catalog from the v4 bundle."""
    root = repository_root.resolve()
    target = (output or root / DEFAULT_CATALOG_RELATIVE).resolve()
    bundle = load_v4_bundle(root)
    existing = _existing_catalog_metadata(target)
    profile = bundle.profiles.get("defaults")
    if profile is None:
        raise PresentationCatalogError("v4 bundle has no defaults profile")
    profile_value = profile.value
    base_id = str(profile_value.get("base", {}).get("pack_id") or "")
    shell_id = str(profile_value.get("shell", {}).get("provider_id") or "")
    base = bundle.bases.get(base_id)
    shell = bundle.shells.get(shell_id)
    if base is None or shell is None:
        raise PresentationCatalogError("defaults profile selects an unavailable Base or Shell")
    base_pack = bundle.packs.get(base_id)
    shell_pack_id = str(shell.value.get("pack_id") or "")
    shell_pack = bundle.packs.get(shell_pack_id)
    if base_pack is None or shell_pack is None:
        raise PresentationCatalogError("selected Base or Shell Pack is missing")
    selected_pack_ids = set(bundle.selected_pack_ids)
    revisions = _contract_revisions(root, bundle.packs, selected_pack_ids)
    revision_map = {item["contract_id"]: item for item in revisions}
    base_descriptor = _base_descriptor(root, base, base_pack, profile_value, bundle.packs)
    shell_descriptor = _shell_descriptor(
        root, shell, shell_pack, revision_map, profile_value, existing
    )
    source_manifest_digests = {
        identity: bundle.packs[identity].digest
        for identity in sorted(selected_pack_ids)
    }
    catalog: dict[str, Any] = {
        "schema": CATALOG_SCHEMA,
        "generator": CATALOG_GENERATOR,
        "generator_version": CATALOG_GENERATOR_VERSION,
        "default_profile_id": str(profile_value["profile_id"]),
        "default_profile_source": _relative_repository_path(root, profile.path),
        "default_profile_digest": profile.digest,
        "default_selection": {
            "base_pack_id": base_id,
            "shell_provider_id": shell_id,
        },
        "contract_revisions": revisions,
        "source_manifest_digests": source_manifest_digests,
        "base_packs": [base_descriptor],
        "shell_providers": [shell_descriptor],
        "generated_at": 0,
    }
    binding = (
        None
        if shell.value.get("availability") == "build_required"
        else _release_binding(existing, shell_descriptor["artifact_variants"])
    )
    if binding is not None:
        catalog["release_binding"] = binding
    return catalog


def write_presentation_catalog(repository_root: Path, output: Path | None = None) -> Path:
    """Write the generated catalog with stable, byte-reproducible formatting."""
    root = repository_root.resolve()
    target = (output or root / DEFAULT_CATALOG_RELATIVE).resolve()
    payload = generate_presentation_catalog(root, target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return target


def presentation_catalog_drift(repository_root: Path, output: Path | None = None) -> bool:
    """Return whether the checked-in catalog differs from the v4 projection."""
    root = repository_root.resolve()
    target = (output or root / DEFAULT_CATALOG_RELATIVE).resolve()
    if target.is_symlink() or not target.is_file():
        return True
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return True
    try:
        return existing != generate_presentation_catalog(root, target)
    except PresentationCatalogError:
        return True
