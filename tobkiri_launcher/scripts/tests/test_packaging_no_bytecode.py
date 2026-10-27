from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_resource_packaging_probe_cannot_pollute_source_with_bytecode(
    tmp_path: Path,
) -> None:
    checkout = tmp_path / "checkout"
    preparer = checkout / ".github/scripts/prepare_tauri_resources.py"
    cleanup = checkout / "tobkiri_runtime/scripts/packaging_cleanup.py"
    preparer.parent.mkdir(parents=True)
    cleanup.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / ".github/scripts/prepare_tauri_resources.py", preparer)
    shutil.copyfile(ROOT / "tobkiri_runtime/scripts/packaging_cleanup.py", cleanup)
    environment = os.environ.copy()
    environment.pop("PYTHONDONTWRITEBYTECODE", None)
    environment.pop("PYTHONPYCACHEPREFIX", None)

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy,sys; "
            "assert not sys.dont_write_bytecode; "
            f"runpy.run_path({os.fspath(preparer)!r}, run_name='packaging_probe')",
        ],
        check=False,
        cwd=checkout,
        env=environment,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    assert not list(checkout.rglob("__pycache__"))
    assert not list(checkout.rglob("*.py[co]"))
