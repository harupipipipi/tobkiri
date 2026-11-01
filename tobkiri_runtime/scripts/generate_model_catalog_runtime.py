"""Vendor the public filter contract into the sealed Catalog Pack, without drift."""

from __future__ import annotations

import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path("tobkiri_protocol/provider_compiler/native_filters.py")
DESTINATION = Path("ecosystem/rumi_model_catalog_pack/runtime/native_filters.py")


def generate(*, check: bool, root: Path = ROOT) -> Path:
    """Copy canonical bytes, or reject a missing, linked, or divergent companion."""
    source = root / SOURCE
    destination = root / DESTINATION
    if source.is_symlink() or not source.is_file():
        raise ValueError("public model filter contract is missing or linked")
    if destination.is_symlink():
        raise ValueError("sealed model filter contract must not be linked")
    expected = source.read_bytes()
    if check:
        if not destination.is_file() or destination.read_bytes() != expected:
            raise ValueError(
                "sealed model filter contract is stale; run "
                "scripts/generate_model_catalog_runtime.py before resealing"
            )
    else:
        destination.write_bytes(expected)
    return destination


def main() -> None:
    """Generate or check the exact Pack-local public compiler snapshot."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    print(generate(check=args.check))


if __name__ == "__main__":
    main()
