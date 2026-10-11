"""The packaged Shell entrypoint must load before it configures import roots."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_defaultspack_entrypoint_imports_in_isolated_python(tmp_path: Path) -> None:
    entrypoint = (
        Path(__file__).resolve().parents[1]
        / "ecosystem" / "defaultspack" / "defaultspack" / "desktop_app.py"
    )
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1], run_name='import_probe')",
            str(entrypoint),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
