"""Fixed packaged Kernel role entrypoint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence

_TARGET_MODULE = None


def _load_application_module():
    """Load and cache the canonical long-lived Host composition root."""
    global _TARGET_MODULE
    if _TARGET_MODULE is not None:
        return _TARGET_MODULE
    app_root = Path(__file__).resolve().parents[1] / "app"
    target = app_root / "app.py"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"canonical Kernel entrypoint is missing: {target}")
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    spec = importlib.util.spec_from_file_location("tobkiri_packaged_kernel", target)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"canonical Kernel entrypoint is not importable: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TARGET_MODULE = module
    return module


def _load_application_main():
    """Return the cached canonical application main function."""
    return _load_application_module().main


def prepare_for_dispatch(scope: object):
    """Bind the verified sealed scope before exposing the Host entrypoint."""
    module = _load_application_module()
    prepare = getattr(module, "prepare_for_sealed_dispatch", None)
    if not callable(prepare):
        raise RuntimeError("Kernel target lacks sealed dispatch preparation")
    prepare(scope)
    return module.main


def main(argv: Sequence[str] | None = None) -> int:
    """Run ``app.py`` with role arguments unchanged."""
    return int(_load_application_main()(list(argv or ())))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
