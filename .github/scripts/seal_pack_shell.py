#!/usr/bin/env python3
"""Seal one prebuilt Pack Shell artifact for Launcher build-script verification."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from prepare_tauri_resources import seal_pack_shell_binary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[2],
        help="Repository root. Defaults to this script's repository.",
    )
    parser.add_argument("--target", required=True, help="Rust target triple.")
    parser.add_argument(
        "--profile",
        default="debug",
        help="Cargo profile containing the prebuilt Pack Shell. Defaults to debug.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    repo_root = args.repo_root.resolve()
    try:
        binary, _payload, _mode, digest_path = seal_pack_shell_binary(
            repo_root,
            args.target,
            profile=args.profile,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        print(f"Failed to seal Pack Shell: {exc}", file=sys.stderr)
        return 2

    try:
        binary_label = binary.relative_to(repo_root)
        digest_label = digest_path.relative_to(repo_root)
    except ValueError:
        binary_label = binary
        digest_label = digest_path
    print(f"Sealed {binary_label} with {digest_label}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
