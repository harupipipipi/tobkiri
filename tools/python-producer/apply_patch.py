"""Check pinned source diffs; optionally write changed files to a fresh review tree.

This never invokes Cargo, Python guest initialization, archive extraction or a
package installer. The output is a partial source review tree, not a compiler.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path, PurePosixPath

HERE = Path(__file__).resolve().parent
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)\n")


def safe_path(value: str) -> str:
    """Reject every path spelling outside the pinned relative source namespace."""
    path = PurePosixPath(value)
    if (path.is_absolute() or not path.parts or ".." in path.parts
            or value != path.as_posix() or "\\" in value):
        raise ValueError("unsafe source patch path")
    return value


def apply_diff(patch: str, originals: dict[str, bytes]) -> dict[str, bytes]:
    """Apply unified diffs with exact contexts and counts, without fuzz or files."""
    lines = patch.splitlines(keepends=True)
    outputs: dict[str, bytes] = {}
    index = 0
    while index < len(lines):
        if not lines[index].startswith("--- "):
            raise ValueError("unexpected patch header")
        old_path = lines[index][4:].rstrip("\n")
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ b/"):
            raise ValueError("missing new source path")
        path = safe_path(lines[index][6:].rstrip("\n"))
        index += 1
        if path in outputs:
            raise ValueError("duplicate source patch file")
        if old_path == "/dev/null":
            if path in originals:
                raise ValueError("new source file already exists")
            original = []
        elif old_path == f"a/{path}" and path in originals:
            original = originals[path].decode("utf-8").splitlines(keepends=True)
        else:
            raise ValueError("old source path differs")
        result: list[str] = []
        cursor = 0
        hunks = 0
        while index < len(lines) and not lines[index].startswith("--- "):
            match = HUNK.fullmatch(lines[index])
            if match is None:
                raise ValueError("invalid source hunk")
            old_start, old_count, new_start, new_count = match.groups()
            old_count = int(old_count) if old_count is not None else 1
            new_count = int(new_count) if new_count is not None else 1
            target = max(int(old_start) - 1, 0)
            if target < cursor or target > len(original):
                raise ValueError("overlapping/out-of-range source hunk")
            result.extend(original[cursor:target])
            cursor = target
            if max(int(new_start) - 1, 0) != len(result):
                raise ValueError("new hunk location differs")
            index += 1
            removed = added = 0
            while index < len(lines) and lines[index][:1] in (" ", "+", "-"):
                if lines[index].startswith("--- "):
                    break
                operation, value = lines[index][0], lines[index][1:]
                if operation in (" ", "-"):
                    if cursor >= len(original) or original[cursor] != value:
                        raise ValueError("source hunk context differs")
                    cursor += 1
                    removed += 1
                if operation in (" ", "+"):
                    result.append(value)
                    added += 1
                index += 1
            if (removed, added) != (old_count, new_count):
                raise ValueError("source hunk counts differ")
            hunks += 1
        if not hunks:
            raise ValueError("source file has no hunk")
        result.extend(original[cursor:])
        outputs[path] = "".join(result).encode("utf-8")
    return outputs


def check(source: Path) -> tuple[dict[str, bytes], dict]:
    """Verify every source, patch and resulting hash in the review manifest."""
    pins = json.loads((HERE / "pins.json").read_text())
    delta = json.loads((HERE / "source-delta.json").read_text())
    outputs: dict[str, bytes] = {}
    receipt = {"format": "tobkiri.python-source-patch-check/1", "source_only": True,
               "compiler_executed": False, "guest_executed": False, "files": {}}
    for producer in ("componentize-py", "cpython", "wasmtime-wasi"):
        originals = {}
        for path, pin in pins["sources"].items():
            label, relative = path.split("/", 1)
            if label != producer:
                continue
            source_path = source / path
            if source_path.is_symlink() or not source_path.is_file():
                raise ValueError(f"original source must be an ordinary file: {path}")
            data = source_path.read_bytes()
            if len(data) != pin["bytes"] or hashlib.sha256(data).hexdigest() != pin["sha256"]:
                raise ValueError(f"original source pin differs: {path}")
            originals[relative] = data
        patch_path = HERE / "patches" / f"{producer}-0001.patch"
        patch = patch_path.read_bytes()
        expected = pins["patches"][patch_path.relative_to(HERE).as_posix()]
        if hashlib.sha256(patch).hexdigest() != expected["sha256"]:
            raise ValueError("versioned source patch digest differs")
        for relative, data in apply_diff(patch.decode(), originals).items():
            path = f"{producer}/{relative}"
            expected = delta[path]
            digest = hashlib.sha256(data).hexdigest()
            if len(data) != expected["after_bytes"] or digest != expected["after"]:
                raise ValueError(f"patched source digest differs: {path}")
            outputs[path] = data
            receipt["files"][path] = expected
    if set(outputs) != set(delta):
        raise ValueError("source delta file set differs")
    return outputs, receipt


def main() -> None:
    """Check only, or emit a fresh partial review tree and sanitized receipt."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    outputs, receipt = check(args.source.resolve(strict=True))
    if args.output is not None:
        output = args.output.absolute()
        if output.exists() or output.is_symlink():
            raise ValueError("output must be a fresh review directory; never patch in place")
        output.mkdir(parents=False)
        for path, data in outputs.items():
            destination = output / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
        (output / "source-check.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"source_only": True, "files_checked": len(outputs),
                      "compiler_executed": False, "guest_executed": False}, sort_keys=True))


if __name__ == "__main__":
    main()
