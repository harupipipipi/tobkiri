"""Fixed packaged Host-helper role entrypoint."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Sequence

_TARGET_MODULE = None


def _load_host_helper_module():
    """Load and cache the canonical stdin/stdout JSON helper."""
    global _TARGET_MODULE
    if _TARGET_MODULE is not None:
        return _TARGET_MODULE
    app_root = Path(__file__).resolve().parents[1] / "app"
    target = app_root / "core_runtime" / "host_broker" / "computer_host_helper.py"
    if target.is_symlink() or not target.is_file():
        raise RuntimeError(f"canonical Host helper entrypoint is missing: {target}")
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    spec = importlib.util.spec_from_file_location(
        "tobkiri_packaged_host_helper",
        target,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"canonical Host helper entrypoint is not importable: {target}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _TARGET_MODULE = module
    return module


def _load_host_helper_main():
    """Return the cached canonical host-helper main function."""
    return _load_host_helper_module().main


def prepare_for_dispatch(_scope: object):
    """Load the role target before the sealed Launcher attestation."""
    return _load_host_helper_main()


def main(_argv: Sequence[str] | None = None) -> int:
    """Run ``computer_host_helper.py`` without touching its JSON stdio pipe."""
    return int(_load_host_helper_main()())


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
