#!/usr/bin/env python3
"""Check required local runtime dependency edges in Launcher's independent lock.

This catches missing edges without building locally. Cargo --locked remains
authoritative for resolution, optional features, versions and all platforms.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import tomllib


def missing_dependencies(root: Path, lock: dict | None = None) -> list[str]:
    """Compare non-optional runtime edges for local crates in the nested lock."""
    if lock is None:
        lock = tomllib.loads(
            (root / "tobkiri_launcher/src-tauri/Cargo.lock").read_text()
        )
    workspace = tomllib.loads((root / "Cargo.toml").read_text())["workspace"]
    local = {
        package["name"]: package
        for package in lock["package"]
        if "source" not in package
    }
    missing = []
    for path in sorted((root / "crates").glob("*/Cargo.toml")):
        manifest = tomllib.loads(path.read_text())
        name = manifest.get("package", {}).get("name")
        if name not in local:
            continue
        recorded = {
            dependency.split(" ", 1)[0]
            for dependency in local[name].get("dependencies", [])
        }
        tables = [manifest.get("dependencies", {})]
        tables.extend(
            target.get("dependencies", {})
            for target in manifest.get("target", {}).values()
        )
        for table in tables:
            for alias, specification in table.items():
                if isinstance(specification, str):
                    specification = {}
                if specification.get("optional", False):
                    continue
                if specification.get("workspace", False):
                    inherited = workspace["dependencies"][alias]
                    if isinstance(inherited, dict):
                        specification = inherited | specification
                dependency = specification.get("package", alias)
                if dependency not in recorded:
                    missing.append(f"{name} -> {dependency}")
    return sorted(set(missing))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2]
    )
    arguments = parser.parse_args()
    missing = missing_dependencies(arguments.root)
    if missing:
        raise SystemExit("Launcher runtime lock is missing: " + ", ".join(missing))
    print("Launcher runtime lock: all required local dependency edges present")


if __name__ == "__main__":
    main()
