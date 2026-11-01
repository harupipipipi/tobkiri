"""Compatibility bridge for legacy transport -> block dispatch.

Transport code should not import individual block handlers directly. This
module centralizes the legacy adapter path so block-backed transports can
continue to work while function-first routes become the primary path.
"""

from __future__ import annotations

import importlib
import sys
import threading
from pathlib import Path
from typing import Any, Dict

_PACK_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _PACK_ROOT.parent.parent
_REPO_ECOSYSTEM_ROOT = _REPO_ROOT / "ecosystem"
_PACK_LOCAL_PACKAGES = ("blocks", "domain", "transport", "ecosystem")
_IMPORT_LOCK = threading.RLock()


def _path_is_inside_pack(path: Path) -> bool:
    try:
        path.resolve().relative_to(_PACK_ROOT)
        return True
    except (OSError, ValueError):
        return False


def _path_is_inside_repo_ecosystem(path: Path) -> bool:
    try:
        path.resolve().relative_to(_REPO_ECOSYSTEM_ROOT)
        return True
    except (OSError, ValueError):
        return False


def _module_is_from_local_package(module: Any, package_name: str) -> bool:
    module_file = getattr(module, "__file__", None)
    path_check = _path_is_inside_repo_ecosystem if package_name == "ecosystem" else _path_is_inside_pack
    if not module_file:
        module_paths = getattr(module, "__path__", None)
        if module_paths is None:
            return False
        return any(path_check(Path(item)) for item in module_paths)
    return path_check(Path(module_file))


def _drop_foreign_top_level_package(package_name: str) -> None:
    module = sys.modules.get(package_name)
    if module is None or _module_is_from_local_package(module, package_name):
        return
    prefix = package_name + "."
    for loaded_name in list(sys.modules):
        if loaded_name == package_name or loaded_name.startswith(prefix):
            sys.modules.pop(loaded_name, None)


def _prepare_pack_imports() -> None:
    if type(sys.path) is not list:
        # Sealed packaged runtime: sys.path is the frozen attested object and
        # already contains the pack root, so it must not be rebound or mutated.
        return
    pack_root = str(_PACK_ROOT)
    sys.path = [item for item in sys.path if item != pack_root]
    repo_root = str(_REPO_ROOT)
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)
    sys.path.insert(0, pack_root)
    for package_name in _PACK_LOCAL_PACKAGES:
        _drop_foreign_top_level_package(package_name)


def invoke_block(module_name: str, input_data: Dict[str, Any], context: Dict[str, Any]) -> Any:
    # The standalone HTTP server handles UI bootstrap requests concurrently.
    # Import preparation mutates sys.path/sys.modules, so keep it serialized.
    with _IMPORT_LOCK:
        _prepare_pack_imports()
        module = importlib.import_module(module_name)
    handler = getattr(module, "run", None)
    if handler is None:
        raise AttributeError(f"run not found in {module_name}")
    return handler(input_data, context)
