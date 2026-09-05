#!/usr/bin/env python3
"""Write the exact, post-build Shell v4 output contract consumed by packaging."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


VALID_TARGETS = {
    ("macos", "arm64"),
    ("macos", "x86_64"),
    ("windows", "x86_64"),
    ("linux", "x86_64"),
}
FULL_REVISION = re.compile(r"^[0-9a-f]{40}$")


def reject_symlink_components(path: Path) -> None:
    """Reject a build output reached through a symlinked parent directory."""
    current = Path(path.anchor) if path.is_absolute() else Path()
    parts = path.parts[1:] if path.is_absolute() else path.parts
    for part in parts:
        current /= part
        if current.is_symlink():
            raise SystemExit(f"build artifact path contains a symlink: {current}")


def main() -> int:
    """Validate explicit build facts and write a deterministic manifest."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact-id", required=True)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument(
        "--platform", required=True, choices=("macos", "linux", "windows")
    )
    parser.add_argument("--architecture", required=True, choices=("arm64", "x86_64"))
    parser.add_argument("--source-identity", required=True)
    parser.add_argument("--source-revision", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    artifact_input = args.artifact.expanduser()
    reject_symlink_components(artifact_input)
    artifact = artifact_input.resolve()
    if args.artifact.is_symlink() or not artifact.exists():
        raise SystemExit(f"build artifact is missing or symlinked: {args.artifact}")
    if (args.platform, args.architecture) not in VALID_TARGETS:
        raise SystemExit(
            "unsupported release target: "
            f"{args.platform}/{args.architecture}"
        )
    for name in ("artifact_id", "source_identity", "source_revision"):
        if not getattr(args, name).strip():
            raise SystemExit(f"{name} must not be empty")
    expected_suffix = f".{args.platform}-{args.architecture}"
    if not args.artifact_id.endswith(expected_suffix):
        raise SystemExit(
            "artifact_id must identify the exact platform/architecture: "
            f"expected suffix {expected_suffix!r}"
        )
    if FULL_REVISION.fullmatch(args.source_revision) is None:
        raise SystemExit("source_revision must be a full 40-character Git commit SHA")
    value = {
        "schema": "io.tobkiri.shell.build-output.v4",
        "artifact_id": args.artifact_id,
        "artifact_path": str(artifact),
        "platform": args.platform,
        "architecture": args.architecture,
        "build_profile": "release",
        "source_identity": args.source_identity,
        "source_revision": args.source_revision,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
