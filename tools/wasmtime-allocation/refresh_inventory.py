#!/usr/bin/env python3
"""Hash the pinned source delta without modifying either upstream directory.

This is a source-review index, not an allocation or runtime verification tool.
Pass the two read-only, unpacked upstream crates as command-line arguments.
Their machine-local paths are deliberately omitted from public output.
"""

import argparse
import difflib
import hashlib
import json
from pathlib import Path


def digest(data: bytes) -> str:
    """Return a content identity for exact bytes."""
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    """Regenerate public hashes and a bounded, reproducible review set."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-wasmtime", required=True, type=Path)
    parser.add_argument("--upstream-fiber", required=True, type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[2]
    output = repo / "tools/wasmtime-allocation"
    specs = [
        (
            "wasmtime",
            args.upstream_wasmtime,
            "4cdaf5b8af5713146e3a62d940c71e3eb2ad1ebacfa6b55f0c8509a5b4b87718",
        ),
        (
            "wasmtime-internal-fiber",
            args.upstream_fiber,
            "4d126366176553e8f304e8ac2078a4cc37134fe8f19d4d4695a09c5a2f577f10",
        ),
    ]
    packages = []
    delta = []
    for name, upstream, archive in specs:
        vendor = repo / "vendor" / name
        modified = []
        for path in sorted(vendor.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(vendor)
            original = upstream / relative
            actual_bytes = path.read_bytes()
            original_bytes = original.read_bytes() if original.is_file() else None
            if original_bytes == actual_bytes:
                continue
            modified.append(
                {
                    "path": relative.as_posix(),
                    "upstream_sha256": (
                        digest(original_bytes) if original_bytes is not None else None
                    ),
                    "patched_sha256": digest(actual_bytes),
                }
            )
            # Newly added allocation.rs is provided in full in the review set.
            # Existing source changes include the entire delta with narrow context.
            if original_bytes is not None and path.suffix == ".rs":
                delta.extend(
                    difflib.unified_diff(
                        original_bytes.decode().splitlines(keepends=True),
                        actual_bytes.decode().splitlines(keepends=True),
                        fromfile=f"upstream/{name}/{relative.as_posix()}",
                        tofile=f"vendor/{name}/{relative.as_posix()}",
                        n=1,
                    )
                )
        packages.append(
            {
                "crate": name,
                "version": "47.0.4",
                "original_readonly_source": f"crates.io registry package {name}@47.0.4",
                "archive_sha256": archive,
                "license_file": f"vendor/{name}/LICENSE",
                "license_sha256": digest((vendor / "LICENSE").read_bytes()),
                "modified_files": modified,
            }
        )
    (output / "source-inventory.json").write_text(
        json.dumps(
            {
                "status": (
                    "LATEST ASYNC/FIBER/SHARED/RESOURCE SOURCE IS UNBUILT. "
                    "Prior successful wasmtime metadata receipt excludes these changes."
                ),
                "packages": packages,
            },
            indent=2,
        )
        + "\n"
    )
    (output / "upstream-delta.patch").write_text("".join(delta))
    paths = [
        "crates/tobkiri-canon/src/lib.rs",
        "crates/tobkiri-canon/src/ownership.rs",
        "crates/tobkiri-canon/tests/ownership.rs",
        "vendor/wasmtime/src/runtime/component/allocation.rs",
        "tools/wasmtime-allocation/upstream-delta.patch",
        "tools/wasmtime-allocation/README.md",
    ]
    items = []
    for relative in paths:
        data = (repo / relative).read_bytes()
        items.append({"path": relative, "bytes": len(data), "sha256": digest(data)})
    total = sum(item["bytes"] for item in items)
    result = {
        "scope": "Immutable source review, not runtime/allocation test proof",
        "paths": items,
        "total_bytes": total,
        "limit_bytes": 200 * 1024,
    }
    (output / "focused-review-set.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"total_bytes": total, "limit_bytes": 200 * 1024}))
    if total > 200 * 1024:
        raise SystemExit("review set exceeds the promised source-review size")


if __name__ == "__main__":
    main()
