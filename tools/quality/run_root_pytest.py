"""Run current and frozen-source root tests without mixing their imports."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from stage_1520_preserved_source import HISTORICAL_TESTS, ROOT, stage


def run(*, verbose: bool = False, collect_only: bool = False) -> int:
    """Run both disjoint suites; empty collection in either is a failure."""
    if len(set(HISTORICAL_TESTS)) != len(HISTORICAL_TESTS) or any(
        not (ROOT / path).is_file() for path in HISTORICAL_TESTS
    ):
        raise ValueError("missing or duplicated historical root test")
    command = [sys.executable, "-B", "-m", "pytest"]
    if verbose:
        command.append("-v")
    if collect_only:
        command.extend(("--collect-only", "-q"))

    with tempfile.TemporaryDirectory(prefix="tobkiri-preserved-pytest-") as directory:
        temporary = Path(directory).resolve(strict=True)
        preserved = temporary / "source"
        stage(preserved, root_tests=True)
        if any(not (preserved / path).is_file() for path in HISTORICAL_TESTS):
            raise ValueError("historical root test missing from stage")

        current = subprocess.run(
            [*command, *(f"--ignore={path}" for path in HISTORICAL_TESTS), "tests/"],
            cwd=ROOT,
            check=False,
        ).returncode
        historical = subprocess.run(
            [*command, *HISTORICAL_TESTS],
            cwd=preserved,
            env={**os.environ, "TMPDIR": str(temporary)},
            check=False,
        ).returncode
    result = current or historical
    return 128 - result if result < 0 else result


def main() -> int:
    """Expose only the CI invocation and its collection check."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--collect-only", action="store_true")
    args = parser.parse_args()
    return run(verbose=args.verbose, collect_only=args.collect_only)


if __name__ == "__main__":
    sys.exit(main())
