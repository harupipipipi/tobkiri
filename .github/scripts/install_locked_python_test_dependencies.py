#!/usr/bin/env python3
"""Install the locked runtime and test dependencies used by CI entrypoints."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOCKED_REQUIREMENT_PATHS = (
    "tobkiri_runtime/requirements.txt",
    "tobkiri_runtime/requirements-dev.txt",
)
LOCKED_REQUIREMENTS = tuple(
    REPOSITORY_ROOT / relative_path for relative_path in LOCKED_REQUIREMENT_PATHS
)


def main() -> int:
    """Install the canonical locked runtime and development exports."""
    missing = [path for path in LOCKED_REQUIREMENTS if not path.is_file()]
    if missing:
        missing_paths = ", ".join(str(path) for path in missing)
        raise SystemExit(f"locked dependency export is missing: {missing_paths}")

    command = [sys.executable, "-m", "pip", "install"]
    command.append("--no-compile")
    for requirements in LOCKED_REQUIREMENTS:
        command.extend(("-r", str(requirements)))
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    subprocess.run(command, check=True, cwd=REPOSITORY_ROOT, env=environment)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
