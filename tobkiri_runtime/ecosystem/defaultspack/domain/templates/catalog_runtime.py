from __future__ import annotations

import hashlib
import threading
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..extensions.activation import (
    DEFAULT_PACK_ID,
    selected_extension_pack_ids,
    setup_pack_selection_path,
)
from ._helpers import canonical_json
from .discovery import TemplateRoot, default_template_roots
from .models import CURRENT_TEMPLATE_SCHEMA_VERSION, TemplateTrustLevel


@dataclass(frozen=True)
class TemplateCatalogSnapshot:
    catalog: dict[str, Any]
    generation: str
    source_files: tuple[str, ...]
    diagnostics: tuple[dict[str, Any], ...]


class TemplateCatalogProvider:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._snapshots: dict[tuple[str, tuple[str, ...]], TemplateCatalogSnapshot] = {}

    def get_snapshot(
        self,
        *,
        defaultspack_root: str | Path | None = None,
        roots: list[str | Path | TemplateRoot] | None = None,
        force_reload: bool = False,
    ) -> TemplateCatalogSnapshot:
        root_key = str(_default_root(defaultspack_root))
        roots_key = tuple(_root_cache_value(root) for root in roots or [])
        cache_key = (root_key, roots_key)
        generation, source_files = _catalog_generation(
            defaultspack_root=defaultspack_root,
            roots=roots,
        )
        with self._lock:
            snapshot = self._snapshots.get(cache_key)
            if snapshot is not None and snapshot.generation == generation and not force_reload:
                return snapshot
            catalog = _build_catalog(defaultspack_root=defaultspack_root, roots=roots)
            catalog["catalog_generation"] = generation
            catalog["schema_version"] = CURRENT_TEMPLATE_SCHEMA_VERSION
            diagnostics = tuple(
                dict(item)
                for item in catalog.get("template_diagnostics", [])
                if isinstance(item, dict)
            )
            snapshot = TemplateCatalogSnapshot(
                catalog=deepcopy(catalog),
                generation=generation,
                source_files=tuple(source_files),
                diagnostics=diagnostics,
            )
            self._snapshots[cache_key] = snapshot
            return snapshot

    def invalidate(
        self,
        *,
        defaultspack_root: str | Path | None = None,
    ) -> None:
        root_key = str(_default_root(defaultspack_root))
        with self._lock:
            for cache_key in list(self._snapshots):
                if cache_key[0] == root_key:
                    self._snapshots.pop(cache_key, None)


_PROVIDER = TemplateCatalogProvider()


def get_template_catalog_snapshot(
    *,
    defaultspack_root: str | Path | None = None,
    roots: list[str | Path | TemplateRoot] | None = None,
    force_reload: bool = False,
) -> TemplateCatalogSnapshot:
    return _PROVIDER.get_snapshot(
        defaultspack_root=defaultspack_root,
        roots=roots,
        force_reload=force_reload,
    )


def invalidate_template_catalog(
    *,
    defaultspack_root: str | Path | None = None,
) -> None:
    _PROVIDER.invalidate(defaultspack_root=defaultspack_root)


def current_template_catalog_generation(
    *,
    defaultspack_root: str | Path | None = None,
    roots: list[str | Path | TemplateRoot] | None = None,
) -> str:
    return _catalog_generation(defaultspack_root=defaultspack_root, roots=roots)[0]


def _build_catalog(
    *,
    defaultspack_root: str | Path | None,
    roots: list[str | Path | TemplateRoot] | None,
) -> dict[str, Any]:
    from .projectors import build_template_catalog

    return build_template_catalog(defaultspack_root=defaultspack_root, roots=roots)


def _catalog_generation(
    *,
    defaultspack_root: str | Path | None,
    roots: list[str | Path | TemplateRoot] | None,
) -> tuple[str, list[str]]:
    descriptors = _template_root_descriptors(defaultspack_root=defaultspack_root, roots=roots)
    root_paths = [descriptor.path for descriptor in descriptors]
    files = _template_files(root_paths)
    payload = {
        "roots": [
            {
                "path": str(descriptor.path),
                "trust_level": _trust_value(descriptor.trust_level),
                "source_pack_id": descriptor.source_pack_id or "",
                "source_kind": descriptor.source_kind,
                "pack_manifest_sha256": _pack_manifest_sha256(descriptor),
            }
            for descriptor in descriptors
        ],
        "selected_pack_ids": sorted(
            _selected_pack_ids_for_generation(defaultspack_root, roots, descriptors)
        ),
        "selection_sha256": _selection_sha256(defaultspack_root, roots),
        "schema_version": CURRENT_TEMPLATE_SCHEMA_VERSION,
        "files": [
            {
                "path": _relative_template_path(path, root_paths),
                "sha256": _file_sha256(path),
            }
            for path in files
        ],
    }
    generation = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return generation, [str(path) for path in files]


def _template_root_descriptors(
    *,
    defaultspack_root: str | Path | None,
    roots: list[str | Path | TemplateRoot] | None,
) -> list[TemplateRoot]:
    if roots is not None:
        descriptors = [
            root
            if isinstance(root, TemplateRoot)
            else TemplateRoot(Path(root), TemplateTrustLevel.USER)
            for root in roots
        ]
    else:
        descriptors = default_template_roots(defaultspack_root)
    result: list[TemplateRoot] = []
    seen: set[tuple[Path, str, str]] = set()
    for descriptor in descriptors:
        resolved = descriptor.path.resolve()
        key = (
            resolved,
            _trust_value(descriptor.trust_level),
            descriptor.source_pack_id or "",
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(
            TemplateRoot(
                resolved,
                descriptor.trust_level,
                source_pack_id=descriptor.source_pack_id,
                source_kind=descriptor.source_kind,
            )
        )
    return result


def _template_files(root_paths: list[Path]) -> list[Path]:
    files: set[Path] = set()
    for root in root_paths:
        if root.is_file() and root.name == "template.json":
            files.add(root.resolve())
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("template.json"):
            if path.is_file():
                files.add(path.resolve())
    return sorted(files)


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _pack_manifest_sha256(root: TemplateRoot) -> str:
    if root.source_kind != "selected_sibling_pack":
        return ""
    pack_root = root.path.parent
    for manifest_name in ("rumi-pack.json", "ecosystem.json"):
        manifest_path = pack_root / manifest_name
        if manifest_path.is_file():
            return _file_sha256(manifest_path)
    return ""


def _selected_pack_ids_for_generation(
    defaultspack_root: str | Path | None,
    roots: list[str | Path | TemplateRoot] | None,
    descriptors: list[TemplateRoot],
) -> set[str]:
    selected = {
        descriptor.source_pack_id for descriptor in descriptors if descriptor.source_pack_id
    }
    if roots is not None:
        return selected
    configured = selected_extension_pack_ids(_default_root(defaultspack_root))
    if configured is not None:
        selected.update(configured - {DEFAULT_PACK_ID})
    return selected


def _selection_sha256(
    defaultspack_root: str | Path | None,
    roots: list[str | Path | TemplateRoot] | None,
) -> str:
    if roots is not None:
        return ""
    return _file_sha256(setup_pack_selection_path(_default_root(defaultspack_root)))


def _relative_template_path(path: Path, roots: list[Path]) -> str:
    resolved = path.resolve()
    for root in roots:
        try:
            return str(resolved.relative_to(root.resolve()))
        except (OSError, ValueError):
            continue
    return resolved.name


def _default_root(defaultspack_root: str | Path | None) -> Path:
    return (
        Path(defaultspack_root).resolve()
        if defaultspack_root is not None
        else Path(__file__).resolve().parents[2]
    )


def _trust_value(value: TemplateTrustLevel | str) -> str:
    return value.value if isinstance(value, TemplateTrustLevel) else str(value)


def _root_cache_value(root: str | Path | TemplateRoot) -> str:
    if isinstance(root, TemplateRoot):
        return "|".join(
            (
                str(root.path.resolve()),
                _trust_value(root.trust_level),
                root.source_pack_id or "",
                root.source_kind,
            )
        )
    return str(Path(root).resolve())
