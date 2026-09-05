from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from tobkiri_protocol.validation import validate_document

ROOT = Path(__file__).resolve().parent.parent


def _command(request_id: str, command: str, **overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "protocol": "io.tobkiri.cli.io.v1",
        "type": "command",
        "request_id": request_id,
        "command": command,
        "arguments": {},
        "stdin": None,
        "tty": False,
        "output_limit": 1_048_576,
    }
    request.update(overrides)
    return request


def _run_cli(requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        item for item in [str(ROOT), environment.get("PYTHONPATH", "")] if item
    )
    completed = subprocess.run(
        [sys.executable, "-m", "tobkiri.cli_shell", "--structured-stdio"],
        cwd=ROOT,
        env=environment,
        input="".join(json.dumps(request) + "\n" for request in requests),
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    return [validate_document(line, "cli_io") for line in completed.stdout.splitlines()]


def test_actual_cli_shell_does_not_infer_profile_identity_from_bundled_definition() -> None:
    responses = _run_cli(
        [_command("cli:req:identity001", "profile.identity", profile_id="defaults")]
    )

    assert responses[0]["type"] == "error"
    assert "Host-captured active Profile v4" in responses[0]["error"]


def test_actual_cli_shell_rejects_arbitrary_commands_and_keeps_artifacts_non_executable() -> None:
    responses = _run_cli(
        [
            _command("cli:req:health001", "health"),
            _command("cli:req:arbitrary1", "cargo tauri dev"),
            _command(
                "cli:req:limit001",
                "echo",
                stdin="x" * 32,
                output_limit=4,
            ),
        ]
    )
    assert responses[0]["type"] == "result"
    assert responses[0]["exit_status"] == 0
    assert responses[1]["type"] == "error"
    assert "command" in responses[1]["error"]
    assert responses[2]["type"] == "error"
    assert "output" in responses[2]["error"]
