"""Stage an unsigned implementation-only Host Extension successor offline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core_runtime.host_extension_authoring import (  # noqa: E402
    build_host_extension_successor,
)
from core_runtime.host_extension_update_v4 import _relative_path  # noqa: E402
from tobkiri_protocol.secure_persistence import SecureDirectory  # noqa: E402


def stage_host_extension_successor(
    predecessor_root: Path,
    target: Path,
    *,
    expected_predecessor_digest: str,
    version: str,
    replacement_root: Path,
    replacement_paths: list[str],
) -> Path:
    """Capture named source bytes; never sign, admit, authorize, or execute them."""
    if not replacement_paths or len(replacement_paths) != len(set(replacement_paths)):
        raise ValueError("replacement paths must be explicit and unique")
    reader = SecureDirectory(replacement_root, create=False)
    captured = {
        _relative_path(name): reader.read_bytes_bounded(
            name, max_bytes=128 * 1024 * 1024
        )
        for name in replacement_paths
    }
    return build_host_extension_successor(
        target,
        predecessor_root=predecessor_root,
        expected_predecessor_digest=expected_predecessor_digest,
        version=version,
        replacements=captured,
    )


def main() -> None:
    """Produce unsigned inputs for later normal publisher/Host verification."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--predecessor-root", required=True, type=Path)
    parser.add_argument("--expected-predecessor-digest", required=True)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--replacement-root", required=True, type=Path)
    parser.add_argument(
        "--replace", required=True, action="append", dest="replacement_paths"
    )
    args = parser.parse_args()
    result = stage_host_extension_successor(
        args.predecessor_root,
        args.target,
        expected_predecessor_digest=args.expected_predecessor_digest,
        version=args.version,
        replacement_root=args.replacement_root,
        replacement_paths=args.replacement_paths,
    )
    print(result)


if __name__ == "__main__":
    main()
