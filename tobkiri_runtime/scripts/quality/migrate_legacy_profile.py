#!/usr/bin/env python3
"""Migrate a legacy profile into a review-only v4 JSON document."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

RUNTIME_ROOT = Path(__file__).resolve().parents[2]
if str(RUNTIME_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNTIME_ROOT))

from tobkiri_protocol.migration import load_and_migrate_legacy_profile  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    """Run one-way legacy profile migration without activating the result."""
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--root", type=Path, default=RUNTIME_ROOT.parent)
    args = parser.parse_args(argv)
    result = load_and_migrate_legacy_profile(
        args.source,
        repository_root=args.root.resolve(),
    )
    if args.output is not None and result.get("profile") is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result["profile"], ensure_ascii=False, indent=2, sort_keys=True)
            + "\n",
            encoding="utf-8",
        )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("status") != "blocked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
