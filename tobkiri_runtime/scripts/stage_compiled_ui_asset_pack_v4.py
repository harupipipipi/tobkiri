"""Stage an external UI asset Pack without changing the bundled catalog."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.external_pack_catalog_v4 import (  # noqa: E402
    _read_regular_from_root_descriptor,
)
from core_runtime.ui_asset_pack_authoring import build_ui_asset_pack  # noqa: E402
from scripts.compiled_ui_artifacts_v4 import (  # noqa: E402
    _identity, _open_root, compiled_ui_artifacts,
)


def stage_compiled_ui_asset_pack(
    ui_root: Path, target: Path, *, pack_id: str, version: str,
) -> Path:
    """Capture verified compiled bytes and use the ordinary artifact compiler."""
    inventory = compiled_ui_artifacts(ui_root)
    descriptor = _open_root(ui_root)
    try:
        before = _identity(os.fstat(descriptor))
        assets: dict[str, bytes] = {}
        for item in inventory:
            relative = item["path"].removeprefix("ui/")
            content, _ = _read_regular_from_root_descriptor(
                descriptor, relative, max_file_bytes=32 * 1024 * 1024,
            )
            if "sha256:" + hashlib.sha256(content).hexdigest() != item["digest"]:
                raise ValueError("compiled UI changed before capture")
            assets[item["path"]] = content
        if _identity(os.fstat(descriptor)) != before:
            raise ValueError("compiled UI root changed during capture")
    finally:
        os.close(descriptor)
    return build_ui_asset_pack(
        target, pack_id=pack_id, version=version,
        display_name="Tobkiri Default UI renderer", assets=assets,
    )


def main() -> None:
    """Produce unsigned inputs; signing and Native review remain separate."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ui-root", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--pack-id", default="defaultspack_ui_assets_pack")
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    result = stage_compiled_ui_asset_pack(
        args.ui_root, args.target, pack_id=args.pack_id, version=args.version,
    )
    print(result)


if __name__ == "__main__":
    main()
