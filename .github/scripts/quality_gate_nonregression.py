#!/usr/bin/env python3
"""Compare static-analysis debt between a base ref and the current checkout.

This is intentionally a non-regression gate. The package still has historical
Ruff and mypy debt, so CI fails when a PR increases the checked debt count
instead of requiring a whole-repository cleanup in an unrelated change.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def _run(command: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )


def _ruff_count(package_dir: Path, targets: list[str]) -> tuple[int, str]:
    result = _run(
        [sys.executable, "-m", "ruff", "check", *targets, "--output-format=json"],
        cwd=package_dir,
    )
    try:
        diagnostics = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"failed to parse Ruff JSON output:\n{result.stdout}") from exc
    return len(diagnostics), result.stdout


def _mypy_count(package_dir: Path, targets: list[str]) -> tuple[int, str]:
    result = _run(
        [sys.executable, "-m", "mypy", "--check-untyped-defs", *targets],
        cwd=package_dir,
    )
    output = result.stdout
    if result.returncode == 0:
        return 0, output
    match = re.search(r"Found (\d+) errors? in ", output)
    if match:
        return int(match.group(1)), output
    fallback = sum(1 for line in output.splitlines() if ": error:" in line)
    if fallback:
        return fallback, output
    raise RuntimeError(f"failed to parse mypy output:\n{output}")


def _add_base_worktree(repo_root: Path, base_ref: str) -> Path:
    temp_root = Path(tempfile.mkdtemp(prefix="rumi-static-base-"))
    base_worktree = temp_root / "base"
    result = _run(["git", "worktree", "add", "--detach", str(base_worktree), base_ref], cwd=repo_root)
    if result.returncode != 0:
        shutil.rmtree(temp_root, ignore_errors=True)
        raise RuntimeError(f"failed to create base worktree for {base_ref}:\n{result.stdout}")
    return base_worktree


def _remove_base_worktree(path: Path) -> None:
    repo_root = Path.cwd()
    _run(["git", "worktree", "remove", "--force", str(path)], cwd=repo_root)
    shutil.rmtree(path.parent, ignore_errors=True)


def _resolve_base_package_dir(base_worktree: Path, package_dir: str) -> Path:
    requested = base_worktree / package_dir
    if requested.is_dir():
        return requested
    legacy_names = {
        "tobkiri_runtime": "rumi_ai_1_10",
    }
    legacy_name = legacy_names.get(package_dir)
    if legacy_name:
        legacy = base_worktree / legacy_name
        if legacy.is_dir():
            return legacy
    raise RuntimeError(
        f"package directory {package_dir!r} is absent from the base ref"
    )


def _check_non_regression(name: str, base_count: int, head_count: int) -> bool:
    print(f"{name}: base={base_count} head={head_count}")
    if head_count > base_count:
        print(f"{name}: static-analysis debt regressed by {head_count - base_count}", file=sys.stderr)
        return False
    return True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-ref", default="origin/master")
    parser.add_argument("--package-dir", default="tobkiri_runtime")
    parser.add_argument("--ruff-target", action="append", default=[])
    parser.add_argument("--mypy-target", action="append", default=[])
    args = parser.parse_args()

    repo_root = Path.cwd()
    package_dir = repo_root / args.package_dir
    ruff_targets = args.ruff_target or ["core_runtime", "app.py"]
    mypy_targets = args.mypy_target or ["core_runtime", "app.py"]

    base_worktree = _add_base_worktree(repo_root, args.base_ref)
    try:
        base_package_dir = _resolve_base_package_dir(base_worktree, args.package_dir)
        base_ruff, _ = _ruff_count(base_package_dir, ruff_targets)
        head_ruff, _ = _ruff_count(package_dir, ruff_targets)
        base_mypy, _ = _mypy_count(base_package_dir, mypy_targets)
        head_mypy, _ = _mypy_count(package_dir, mypy_targets)
    finally:
        _remove_base_worktree(base_worktree)

    ok = True
    ok &= _check_non_regression("ruff", base_ruff, head_ruff)
    ok &= _check_non_regression("mypy", base_mypy, head_mypy)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
