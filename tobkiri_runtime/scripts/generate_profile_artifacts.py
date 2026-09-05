#!/usr/bin/env python3
"""Compile an author-edited Named Profile intent into immutable artifacts."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from typing import Any, Mapping

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tobkiri_protocol.canonical import canonical_digest, strict_loads  # noqa: E402
from tobkiri_protocol.executable_catalog import (  # noqa: E402
    materialization_catalog_digest,
)
from tobkiri_protocol.profile_scope import (  # noqa: E402
    normalize_requested_scope_template,
)
from tobkiri_protocol.validation import SCHEMA_DIR, validate_document  # noqa: E402
from core_runtime.profile_content_projection import (  # noqa: E402
    resolve_intent_projection,
    selected_projection_roots,
)
from scripts.profile_compatibility_provenance import (  # noqa: E402
    compatibility_profile_provenance,
    validate_compatibility_profile,
)

GENERATOR_NAME = "tobkiri-profile-artifacts"
GENERATOR_VERSION = "1.0.0"
GENERATOR_PATH = Path(__file__).relative_to(ROOT.parent).as_posix()
LOCAL_INPUTS = (
    ROOT / "tobkiri_protocol" / "canonical.py",
    ROOT / "tobkiri_protocol" / "executable_catalog.py",
    ROOT / "tobkiri_protocol" / "profile_scope.py",
    ROOT / "tobkiri_protocol" / "provenance.py",
    ROOT / "tobkiri_protocol" / "validation.py",
    ROOT / "core_runtime" / "profile_content_projection.py",
    ROOT / "scripts" / "profile_compatibility_provenance.py",
)
COMPATIBILITY_PROVENANCE_INPUTS = LOCAL_INPUTS
DEFAULT_BUNDLE = ROOT / "ecosystem" / "defaultspack" / "v4"
DEFAULT_INTENT = DEFAULT_BUNDLE / "defaults.profile.intent.v1.json"
DEFAULT_COMPATIBILITY = DEFAULT_BUNDLE / "defaults.profile.v4.json"
DEFAULT_LOCK = DEFAULT_BUNDLE / "defaults.profile.lock.v5.json"
DEFAULT_PROVENANCE = DEFAULT_BUNDLE / "defaults.release.provenance.json"
_DIGEST_PREFIX = "sha256:"


def _sha256(raw: bytes) -> str:
    return _DIGEST_PREFIX + hashlib.sha256(raw).hexdigest()


def _pretty(document: Mapping[str, Any]) -> bytes:
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.resolve().as_posix()


def _read_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = strict_loads(path.read_bytes())
    except OSError as exc:
        raise ValueError(f"cannot read {label}: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object: {path}")
    return value


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _reject_symlink_components(path: Path, label: str) -> None:
    candidate = _absolute(path)
    current = Path(candidate.anchor)
    for part in candidate.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError(f"{label} contains a symlink component: {current}")
        if not current.exists():
            break


def _require_output_layout(bundle_root: Path, outputs: tuple[Path, ...]) -> None:
    root = _absolute(bundle_root)
    _reject_symlink_components(root, "bundle root")
    if not root.is_dir():
        raise ValueError("bundle root must be a real directory")
    for output in outputs:
        candidate = _absolute(output)
        try:
            candidate.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"Profile artifact output escapes bundle root: {output}") from exc
        _reject_symlink_components(candidate, "Profile artifact output")
        if candidate.exists() and not candidate.is_file():
            raise ValueError(f"Profile artifact output is not a regular file: {output}")


class ProfileCatalog:
    """Digest-verified bundle inputs used by the generic Profile compiler."""

    def __init__(
        self,
        bundle_root: Path,
        compatibility_path: Path,
        *,
        source_bundle_root: Path | None = None,
    ) -> None:
        _reject_symlink_components(bundle_root, "bundle root")
        self.root = bundle_root.resolve(strict=True)
        self.source_bundle_root = _absolute(source_bundle_root or bundle_root)
        _reject_symlink_components(self.source_bundle_root, "source bundle root")
        if not self.source_bundle_root.is_dir():
            raise ValueError("source bundle root must be a real directory")
        _reject_symlink_components(compatibility_path, "compatibility Profile")
        self.compatibility_path = _absolute(compatibility_path)
        self.lock_path = self.root / "bundle.lock.json"
        self.lock = _read_object(self.lock_path, "bundle lock")
        if set(self.lock) != {"schema", "entries"}:
            raise ValueError("bundle lock has unknown or missing fields")
        if self.lock["schema"] != "io.tobkiri.defaultspack-bundle-lock.v1":
            raise ValueError("bundle lock schema is unsupported")
        if not isinstance(self.lock["entries"], list) or not self.lock["entries"]:
            raise ValueError("bundle lock entries must be non-empty")

        self.packs: dict[str, dict[str, Any]] = {}
        self.bases: dict[str, dict[str, Any]] = {}
        self.shells: dict[str, dict[str, Any]] = {}
        self.executables: dict[str, dict[str, Any]] = {}
        self.input_digests: dict[str, str] = {}
        self.compatibility_entry: dict[str, Any] | None = None
        seen: set[str] = set()
        for entry in self.lock["entries"]:
            self._load_entry(entry, seen)
        if self.compatibility_entry is None:
            raise ValueError("bundle lock does not name the compatibility Profile")
        for schema_path in sorted(SCHEMA_DIR.glob("*.schema.json")):
            self.input_digests[_relative(schema_path)] = _sha256(schema_path.read_bytes())
        for local_input in LOCAL_INPUTS:
            self.input_digests[_relative(local_input)] = _sha256(local_input.read_bytes())

    def provenance_path(self, path: Path) -> str:
        """Return a stable source path for a staged or source bundle member."""

        candidate = _absolute(path)
        try:
            relative = candidate.relative_to(self.root)
        except ValueError:
            return _relative(candidate)
        return _relative(self.source_bundle_root / relative)

    def _load_entry(self, entry: object, seen: set[str]) -> None:
        if not isinstance(entry, dict) or set(entry) != {"path", "kind", "digest"}:
            raise ValueError("bundle lock entry has invalid fields")
        relative = entry["path"]
        kind = entry["kind"]
        if not isinstance(relative, str) or not relative or relative in seen:
            raise ValueError("bundle lock entry has an invalid or duplicate path")
        candidate = self.root / relative
        _reject_symlink_components(candidate, "bundle input")
        path = candidate.resolve(strict=False)
        if path == self.root or self.root not in path.parents:
            raise ValueError(f"bundle lock entry escapes bundle root: {relative}")
        if path == self.compatibility_path:
            if kind != "profile":
                raise ValueError("compatibility Profile lock entry has the wrong kind")
            self.compatibility_entry = dict(entry)
            seen.add(relative)
            return
        if not path.is_file() or path.is_symlink():
            raise ValueError(f"bundle input is unavailable: {relative}")
        raw = path.read_bytes()
        if _sha256(raw) != entry["digest"]:
            raise ValueError(f"bundle input digest changed: {relative}")
        self.input_digests[self.provenance_path(path)] = _sha256(raw)
        if kind in {"pack", "base", "shell", "executable_catalog"}:
            document = validate_document(raw, kind)
            if kind == "pack":
                collection, identity = self.packs, document["pack"]["id"]
            elif kind == "base":
                collection, identity = self.bases, document["pack_id"]
            elif kind == "shell":
                collection, identity = self.shells, document["provider_id"]
            else:
                collection, identity = self.executables, document["pack_id"]
            if identity in collection:
                raise ValueError(f"duplicate {kind} identity: {identity}")
            collection[identity] = document
        seen.add(relative)

    def bundle_lock_with(self, compatibility_raw: bytes) -> dict[str, Any]:
        """Return the bundle lock with only the compatibility digest replaced."""

        relative = self.compatibility_path.relative_to(self.root).as_posix()
        entries = []
        replaced = 0
        for source in self.lock["entries"]:
            entry = dict(source)
            if entry["path"] == relative:
                entry["digest"] = _sha256(compatibility_raw)
                replaced += 1
            entries.append(entry)
        if replaced != 1:
            raise ValueError("compatibility Profile must have exactly one bundle lock entry")
        return {"schema": self.lock["schema"], "entries": entries}


def _resolve_closure(
    catalog: ProfileCatalog, intent: Mapping[str, Any]
) -> tuple[list[str], dict[str, str]]:
    base_id = str(intent["base"]["pack_id"])
    base = catalog.bases.get(base_id)
    base_manifest = catalog.packs.get(base_id)
    if base is None or base_manifest is None:
        raise ValueError(f"Profile Base is incomplete: {base_id}")
    if base["artifact_digest"] != base_manifest["pack"]["artifact_digest"]:
        raise ValueError(f"Profile Base artifact is stale: {base_id}")

    shell = intent.get("shell")
    shell_pack_id: str | None = None
    if shell is not None:
        definition = catalog.shells.get(str(shell["provider_id"]))
        if definition is None or definition["pack_id"] != shell["pack_id"]:
            raise ValueError("Profile Shell binding is unavailable or stale")
        shell_pack_id = str(shell["pack_id"])
        if shell_pack_id not in catalog.packs:
            raise ValueError(f"Profile Shell Pack is unavailable: {shell_pack_id}")

    requested_roles = {str(item["pack_id"]): str(item["role"]) for item in intent["packs"]}
    if len(requested_roles) != len(intent["packs"]):
        raise ValueError("Profile intent contains duplicate Packs")
    application_ids = [
        pack_id for pack_id, role in requested_roles.items() if role == "application"
    ]
    if len(application_ids) != 1:
        raise ValueError("Profile intent requires exactly one Application Pack")

    selected = [base_id]
    if shell_pack_id is not None:
        selected.append(shell_pack_id)
    selected.extend(pack_id for pack_id in requested_roles if pack_id not in selected)
    selected.extend(
        str(item["pack_id"])
        for item in base["dependencies"]
        if str(item["pack_id"]) not in selected
    )
    pending = list(selected)
    while pending:
        pack_id = pending.pop(0)
        manifest = catalog.packs.get(pack_id)
        if manifest is None:
            raise ValueError(f"Profile Pack is unavailable: {pack_id}")
        for dependency_id, version_range in sorted(
            manifest["requirements"]["pack_dependencies"].items()
        ):
            dependency = catalog.packs.get(dependency_id)
            if dependency is None:
                raise ValueError(f"Profile dependency is unavailable: {dependency_id}")
            try:
                compatible = Version(dependency["pack"]["version"]) in SpecifierSet(
                    version_range.replace(" ", ",")
                )
            except (InvalidSpecifier, InvalidVersion) as exc:
                raise ValueError(f"invalid dependency constraint: {dependency_id}") from exc
            if not compatible:
                raise ValueError(f"Profile dependency is incompatible: {dependency_id}")
            if dependency_id not in selected:
                selected.append(dependency_id)
                pending.append(dependency_id)
    return selected, requested_roles


def _edge_variant(
    catalog: ProfileCatalog,
    selected: list[str],
    edge: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    candidates = []
    for pack_id in selected:
        manifest = catalog.packs[pack_id]
        functions = [
            item for item in manifest["functions"] if item["id"] == edge["target_provider_id"]
        ]
        for function in functions:
            contracts = [
                item
                for item in manifest["contracts"]
                if item["contract_id"] == edge["contract_id"]
                and edge["operation_id"] in item["operations"]
                and item["revision_digest"] == function["contract_revision_digest"]
            ]
            executable = catalog.executables.get(pack_id)
            variants = (
                []
                if executable is None
                else [
                    variant
                    for variant in executable["variants"]
                    if variant["function_id"] == function["id"]
                    and any(
                        operation["contract_id"] == edge["contract_id"]
                        and operation["operation_id"] == edge["operation_id"]
                        and operation["revision_digest"] == function["contract_revision_digest"]
                        for operation in variant["operations"]
                    )
                ]
            )
            if len(contracts) == 1 and len(variants) == 1:
                candidates.append((manifest, function, contracts[0], variants[0]))
    if len(candidates) != 1:
        raise ValueError(
            "Profile edge must resolve to one executable Provider: "
            f"{edge['target_provider_id']} / {edge['operation_id']}"
        )
    return candidates[0]


def _compile_profile(
    catalog: ProfileCatalog,
    intent: Mapping[str, Any],
    compatibility_path: Path,
) -> tuple[
    dict[str, Any],
    list[str],
    dict[str, str],
    list[dict[str, Any]],
    dict[str, str],
]:
    selected, requested_roles = _resolve_closure(catalog, intent)
    profile: dict[str, Any] = {}
    for key, value in intent.items():
        if key == "intent_api_version":
            profile["profile_api_version"] = "io.tobkiri.profile.v5"
        else:
            profile[key] = value
    pins: dict[tuple[str, str], dict[str, Any]] = {}
    resolved_edges = []
    for source_edge in intent["requested_edges"]:
        edge = dict(source_edge)
        manifest, function, contract, variant = _edge_variant(catalog, selected, edge)
        template = {
            key: value
            for key, value in edge["requested_scope_template"].items()
            if key != "semantics_digest"
        }
        edge["requested_scope_template"] = normalize_requested_scope_template(
            template,
            contract_id=edge["contract_id"],
            operation_id=edge["operation_id"],
            semantics_digest=contract["revision_digest"],
        )
        resolved_edges.append(edge)
        executable = catalog.executables[manifest["pack"]["id"]]
        domain_kind = function.get("isolation", "pack_vm")
        pin = {
            "pack_id": manifest["pack"]["id"],
            "artifact_digest": manifest["pack"]["artifact_digest"],
            "executable_catalog_digest": materialization_catalog_digest(
                manifest,
                executable,
            ),
            "variant_id": variant["variant_id"],
            "platform": variant["platform"],
            "architecture": variant["architecture"],
            "runtime_abi": variant["runtime_abi"],
            "backend": variant["backend"],
            "execution_kind": variant["execution_kind"],
            "domain_kind": domain_kind,
        }
        pins[(pin["pack_id"], pin["variant_id"])] = pin
    profile["requested_edges"] = resolved_edges
    projections = []
    projection_inputs: dict[str, str] = {}
    for source in intent.get("content_projections") or []:
        resolved, files = resolve_intent_projection(source)
        projections.append(resolved)
        root = ROOT / resolved["artifact_root"]
        for relative, digest in files.items():
            projection_inputs[_relative(root / relative)] = digest
    profile["content_projections"] = sorted(projections, key=lambda item: item["projection_id"])
    selected_projection_roots(profile["content_projections"])
    provenance = compatibility_profile_provenance(
        root=ROOT,
        profile=profile,
        source_path=catalog.provenance_path(compatibility_path),
        generator=GENERATOR_NAME,
        generator_version=GENERATOR_VERSION,
        generator_path=Path(__file__),
        input_paths=COMPATIBILITY_PROVENANCE_INPUTS,
    )
    with_provenance: dict[str, Any] = {}
    for key, value in profile.items():
        if key == "requested_edges":
            with_provenance["provenance"] = provenance
        with_provenance[key] = value
    profile = with_provenance
    profile = validate_document(profile, "profile")
    validate_compatibility_profile(profile)
    return (
        profile,
        selected,
        requested_roles,
        sorted(pins.values(), key=lambda item: (item["pack_id"], item["variant_id"])),
        projection_inputs,
    )


def _compile_lock(
    catalog: ProfileCatalog,
    intent: Mapping[str, Any],
    profile: Mapping[str, Any],
    selected: list[str],
    pins: list[dict[str, Any]],
    bundle_digest: str,
) -> dict[str, Any]:
    base_id = str(intent["base"]["pack_id"])
    base = catalog.bases[base_id]
    shell_request = intent.get("shell")
    shell = None
    shell_pack_id = None
    if shell_request is not None:
        shell_definition = catalog.shells[str(shell_request["provider_id"])]
        shell_pack_id = str(shell_request["pack_id"])
        shell_manifest = catalog.packs[shell_pack_id]
        functions = [
            item
            for item in shell_manifest["functions"]
            if item["id"] == shell_request["provider_id"]
        ]
        if len(functions) != 1:
            raise ValueError("Shell executable identity is ambiguous")
        shell = {
            "provider_id": shell_request["provider_id"],
            "pack_id": shell_pack_id,
            "artifact_digest": shell_manifest["pack"]["artifact_digest"],
            "executable_artifact_digest": functions[0]["implementation_digest"],
            "definition_revision": shell_definition["definition_revision"],
            "contract_id": shell_request["contract_id"],
            "platform": shell_request["platform"],
            "architecture": shell_request["architecture"],
        }
    application_ids = [item["pack_id"] for item in intent["packs"] if item["role"] == "application"]
    application_manifest = catalog.packs[application_ids[0]]
    application_functions = application_manifest["functions"]
    if len(application_functions) != 1:
        raise ValueError("Application executable identity is ambiguous")
    application = {
        "pack_id": application_ids[0],
        "artifact_digest": application_manifest["pack"]["artifact_digest"],
        "executable_artifact_digest": application_functions[0]["implementation_digest"],
        "definition_digest": canonical_digest(application_manifest),
    }
    effective_set = [
        {
            "role": (
                "base" if pack_id == base_id else "shell" if pack_id == shell_pack_id else "pack"
            ),
            "identity": pack_id,
            "artifact_digest": catalog.packs[pack_id]["pack"]["artifact_digest"],
        }
        for pack_id in selected
    ]
    catalog_revision = canonical_digest(
        {
            pack_id: catalog.packs[pack_id]["integrity"]["source_identity"]
            for pack_id in sorted(selected)
        }
    )
    constraints_digest = canonical_digest(
        {
            "base": base,
            "shell": None
            if shell_request is None
            else catalog.shells[str(shell_request["provider_id"])],
            "requirements": {
                pack_id: catalog.packs[pack_id]["requirements"] for pack_id in sorted(selected)
            },
        }
    )
    profile_revision = canonical_digest(profile)
    definition_digest = canonical_digest(intent)
    closure_digest = canonical_digest(
        {
            "effective_set": effective_set,
            "content_projections": profile["content_projections"],
        }
    )
    requested_edges_digest = canonical_digest(profile["requested_edges"])
    lock = {
        "lock_api_version": "io.tobkiri.profile-artifact-lock.v1",
        "profile_api_version": "io.tobkiri.profile.v5",
        "resolution_scope": "source_release",
        "activation_authority": "unbound",
        "profile_id": intent["profile_id"],
        "profile_revision": profile_revision,
        "profile_definition_digest": definition_digest,
        "catalog_revision": catalog_revision,
        "bundle_digest": bundle_digest,
        "base": {
            "pack_id": base_id,
            "artifact_digest": catalog.packs[base_id]["pack"]["artifact_digest"],
            "definition_revision": base["definition_revision"],
        },
        "shell": shell,
        "application": application,
        "effective_set": effective_set,
        "content_projections": list(profile["content_projections"]),
        "content_projection_digest": canonical_digest(profile["content_projections"]),
        "variant_pins": pins,
        "requested_edges_digest": requested_edges_digest,
        "constraints_digest": constraints_digest,
        "closure_digest": closure_digest,
        "provenance_digest": canonical_digest(profile["provenance"]),
        "lock_digest": _DIGEST_PREFIX + "0" * 64,
    }
    lock["lock_digest"] = canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    )
    return validate_document(lock, "profile_artifact_lock")


def _compile_release_provenance(
    catalog: ProfileCatalog,
    intent_path: Path,
    intent_raw: bytes,
    compatibility_path: Path,
    compatibility_raw: bytes,
    lock_path: Path,
    lock_raw: bytes,
    lock: Mapping[str, Any],
    projection_inputs: Mapping[str, str],
) -> dict[str, Any]:
    inputs = dict(catalog.input_digests)
    inputs[catalog.provenance_path(intent_path)] = _sha256(intent_raw)
    inputs.update(projection_inputs)
    document = {
        "schema": "io.tobkiri.profile-release-provenance.v1",
        "profile_id": lock["profile_id"],
        "profile_revision": lock["profile_revision"],
        "profile_definition_digest": lock["profile_definition_digest"],
        "source_inputs": [{"path": path, "digest": inputs[path]} for path in sorted(inputs)],
        "generator": {
            "name": GENERATOR_NAME,
            "version": GENERATOR_VERSION,
            "path": GENERATOR_PATH,
            "digest": _sha256(Path(__file__).read_bytes()),
        },
        "catalog": {
            "bundle_lock_path": catalog.provenance_path(catalog.lock_path),
            "bundle_digest": lock["bundle_digest"],
            "catalog_revision": lock["catalog_revision"],
            "closure_digest": lock["closure_digest"],
        },
        "outputs": [
            {
                "path": catalog.provenance_path(compatibility_path),
                "digest": _sha256(compatibility_raw),
            },
            {
                "path": catalog.provenance_path(lock_path),
                "digest": _sha256(lock_raw),
            },
        ],
        "release_digest": _DIGEST_PREFIX + "0" * 64,
    }
    document["release_digest"] = canonical_digest(
        {key: value for key, value in document.items() if key != "release_digest"}
    )
    return validate_document(document, "profile_release_provenance")


def render(
    *,
    bundle_root: Path,
    intent_path: Path,
    compatibility_path: Path,
    lock_path: Path,
    provenance_path: Path,
    source_bundle_root: Path | None = None,
) -> dict[Path, bytes]:
    """Render one Named Profile release without reading generated Profile bytes.

    ``source_bundle_root`` projects paths in a temporary staged bundle back to
    its stable source-bundle paths during atomic publication.
    """

    outputs = (compatibility_path, lock_path, provenance_path, bundle_root / "bundle.lock.json")
    _require_output_layout(bundle_root, outputs)
    _reject_symlink_components(intent_path, "Profile intent")
    if not _absolute(intent_path).is_file():
        raise ValueError("Profile intent must be a regular file")
    catalog = ProfileCatalog(
        bundle_root,
        compatibility_path,
        source_bundle_root=source_bundle_root,
    )
    intent_raw = intent_path.read_bytes()
    intent = validate_document(intent_raw, "profile_intent")
    profile, selected, _, pins, projection_inputs = _compile_profile(
        catalog, intent, compatibility_path
    )
    compatibility_raw = _pretty(profile)
    bundle_lock = catalog.bundle_lock_with(compatibility_raw)
    bundle_lock_raw = _pretty(bundle_lock)
    bundle_digest = _sha256(bundle_lock_raw)
    lock = _compile_lock(catalog, intent, profile, selected, pins, bundle_digest)
    lock_raw = _pretty(lock)
    provenance = _compile_release_provenance(
        catalog,
        intent_path,
        intent_raw,
        compatibility_path,
        compatibility_raw,
        lock_path,
        lock_raw,
        lock,
        projection_inputs,
    )
    return {
        compatibility_path: compatibility_raw,
        catalog.lock_path: bundle_lock_raw,
        lock_path: lock_raw,
        provenance_path: _pretty(provenance),
    }


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _exchange_directories(left: Path, right: Path) -> None:
    """Atomically exchange two directories or fail before changing either."""

    library = ctypes.CDLL(None, use_errno=True)
    left_raw = os.fsencode(left)
    right_raw = os.fsencode(right)
    if sys.platform == "darwin" and hasattr(library, "renameatx_np"):
        exchange = library.renameatx_np
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(-2, left_raw, -2, right_raw, 0x00000002)
    elif sys.platform.startswith("linux") and hasattr(library, "renameat2"):
        exchange = library.renameat2
        exchange.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        exchange.restype = ctypes.c_int
        result = exchange(-100, left_raw, -100, right_raw, 0x00000002)
    else:
        raise RuntimeError("atomic directory exchange is unavailable on this platform")
    if result != 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), f"{left} <-> {right}")


def _stage_path(source: Path, source_root: Path, stage: Path) -> Path:
    return stage / _absolute(source).relative_to(source_root)


def _bound_input_path(
    value: str,
    source_root: Path,
    stage: Path,
    *,
    source_bundle_root: Path | None = None,
) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = ROOT / candidate
    candidate = _absolute(candidate)
    if source_bundle_root is not None:
        projected_root = _absolute(source_bundle_root)
        try:
            return stage / candidate.relative_to(projected_root)
        except ValueError:
            pass
    try:
        return stage / candidate.relative_to(source_root)
    except ValueError:
        return candidate


def _validate_staged_release(
    stage: Path,
    source_root: Path,
    rendered: Mapping[Path, bytes],
    *,
    source_bundle_root: Path | None = None,
) -> None:
    """Revalidate a complete immutable generation immediately before exchange."""

    for candidate in stage.rglob("*"):
        if candidate.is_symlink():
            raise ValueError(f"staged bundle contains a symlink: {candidate}")
    documents: dict[str, tuple[Path, Path, bytes, dict[str, Any]]] = {}
    for source, expected in rendered.items():
        target = _stage_path(source, source_root, stage)
        if not target.is_file() or target.read_bytes() != expected:
            raise ValueError(f"staged Profile artifact changed: {source.name}")
        if source.name == "bundle.lock.json":
            continue
        value = _read_object(target, "staged Profile artifact")
        if "lock_api_version" in value:
            documents["lock"] = (
                source,
                target,
                expected,
                validate_document(value, "profile_artifact_lock"),
            )
        elif "profile_api_version" in value:
            documents["profile"] = (
                source,
                target,
                expected,
                validate_document(value, "profile"),
            )
        elif value.get("schema") == "io.tobkiri.profile-release-provenance.v1":
            documents["provenance"] = (
                source,
                target,
                expected,
                validate_document(value, "profile_release_provenance"),
            )
    if set(documents) != {"profile", "lock", "provenance"}:
        raise ValueError("staged Profile release set is incomplete")
    _, profile_path, profile_raw, profile = documents["profile"]
    lock_source, _, lock_raw, lock = documents["lock"]
    _, _, _, provenance = documents["provenance"]
    catalog = ProfileCatalog(
        stage,
        profile_path,
        source_bundle_root=source_bundle_root,
    )
    if catalog.bundle_lock_with(profile_raw) != catalog.lock:
        raise ValueError("staged compatibility Profile bundle digest is stale")
    if lock["profile_revision"] != canonical_digest(profile):
        raise ValueError("staged Profile revision is stale")
    if lock["closure_digest"] != canonical_digest(
        {
            "effective_set": lock["effective_set"],
            "content_projections": lock["content_projections"],
        }
    ):
        raise ValueError("staged Profile closure digest is stale")
    if lock["lock_digest"] != canonical_digest(
        {key: value for key, value in lock.items() if key != "lock_digest"}
    ):
        raise ValueError("staged Profile lock digest is stale")
    bundle_raw = (stage / "bundle.lock.json").read_bytes()
    if lock["bundle_digest"] != _sha256(bundle_raw):
        raise ValueError("staged Profile bundle digest is stale")
    if provenance["release_digest"] != canonical_digest(
        {key: value for key, value in provenance.items() if key != "release_digest"}
    ):
        raise ValueError("staged Profile release digest is stale")
    for item in provenance["source_inputs"]:
        path = _bound_input_path(
            item["path"],
            source_root,
            stage,
            source_bundle_root=source_bundle_root,
        )
        _reject_symlink_components(path, "staged release input")
        if not path.is_file() or _sha256(path.read_bytes()) != item["digest"]:
            raise ValueError(f"staged release input changed: {item['path']}")
    staged_lock_path = _stage_path(lock_source, source_root, stage)
    expected_outputs = {
        catalog.provenance_path(profile_path): _sha256(profile_raw),
        catalog.provenance_path(staged_lock_path): _sha256(lock_raw),
    }
    actual_outputs = {item["path"]: item["digest"] for item in provenance["outputs"]}
    if actual_outputs != expected_outputs:
        raise ValueError("staged Profile release output digests are stale")
    if provenance["profile_revision"] != lock["profile_revision"]:
        raise ValueError("staged release provenance Profile revision is stale")
    if provenance["catalog"]["bundle_digest"] != lock["bundle_digest"]:
        raise ValueError("staged release provenance bundle digest is stale")


def _publish(
    rendered: Mapping[Path, bytes],
    bundle_root: Path,
    *,
    fault: Any | None = None,
) -> None:
    """Publish the complete release set with one atomic directory exchange."""

    outputs = tuple(rendered)
    _require_output_layout(bundle_root, outputs)
    root = _absolute(bundle_root)
    stage = Path(tempfile.mkdtemp(prefix=f".{root.name}.profile-stage-", dir=root.parent))
    stage.rmdir()
    exchanged = False
    try:
        shutil.copytree(root, stage, copy_function=shutil.copy2, symlinks=True)
        for path, raw in rendered.items():
            target = _stage_path(path, root, stage)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
        if fault is not None:
            fault("before_validation", stage)
            fault("before_exchange", stage)
        _validate_staged_release(
            stage,
            root,
            rendered,
            source_bundle_root=root,
        )
        for directory in sorted(
            (item for item in stage.rglob("*") if item.is_dir()),
            key=lambda item: len(item.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        _fsync_directory(stage)
        _exchange_directories(root, stage)
        exchanged = True
        _fsync_directory(root.parent)
        if fault is not None:
            fault("after_exchange", stage)
    except BaseException:
        if exchanged:
            try:
                _exchange_directories(root, stage)
                _fsync_directory(root.parent)
                exchanged = False
            except BaseException as rollback_error:
                raise RuntimeError("Profile release rollback failed") from rollback_error
        raise
    finally:
        if stage.exists() and not exchanged:
            shutil.rmtree(stage)
    if stage.exists():
        shutil.rmtree(stage)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle-root", type=Path, default=DEFAULT_BUNDLE)
    parser.add_argument("--intent", type=Path, default=DEFAULT_INTENT)
    parser.add_argument("--compatibility-profile", type=Path, default=DEFAULT_COMPATIBILITY)
    parser.add_argument("--lock", type=Path, default=DEFAULT_LOCK)
    parser.add_argument("--provenance", type=Path, default=DEFAULT_PROVENANCE)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = render(
        bundle_root=args.bundle_root,
        intent_path=args.intent,
        compatibility_path=args.compatibility_profile,
        lock_path=args.lock,
        provenance_path=args.provenance,
    )
    stale = [
        path for path, raw in rendered.items() if not path.is_file() or path.read_bytes() != raw
    ]
    if args.check:
        for path in stale:
            print(_relative(path))
        return int(bool(stale))
    _publish(rendered, args.bundle_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
