#!/usr/bin/env python3
"""Generate or check the Launcher presentation catalog from Protocol v4."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

from presentation_catalog_v4 import (  # noqa: E402
    PresentationCatalogError,
    presentation_catalog_drift,
    write_presentation_catalog,
)


def main(argv: list[str] | None = None) -> int:
    """Generate the checked-in catalog or fail when it has drifted."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    output = args.output.resolve() if args.output else None
    if args.check:
        if presentation_catalog_drift(root, output):
            print(
                "presentation catalog drift detected; run "
                "python scripts/quality/generate_presentation_catalog.py",
                file=sys.stderr,
            )
            return 1
        print("presentation catalog is generated from checked-in defaultspack manifests")
        return 0
    try:
        target = write_presentation_catalog(root, output)
    except PresentationCatalogError as error:
        print(f"presentation catalog generation failed: {error}", file=sys.stderr)
        return 1
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
