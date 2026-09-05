"""Normalize Pack manifests and maintain deterministic authority projections."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend_core.ecosystem.spec.schema.validator import (  # noqa: E402
    validate_ecosystem,
)
from scripts.quality.legacy_manifest_v3 import load_manifest  # noqa: E402
from scripts.offline_legacy_projection import (  # noqa: E402
    render_legacy_ecosystem,
)
from tobkiri_protocol.errors import SchemaValidationError  # noqa: E402
from tobkiri_protocol.validation import validate_document  # noqa: E402

ECOSYSTEM = ROOT / "ecosystem"
CATALOG = ROOT / "schemas" / "manifest_authority.v1.json"
PACK_V4_CATALOG = ROOT / "schemas" / "pack_v4_catalog.v1.json"
V4_PROJECTION_GENERATOR = "tobkiri.scripts.migrate_manifest_authority/v2"


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _load_v4(pack_root: Path) -> dict[str, Any]:
    """Load the Pack v4 artifact that owns one compatibility projection."""
    path = pack_root / "pack.v4.json"
    try:
        payload = validate_document(path.read_bytes(), "pack")
    except (OSError, SchemaValidationError) as exc:
        raise SystemExit(f"cannot read canonical v4 artifact {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"canonical v4 artifact must be an object: {path}")
    pack = payload.get("pack")
    integrity = payload.get("integrity")
    if not isinstance(pack, dict) or not isinstance(integrity, dict):
        raise SystemExit(f"canonical v4 artifact is missing integrity: {path}")
    if str(pack.get("id") or "") != pack_root.name:
        raise SystemExit(f"canonical v4 Pack identity mismatch: {path}")
    source_identity = str(integrity.get("source_identity") or "").strip()
    artifact_digest = str(pack.get("artifact_digest") or "").strip()
    if not source_identity or not artifact_digest:
        raise SystemExit(f"canonical v4 artifact has incomplete integrity: {path}")
    implementation_digests = {
        str(function.get("implementation_digest"))
        for function in payload.get("functions", [])
        if isinstance(function, dict) and function.get("implementation_digest")
    }
    return {
        "pack_id": str(pack["id"]),
        "version": str(pack.get("version") or ""),
        "source_identity": source_identity,
        "artifact_digest": artifact_digest,
        "implementation_digests": implementation_digests,
    }


def _v4_build_identity(v4: dict[str, Any]) -> str:
    """Return a deterministic build identity derived from the v4 Pack."""
    return f"{v4['pack_id']}:{v4['version']}"


def _pin_v4_projection(ecosystem: dict[str, Any], v4: dict[str, Any]) -> None:
    """Bind a legacy compatibility document to its finite v4 source."""
    metadata = ecosystem.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata["canonical_v4"] = {
        "artifact": "pack.v4.json",
        "artifact_digest": v4["artifact_digest"],
        "generator": V4_PROJECTION_GENERATOR,
        "source_identity": v4["source_identity"],
    }
    ecosystem["metadata"] = metadata


def _mark_offline_projection(
    ecosystem: dict[str, Any], v4: dict[str, Any], *, historical_format: str
) -> None:
    """Mark a compatibility document as a non-authoritative v4 projection."""

    metadata = ecosystem.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    metadata.update(
        {
            "manifest_authority": "v4-authoritative",
            "format": "rumi.ecosystem.v1",
            "generated": True,
            "read_only_projection": True,
            "projection_owner": "scripts/migrate_manifest_authority.py",
            "generated_from": {
                "source": "pack.v4.json",
                "source_content_hash": v4["source_identity"],
                "generator": V4_PROJECTION_GENERATOR,
                "historical_format": historical_format,
            },
        }
    )
    ecosystem["metadata"] = metadata


def _set_provenance(
    value: dict[str, Any],
    *,
    content_hash: str,
    build_identity: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Set only deterministic provenance fields while preserving trust data."""
    provenance = dict(existing) if isinstance(existing, dict) else {}
    provenance.update(
        {
            "content_hash": content_hash,
            "build_identity": build_identity,
            "trust_class": str(provenance.get("trust_class") or "local"),
            "signature": provenance.get("signature"),
        }
    )
    value["provenance"] = provenance
    return provenance


def _projection_content_hash(
    manifest: dict[str, Any],
    v4: dict[str, Any],
    artifact_index_hash: str | None,
) -> str:
    """Choose an unambiguous content hash for one finite projection."""
    if artifact_index_hash:
        return artifact_index_hash
    entrypoint_hashes = {
        str(item.get("artifact_hash") or "")
        for item in manifest.get("entrypoints", [])
        if isinstance(item, dict) and item.get("artifact_hash")
    }
    if len(entrypoint_hashes) == 1:
        return next(iter(entrypoint_hashes))
    if len(entrypoint_hashes) > 1:
        raise SystemExit(
            "projection has multiple entrypoint artifacts without an artifact index"
        )
    return v4["artifact_digest"]


def _normalize_v3(
    manifest: dict[str, Any],
    pack_root: Path,
    v4: dict[str, Any],
) -> dict[str, Any]:
    for provided in manifest.get("contracts", {}).get("provides", []):
        if provided.get("security") == "critical":
            provided["security"] = "restricted"
        lifecycle = provided.get("lifecycle")
        if isinstance(lifecycle, dict) and lifecycle.get("data_owner") is None:
            lifecycle.pop("data_owner", None)
    for required in manifest.get("contracts", {}).get("requires", []):
        if "version_range" not in required:
            required["version_range"] = required.pop("version", ">=1.0.0")
        else:
            required.pop("version", None)
        required.setdefault("optional", required.get("cardinality") == "optional")
        required["version_range"] = str(required["version_range"]).replace(
            ",", " "
        )
        required.pop("failure", None)
        for key in set(required) - {
            "id",
            "version_range",
            "cardinality",
            "optional",
            "instance_key",
        }:
            required.pop(key, None)
    migration = manifest.get("migration")
    if isinstance(migration, dict):
        projection = migration.get("compatibility_projection")
        if projection not in {"none", "legacy_to_v3_read_only"}:
            migration["compatibility_projection"] = "legacy_to_v3_read_only"
        aliases = migration.get("compatibility_aliases")
        if isinstance(aliases, list) and any(isinstance(item, str) for item in aliases):
            migration["compatibility_aliases"] = []
    for permission in manifest.get("permissions", []):
        if permission.get("access") == "publish":
            permission["access"] = "execute"
    normalized_resources = []
    for resource in manifest.get("resources", []):
        if not isinstance(resource, dict):
            continue
        path_value = str(resource.get("path") or "").strip()
        content_hash = str(resource.get("content_hash") or "").strip()
        if not content_hash and path_value:
            candidate = (pack_root / path_value).resolve()
            try:
                candidate.relative_to(pack_root.resolve())
                content_hash = _sha256(candidate)
            except (OSError, ValueError):
                content_hash = ""
        normalized_resources.append(
            {
                "id": str(resource.get("id") or path_value),
                "kind": str(resource.get("kind") or "file"),
                "content_hash": content_hash,
            }
        )
    manifest["resources"] = normalized_resources
    for entrypoint in manifest.get("entrypoints", []):
        module = str(entrypoint.get("module") or "").strip()
        if not module:
            continue
        candidate = ROOT.joinpath(*module.split(".")).with_suffix(".py")
        if not candidate.is_file():
            raise SystemExit(f"v3 entrypoint module is missing: {candidate}")
        artifact_hash = _sha256(candidate)
        if v4["implementation_digests"] and artifact_hash not in v4[
            "implementation_digests"
        ]:
            raise SystemExit(
                f"v3 entrypoint is not pinned by canonical v4 implementation: {candidate}"
            )
        entrypoint["artifact_hash"] = artifact_hash
    return manifest


def _normalize_artifact_index(
    pack_root: Path,
    ecosystem: dict[str, Any],
    *,
    check: bool,
    include_unreferenced_sidecar: bool = False,
) -> str | None:
    """Refresh a declared index, or an orphan v3 artifact sidecar.

    Older v3-authoritative Packs shipped ``artifact-manifest.json`` without
    linking it from the compatibility projection.  It is still a generated
    integrity sidecar and must track the declared bytes, but it must not
    become a second authority or change projection provenance semantics.
    """

    metadata = ecosystem.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    integrity = metadata.get("integrity")
    integrity = integrity if isinstance(integrity, dict) else {}
    relative = str(integrity.get("artifact_manifest") or "").strip()
    referenced = bool(relative)
    if not relative and include_unreferenced_sidecar:
        sidecar = pack_root / "artifact-manifest.json"
        if sidecar.is_file():
            relative = sidecar.name
    if not relative:
        return None
    index_path = (pack_root / relative).resolve()
    index_path.relative_to(pack_root.resolve())
    original = index_path.read_text(encoding="utf-8")
    payload = json.loads(original)
    artifacts = payload.get("artifacts")
    if not isinstance(artifacts, list):
        raise SystemExit(f"artifact index has no artifacts: {index_path}")
    expected = original
    for item in artifacts:
        if not isinstance(item, dict) or not item.get("path"):
            raise SystemExit(f"invalid artifact entry: {index_path}")
        candidate = (pack_root / str(item["path"])).resolve()
        candidate.relative_to(pack_root.resolve())
        actual = _sha256(candidate)
        declared = str(item.get("sha256") or "")
        if declared.removeprefix("sha256:") != actual.removeprefix("sha256:"):
            replacement = (
                actual
                if declared.startswith("sha256:")
                else actual.removeprefix("sha256:")
            )
            token = json.dumps(declared, ensure_ascii=False)
            replacement_token = json.dumps(replacement, ensure_ascii=False)
            if token not in expected:
                raise SystemExit(f"artifact hash field is not writable: {index_path}")
            expected = expected.replace(token, replacement_token, 1)
            item["sha256"] = replacement
    if referenced:
        expected = json.dumps(
            payload, ensure_ascii=False, indent=2, sort_keys=True
        ) + "\n"
    if check:
        if index_path.read_text(encoding="utf-8") != expected:
            raise SystemExit(f"artifact index drift: {index_path}")
    else:
        index_path.write_text(expected, encoding="utf-8")
    if not referenced:
        return None
    return "sha256:" + hashlib.sha256(expected.encode("utf-8")).hexdigest()


def _schema_properties() -> set[str]:
    schema = json.loads(
        (
            ROOT
            / "backend_core"
            / "ecosystem"
            / "spec"
            / "schema"
            / "ecosystem.schema.json"
        ).read_text(encoding="utf-8")
    )
    return set(schema["properties"])


def _normalize_legacy(data: dict[str, Any]) -> dict[str, Any]:
    result = dict(data)
    metadata = result.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    for generated_key in (
        "format",
        "generated",
        "generated_from",
        "manifest_authority",
        "read_only_projection",
    ):
        metadata.pop(generated_key, None)
    annotations = metadata.get("legacy_annotations")
    annotations = dict(annotations) if isinstance(annotations, dict) else {}
    depends_on = result.pop("depends_on", None)
    if "dependencies" not in result and isinstance(depends_on, list):
        result["dependencies"] = {
            str(item["pack_id"]): str(item.get("version") or ">=0.0.0")
            for item in depends_on
            if isinstance(item, dict) and item.get("pack_id")
        }
    elif depends_on is not None:
        annotations["depends_on"] = depends_on
    dependencies = result.get("dependencies")
    if isinstance(dependencies, dict) and "defaultspack" in dependencies:
        annotations["runtime_dependency_aliases"] = ["defaultspack"]
        dependencies = dict(dependencies)
        dependencies.pop("defaultspack", None)
        result["dependencies"] = dependencies
    elif isinstance(dependencies, list):
        runtime_aliases = []
        filtered = []
        for dependency in dependencies:
            dependency_id = (
                dependency.get("pack_id") or dependency.get("id")
                if isinstance(dependency, dict)
                else dependency
            )
            if str(dependency_id or "").strip() == "defaultspack":
                runtime_aliases.append("defaultspack")
                continue
            filtered.append(dependency)
        if runtime_aliases:
            annotations["runtime_dependency_aliases"] = runtime_aliases
            result["dependencies"] = filtered
    allowed = _schema_properties()
    for key in sorted(set(result) - allowed):
        annotations[key] = result.pop(key)
    vocabulary = result.get("vocabulary")
    if not isinstance(vocabulary, dict) or not vocabulary.get("types"):
        result["vocabulary"] = {"types": ["service"]}
    runtime = result.get("runtime")
    if isinstance(runtime, dict) and runtime.get("type") == "verified_hybrid_pack":
        annotations["runtime"] = result.pop("runtime")
    connectivity = result.get("connectivity")
    if isinstance(connectivity, dict):
        extras = set(connectivity) - {"requires", "provides"}
        if extras:
            annotations["connectivity"] = {
                key: connectivity.pop(key) for key in sorted(extras)
            }
    if annotations:
        metadata["legacy_annotations"] = annotations
    result["metadata"] = metadata
    return result


def migrate(*, check: bool) -> None:
    pack_catalog = json.loads(PACK_V4_CATALOG.read_text(encoding="utf-8"))
    pack_ids = tuple(str(item) for item in pack_catalog.get("pack_ids") or ())
    if len(pack_ids) != len(set(pack_ids)):
        raise SystemExit("canonical Pack catalog contains duplicate IDs")
    pack_roots = [ECOSYSTEM / pack_id for pack_id in sorted(pack_ids)]
    if any(not root.is_dir() for root in pack_roots):
        raise SystemExit("canonical Pack catalog references a missing Pack root")
    authorities = {root.name: "v4-authoritative" for root in pack_roots}
    catalog_text = json.dumps(
        {"version": 1, "packs": authorities},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    if check:
        if not CATALOG.is_file() or CATALOG.read_text(encoding="utf-8") != catalog_text:
            raise SystemExit("manifest authority catalog drift")
    else:
        CATALOG.write_text(catalog_text, encoding="utf-8")

    for root in pack_roots:
        ecosystem_path = root / "ecosystem.json"
        if not ecosystem_path.is_file():
            continue
        v4 = _load_v4(root)
        ecosystem = _normalize_legacy(
            json.loads(ecosystem_path.read_text(encoding="utf-8"))
        )
        v3_path = root / "rumi.pack.v3.json"
        artifact_index_hash = _normalize_artifact_index(
            root,
            ecosystem,
            check=check,
            include_unreferenced_sidecar=v3_path.is_file(),
        )
        _pin_v4_projection(ecosystem, v4)
        if v3_path.is_file():
            manifest = _normalize_v3(
                json.loads(v3_path.read_text(encoding="utf-8")), root, v4
            )
            content_hash = _projection_content_hash(
                manifest, v4, artifact_index_hash
            )
            build_identity = _v4_build_identity(v4)
            manifest_provenance = _set_provenance(
                manifest,
                content_hash=content_hash,
                build_identity=build_identity,
                existing=manifest.get("provenance"),
            )
            _set_provenance(
                ecosystem,
                content_hash=content_hash,
                build_identity=build_identity,
                existing=manifest_provenance,
            )
            extensions = manifest.setdefault("extensions", {})
            options = extensions.setdefault("rumi.legacy_projection", {})
            options["pack_id"] = root.name
            options["dependencies"] = ecosystem.get("dependencies", {})
            options["host_execution"] = bool(
                ecosystem.get("host_execution", False)
            )
            options["manifest"] = ecosystem
            extensions["tobkiri.offline_projection"] = {
                "owner": root.name,
                "source": "pack.v4.json",
                "source_identity": v4["source_identity"],
                "generator": V4_PROJECTION_GENERATOR,
                "runtime_executable": False,
            }
            v3_text = json.dumps(
                manifest, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
            if check:
                if v3_path.read_text(encoding="utf-8") != v3_text:
                    raise SystemExit(f"canonical v3 manifest drift: {v3_path}")
            else:
                v3_path.write_text(v3_text, encoding="utf-8")
            loaded = load_manifest(v3_path)
            if not loaded.ok or loaded.value is None:
                raise SystemExit(f"invalid canonical v3 manifest {v3_path}: {loaded.diagnostics}")
            projected = json.loads(render_legacy_ecosystem(loaded.value))
            _pin_v4_projection(projected, v4)
            _mark_offline_projection(
                projected, v4, historical_format="rumi.pack.v3.json"
            )
            ecosystem_text = json.dumps(
                projected, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
        else:
            _set_provenance(
                ecosystem,
                content_hash=v4["artifact_digest"],
                build_identity=_v4_build_identity(v4),
                existing=ecosystem.get("provenance"),
            )
            _mark_offline_projection(
                ecosystem, v4, historical_format="ecosystem.json"
            )
            ecosystem_text = json.dumps(
                ecosystem, ensure_ascii=False, indent=2, sort_keys=True
            ) + "\n"
        if check:
            if ecosystem_path.read_text(encoding="utf-8") != ecosystem_text:
                raise SystemExit(f"legacy projection drift: {ecosystem_path}")
        else:
            ecosystem_path.write_text(ecosystem_text, encoding="utf-8")
        errors = validate_ecosystem(
            json.loads(ecosystem_text), raise_on_error=False
        )
        if errors:
            raise SystemExit(f"invalid legacy projection {ecosystem_path}: {errors}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    migrate(check=args.check)


if __name__ == "__main__":
    main()
