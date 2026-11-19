"""Run root tests against their working checkout or frozen source inputs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile

from stage_1520_preserved_source import HISTORICAL_TESTS, ROOT, stage


def run(arguments: list[str]) -> int:
    """Keep historical imports and candidate imports in separate Python processes."""
    command = [sys.executable, "-B", "-m", "pytest", *arguments]
    current = subprocess.run(
        [*command, *(f"--ignore={path}" for path in HISTORICAL_TESTS), "tests/"],
        cwd=ROOT,
        check=False,
    ).returncode
    with tempfile.TemporaryDirectory(prefix="tobkiri-preserved-pytest-") as directory:
        temporary = Path(directory).resolve(strict=True)
        preserved = temporary / "source"
        stage(preserved, root_tests=True)
        historical = subprocess.run(
            [*command, "tests/"], cwd=preserved,
            env={**os.environ, "TMPDIR": str(temporary)}, check=False,
        ).returncode
    # Pytest returns 5 when a selector matches no tests. A selector can match
    # one checkout, but every actual failure must still fail this command.
    for result in (current, historical):
        if result not in (0, 5):
            return result if result > 0 else 128 - result
    return 0 if 0 in (current, historical) else 5


if __name__ == "__main__":
    sys.exit(run(sys.argv[1:]))
