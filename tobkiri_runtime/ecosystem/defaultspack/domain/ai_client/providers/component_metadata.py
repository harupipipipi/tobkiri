from __future__ import annotations

import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from ...components import (
    ComponentEntrypointResolutionError,
    DomainComponent,
    DomainComponentRegistry,
    get_domain_component_registry,
    resolve_component_entrypoint,
)
from ..metadata_json import load_strict_metadata_json
from ..model_metadata_schema import validate_model_catalog_source


_MODEL_CATALOG_PACK_ID = "rumi_model_catalog_pack"


def _model_catalog_root() -> Path:
    return Path(__file__).absolute().parents[4] / _MODEL_CATALOG_PACK_ID / "catalog"


def _safe_catalog_file(path: Path, *, root: Path | None = None) -> Path | None:
    """Return a regular file inside the fixed bundled catalog root."""

    boundary = Path(
        os.path.normpath((root or _model_catalog_root().parent).absolute())
    )
    candidate = Path(os.path.normpath(Path(path).absolute()))
    try:
        if boundary.is_symlink():
            return None
        candidate.relative_to(boundary)
        cursor = candidate
        while cursor != boundary and cursor != cursor.parent:
            if cursor.is_symlink():
                return None
            cursor = cursor.parent
        if cursor != boundary:
            return None
        if not candidate.is_file():
            return None
        return candidate
    except (OSError, RuntimeError, ValueError):
        return None


def _model_catalog_selected() -> bool:
    """Return whether the fixed model-catalog owner is explicitly selected."""

    try:
        from core_runtime.resolved_profile_scope import effective_pack_ids

        return _MODEL_CATALOG_PACK_ID in set(effective_pack_ids())
    except Exception:
        return False


def _is_under(path: Path, root: Path) -> bool:
    try:
        boundary = Path(os.path.normpath(root.absolute()))
        candidate = Path(os.path.normpath(path.absolute()))
        candidate.relative_to(boundary)
        cursor = candidate
        while cursor != boundary and cursor != cursor.parent:
            if cursor.is_symlink():
                return False
            cursor = cursor.parent
        return cursor == boundary and not boundary.is_symlink()
    except (OSError, RuntimeError, ValueError):
        return False


def _provider_id(component_manifest: dict[str, Any]) -> str:
    return str(component_manifest.get("provider_id") or component_manifest.get("id") or "").strip()


def _load_json_entrypoint(
    registry: DomainComponentRegistry,
    component: DomainComponent,
    key: str,
) -> tuple[Any, Path | None]:
    resolved = resolve_component_entrypoint(
        registry,
        category=component.category,
        component_id=component.id,
        contract_id=key,
        required=False,
    )
    if resolved is None:
        return None, None
    try:
        return load_strict_metadata_json(resolved.path), resolved.path
    except (OSError, UnicodeError, ValueError) as exc:
        raise ComponentEntrypointResolutionError(
            category=resolved.category,
            component_id=resolved.component_id,
            contract_id=resolved.contract_id,
            revision=resolved.revision,
            reason="entrypoint_metadata_invalid",
        ) from exc


def _trusted_provider_component_root() -> Path:
    return Path(__file__).resolve().parents[3] / "domain" / "providers"


def _is_trusted_runtime_provider_component(component: Any) -> bool:
    """Return whether a provider component may supply executable runtime config.

    Domain component discovery intentionally catalogs sibling packs and explicit extra
    roots for metadata, but provider manifests are executable runtime configuration:
    an entrypoint is imported and instantiated during provider auto-detection.  Only
    provider components bundled inside the canonical defaultspack provider directory
    are trusted to contribute those runtime manifests.
    """
    try:
        manifest_path = Path(component.manifest_path).resolve()
        manifest_path.relative_to(_trusted_provider_component_root().resolve())
    except (AttributeError, OSError, ValueError):
        return False
    return True


def _is_repository_catalog_component(component: Any) -> bool:
    """Return whether a component is repository-owned declarative catalog data."""

    try:
        return (
            component.source_pack_id == _MODEL_CATALOG_PACK_ID
            and _is_under(Path(component.manifest_path), _model_catalog_root())
        )
    except (AttributeError, OSError):
        return False


def _catalog_provider_manifest_paths(provider_id: str) -> list[Path]:
    catalog_root = _model_catalog_root()
    pack_root = catalog_root.parent
    roots = [
        catalog_root / "providers" / provider_id,
        pack_root / "extensions" / "llm" / "providers" / provider_id,
    ]
    return [
        manifest_path
        for path in roots
        if (manifest_path := _safe_catalog_file(path / "manifest.json", root=pack_root))
        is not None
    ]


def _catalog_models_from_manifest(
    provider_id: str,
    manifest_path: Path,
) -> list[dict[str, Any]]:
    try:
        manifest = load_strict_metadata_json(manifest_path)
    except (OSError, ValueError):
        return []
    if not isinstance(manifest, dict):
        return []

    raw_models: Any = None
    entrypoints = manifest.get("entrypoints")
    relative_models = entrypoints.get("models") if isinstance(entrypoints, dict) else None
    if isinstance(relative_models, str) and relative_models.strip():
        models_path = _safe_catalog_file(manifest_path.parent / relative_models)
        if models_path is not None:
            try:
                raw_models = load_strict_metadata_json(models_path)
            except (OSError, ValueError):
                raw_models = None
    if raw_models is None:
        models_path = _safe_catalog_file(manifest_path.parent / "models.json")
        if models_path is not None:
            try:
                raw_models = load_strict_metadata_json(models_path)
            except (OSError, ValueError):
                raw_models = None
    if raw_models is None:
        model_paths = sorted((manifest_path.parent / "models").glob("*.json"))
        raw_models = []
        for candidate in model_paths:
            model_path = _safe_catalog_file(candidate)
            if model_path is None:
                continue
            try:
                model = load_strict_metadata_json(model_path)
                validate_model_catalog_source({"models": [model]}, path=model_path)
            except (OSError, ValueError):
                continue
            if isinstance(model, dict):
                raw_models.append(model)
    if isinstance(raw_models, dict):
        raw_models = raw_models.get("models")
    if not isinstance(raw_models, list):
        return []

    try:
        validate_model_catalog_source(raw_models, path=manifest_path)
    except ValueError:
        return []
    result: list[dict[str, Any]] = []
    for raw in raw_models:
        if not isinstance(raw, dict):
            continue
        item = deepcopy(raw)
        item.setdefault("provider_id", provider_id)
        item.setdefault("provider", provider_id)
        metadata = dict(item.get("metadata", {})) if isinstance(item.get("metadata"), dict) else {}
        metadata.setdefault("component_id", f"{provider_id}.catalog")
        metadata.setdefault("component_manifest_path", str(manifest_path))
        metadata.setdefault("source_pack_id", _MODEL_CATALOG_PACK_ID)
        item["metadata"] = metadata
        result.append(item)
    return result


def provider_component_metadata_map() -> dict[str, dict[str, Any]]:
    items: dict[str, dict[str, Any]] = {}
    registry = get_domain_component_registry()
    for component in registry.list("providers"):
        manifest = component.as_dict()
        provider_id = _provider_id(manifest)
        if not provider_id:
            continue
        metadata = manifest.get("provider_metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        provider_manifest = manifest.get("provider_manifest")
        if (
            not isinstance(provider_manifest, dict)
            or not (
                _is_trusted_runtime_provider_component(component)
                or _is_repository_catalog_component(component)
            )
        ):
            provider_manifest = {}
        items[provider_id] = {
            **deepcopy(metadata),
            "component_id": component.id,
            "component_manifest_path": manifest.get("source_path", ""),
            "source_pack_id": component.source_pack_id,
            "provider_manifest": deepcopy(provider_manifest),
            "provider_manifest_executable": _is_trusted_runtime_provider_component(component),
        }
    if _model_catalog_selected():
        for manifest_path in sorted((_model_catalog_root() / "providers").glob("*/manifest.json")):
            safe_manifest_path = _safe_catalog_file(manifest_path)
            if safe_manifest_path is None:
                continue
            try:
                manifest = load_strict_metadata_json(safe_manifest_path)
            except (OSError, ValueError):
                continue
            if not isinstance(manifest, dict):
                continue
            provider_id = _provider_id(manifest)
            if not provider_id or provider_id in items:
                continue
            metadata = manifest.get("provider_metadata")
            if not isinstance(metadata, dict):
                metadata = {
                    key: manifest[key]
                    for key in (
                        "display_name",
                        "description",
                        "default_base_url",
                        "default_model",
                        "default_model_for",
                        "api_key_env",
                        "base_url_env",
                        "credential_required",
                    )
                    if key in manifest
                }
            items[provider_id] = {
                **deepcopy(metadata),
                "component_id": provider_id,
                "component_manifest_path": str(safe_manifest_path),
                "source_pack_id": _MODEL_CATALOG_PACK_ID,
                "provider_manifest": deepcopy(manifest.get("provider_manifest") or {}),
                "provider_manifest_executable": False,
            }
    return items


def provider_manifests_from_components() -> dict[str, dict[str, Any]]:
    manifests: dict[str, dict[str, Any]] = {}
    for provider_id, metadata in provider_component_metadata_map().items():
        if not metadata.get("provider_manifest_executable", False):
            continue
        provider_manifest = metadata.get("provider_manifest")
        if not isinstance(provider_manifest, dict) or not provider_manifest:
            continue
        manifest = deepcopy(provider_manifest)
        manifest.setdefault("id", provider_id)
        manifest.setdefault("source_pack_id", metadata.get("source_pack_id", ""))
        manifest.setdefault("component_manifest_path", metadata.get("component_manifest_path", ""))
        manifests[provider_id] = manifest
    return manifests


def model_manifests_from_provider_components(provider_id: str) -> list[dict[str, Any]]:
    provider_id = str(provider_id or "").strip()
    if not provider_id:
        return []
    registry = get_domain_component_registry()
    models: list[dict[str, Any]] = []
    for component in registry.list("providers"):
        manifest = component.as_dict()
        if _provider_id(manifest) != provider_id:
            continue
        raw_models, resolved_models_path = _load_json_entrypoint(
            registry,
            component,
            "models",
        )
        source_path = manifest.get("source_path", "")
        raw_models_path = resolved_models_path or source_path
        trusted_metadata = _is_trusted_runtime_provider_component(component) or _is_repository_catalog_component(component)
        if raw_models is not None and trusted_metadata:
            source_path = manifest.get("source_path", "")
            validate_model_catalog_source(
                raw_models,
                path=raw_models_path,
            )
        if isinstance(raw_models, dict):
            raw_models = raw_models.get("models")
        if not isinstance(raw_models, list):
            continue
        for raw in raw_models:
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            item.setdefault("provider_id", provider_id)
            item.setdefault("provider", provider_id)
            metadata = dict(item.get("metadata", {})) if isinstance(item.get("metadata"), dict) else {}
            metadata.setdefault("component_id", component.id)
            metadata.setdefault("component_manifest_path", manifest.get("source_path", ""))
            metadata.setdefault("source_pack_id", component.source_pack_id)
            item["metadata"] = metadata
            models.append(item)
    if _model_catalog_selected():
        for manifest_path in _catalog_provider_manifest_paths(provider_id):
            models.extend(_catalog_models_from_manifest(provider_id, manifest_path))
    deduplicated: dict[str, dict[str, Any]] = {}
    for item in models:
        model_key = str(
            item.get("id")
            or item.get("qualified_model_id")
            or item.get("model_id")
            or ""
        ).strip()
        if model_key:
            deduplicated.setdefault(model_key, item)
    return list(deduplicated.values())
