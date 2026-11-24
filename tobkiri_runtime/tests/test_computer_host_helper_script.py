from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "core_runtime" / "host_broker" / "computer_host_helper.py"


def test_computer_host_helper_script_bootstraps_project_imports_outside_repo(tmp_path):
    canary = "CANARY_PRIVATE_ARTIFACT_ROOT_9f4a"
    request = {
        "function_id": "computer.observe",
        "args": {},
        "artifact_root": str(tmp_path / canary / "rogue" / "computer"),
    }
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"

    completed = subprocess.run(
        [sys.executable, str(HELPER)],
        cwd=tmp_path,
        env=env,
        input=json.dumps(request),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert completed.returncode == 0
    response = json.loads(completed.stdout)
    assert response == {
        "ok": False,
        "error_code": "INVALID_ARTIFACT_ROOT",
        "error": "The artifact root is invalid.",
    }
    assert canary not in completed.stdout
    assert canary not in completed.stderr


def test_computer_host_helper_hides_runtime_exception_text(monkeypatch, capsys):
    from core_runtime.host_broker import computer_host_helper
    from ecosystem.rumi_default_tools_pack.domain.tool.browser_computer import (
        BrowserComputerController,
    )

    canary = "CANARY_PRIVATE_RUNTIME_ERROR_2c17"

    def fail_with_private_text(*args, **kwargs):
        raise RuntimeError(canary)

    monkeypatch.setenv("RUMI_COMPUTER_HOST_INTERNAL", "restore-after-test")
    monkeypatch.setattr(BrowserComputerController, "run", fail_with_private_text)
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"function_id": "computer.observe", "args": {}})),
    )

    assert computer_host_helper.main() == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {
        "ok": False,
        "error_code": "VIEWER_HOST_FAILED",
        "error": "Viewer host helper failed.",
    }
    assert canary not in captured.out
    assert canary not in captured.err
