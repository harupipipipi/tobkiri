"""Fixed packaged Defaultspack role entrypoint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence

_TARGET_MODULE = None


def _load_desktop_app_module():
    """Load and cache the canonical long-lived Defaultspack desktop server."""
    global _TARGET_MODULE
    if _TARGET_MODULE is not None:
        return _TARGET_MODULE
    app_root = Path(__file__).resolve().parents[1] / "app"
    target = app_root / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"canonical Defaultspack entrypoint is missing: {target}")
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    spec = importlib.util.spec_from_file_location(
        "tobkiri_packaged_defaultspack",
        target,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"canonical Defaultspack entrypoint is not importable: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TARGET_MODULE = module
    return module


def _load_desktop_app_main():
    """Return the cached canonical desktop application main function."""
    return _load_desktop_app_module().main


def prepare_for_dispatch(scope: object):
    """Prepare the sealed import path before the Launcher attestation."""
    module = _load_desktop_app_module()
    prepare = getattr(module, "prepare_for_sealed_dispatch", None)
    if not callable(prepare):
        raise RuntimeError("Defaultspack target lacks sealed dispatch preparation")
    prepare(scope)
    return module.main


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``desktop_app.py`` with argv, environment, and stdio untouched."""
    return int(_load_desktop_app_main()(list(argv or ())))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
