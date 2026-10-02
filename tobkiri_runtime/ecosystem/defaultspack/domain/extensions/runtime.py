from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Iterable, Optional

from .activation import selected_extension_pack_ids
from .registry import ExtensionRegistry

_LOCK = threading.Lock()
_REGISTRY: Optional[ExtensionRegistry] = None


def get_extensions_root() -> Path:
    # .../ecosystem/defaultspack/domain/extensions/runtime.py -> .../defaultspack
    pack_root = Path(__file__).resolve().parents[2]
    return pack_root / "extensions"


def _coerce_extension_root(path: Path | str) -> Path:
    candidate = Path(path).expanduser()
    if (candidate / "pack.v4.json").is_file():
        return candidate / "extensions"
    return candidate


def _append_unique_root(roots: list[Path], root: Path | str) -> None:
    candidate = _coerce_extension_root(root)
    if candidate not in roots:
        roots.append(candidate)


def _pack_id_for_root(pack_root: Path) -> str:
    for manifest_path in (
        pack_root / "pack.v4.json",
        pack_root / "v4" / "packs" / f"{pack_root.name}.pack.v4.json",
    ):
        if not manifest_path.is_file():
            continue
        try:
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict):
            continue
        value = str((raw.get("pack") or {}).get("id") or "").strip()
        if value:
            return value
    return pack_root.name


def _append_ecosystem_extension_roots(
    roots: list[Path],
    ecosystem_dir: Path,
    *,
    pack_root: Path,
    selected_pack_ids: set[str] | None,
) -> None:
    if not ecosystem_dir.is_dir():
        return
    active_pack_name = pack_root.name
    active_pack_id = _pack_id_for_root(pack_root)
    for pack_id in sorted(selected_pack_ids or set()):
        path = ecosystem_dir / pack_id
        extensions = path / "extensions"
        if path == pack_root or path.name in {active_pack_name, active_pack_id}:
            continue
        if path.is_dir() and _pack_id_for_root(path) == pack_id and extensions.is_dir():
            _append_unique_root(roots, extensions)


def build_extensions_roots(
    pack_root: Path | str,
    *,
    extra_roots: Iterable[Path | str] | None = None,
) -> list[Path]:
    pack_root = Path(pack_root)
    ecosystem_dir = pack_root.parent
    roots: list[Path] = []
    default_root = pack_root / "extensions"
    selected_pack_ids = selected_extension_pack_ids(pack_root)

    # Core defaults must load first so sibling packs and user/env roots can
    # extend or override them by id.
    _append_unique_root(roots, default_root)

    _append_ecosystem_extension_roots(
        roots,
        ecosystem_dir,
        pack_root=pack_root,
        selected_pack_ids=selected_pack_ids,
    )
    for root in extra_roots or ():
        _append_unique_root(roots, root)
    return roots


def get_extensions_roots() -> list[Path]:
    pack_root = Path(__file__).resolve().parents[2]
    return build_extensions_roots(pack_root)


def get_extension_registry(
    *,
    force_reload: bool = False,
    strict: bool = False,
) -> ExtensionRegistry:
    global _REGISTRY
    roots = get_extensions_roots()
    if _REGISTRY is None:
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = ExtensionRegistry(roots, strict=strict)
    elif force_reload or [Path(root) for root in roots] != list(getattr(_REGISTRY, "_roots", [])):
        with _LOCK:
            if _REGISTRY is None:
                _REGISTRY = ExtensionRegistry(roots, strict=strict)
            else:
                _REGISTRY._roots = [Path(root) for root in roots]
                _REGISTRY._root = _REGISTRY._roots[0] if _REGISTRY._roots else Path(".")
                _REGISTRY._strict = strict
                _REGISTRY.reload()
    return _REGISTRY
