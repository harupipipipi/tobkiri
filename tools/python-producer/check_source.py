"""Check the exact source patch and syntax without compiling or copying a tree."""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess

from apply_patch import HERE, check


def digest(data: bytes) -> str:
    """Return the untagged SHA-256 used by the source pin schema."""
    return hashlib.sha256(data).hexdigest()


def snapshot() -> dict[str, dict]:
    """Capture ordinary preparation files, excluding generated Python caches."""
    return {
        path.relative_to(HERE).as_posix(): {
            "bytes": len(data), "sha256": digest(data)
        }
        for path in sorted(HERE.rglob("*"))
        if path.is_file() and not path.is_symlink()
        and "__pycache__" not in path.parts
        for data in [path.read_bytes()]
    }


def main() -> None:
    """Apply in memory and ask only syntax parsers to inspect changed sources."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--receipt", required=True, type=Path)
    args = parser.parse_args()
    if args.receipt.exists() or args.receipt.is_symlink():
        raise ValueError("receipt must be new; preserve historical checks")
    before = snapshot()
    outputs, patch_check = check(args.source.resolve(strict=True))
    rust = []
    python = []
    for path, data in sorted(outputs.items()):
        if path.endswith(".rs"):
            result = subprocess.run(
                ["rustfmt", "--edition", "2024", "--config",
                 "skip_children=true", "--emit", "stdout"],
                input=data, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode:
                raise ValueError(f"Rust syntax check failed: {path}: "
                                 f"{result.stderr.decode(errors='replace')}")
            rust.append(path)
        elif path.endswith(".py"):
            ast.parse(data, filename=path)
            python.append(path)
    # Parse the preparation tools too; none of their guest/staging paths run.
    for path in sorted(HERE.glob("*.py")):
        ast.parse(path.read_bytes(), filename=str(path))
        python.append("preparation/" + path.name)
    after = snapshot()
    if before != after:
        raise ValueError("preparation sources changed during source check")
    identity = digest(json.dumps(before, sort_keys=True,
                                 separators=(",", ":")).encode())
    receipt = {
        "format": "tobkiri.python-source-check/2", "source_only": True,
        "source_identity_sha256": identity, "before_equals_after": True,
        "snapshot": before, "patch_check": patch_check,
        "rust_syntax_only": rust, "python_ast_only": python,
        "compiler_executed": False, "cargo_executed": False,
        "guest_executed": False, "native_cpython_executed": False,
        "typecheck_executed": False,
        "limits": "syntax/application checks do not prove FS, RNG, lifecycle, staging, reproducibility or parity",
    }
    args.receipt.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    print(json.dumps({"files_checked": len(outputs),
                      "rust_syntax_only": len(rust),
                      "python_ast_only": len(python),
                      "source_identity_sha256": identity}, sort_keys=True))


if __name__ == "__main__":
    main()
