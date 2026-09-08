"""Defaultspack presentation packaging is standalone and grants no execution."""

import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts.generate_application_presentation import OUTPUT, render

OPERATION = "defaultspack.presentation.read"


def _module():
    spec = importlib.util.spec_from_file_location("presentation_fixture", OUTPUT)
    module = importlib.util.module_from_spec(spec)
    # Match the PackVM runner, which does not add the entrypoint to sys.modules.
    spec.loader.exec_module(module)
    return module


def test_generated_artifact_is_current() -> None:
    assert OUTPUT.read_text(encoding="utf-8") == render()


def test_isolated_single_file_needs_no_host_or_other_pack_files(tmp_path: Path) -> None:
    staged = tmp_path / "presentation.py"
    staged.write_bytes(OUTPUT.read_bytes())
    script = """
import importlib.util,json,sys
spec = importlib.util.spec_from_file_location('isolated_presentation', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module.tobkiri_packvm_invoke(sys.argv[2], {'kind':'ui', 'model_options':[{'value':'selected','label':'Selected'}]})))
"""
    result = subprocess.run(
        [sys.executable, "-B", "-I", "-S", "-c", script, str(staged), OPERATION],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
        timeout=20,
    )
    value = json.loads(result.stdout)
    assert value["sections"]
    assert value["definitions"]["shell"]
    assert "selected" in result.stdout
    assert list(tmp_path.iterdir()) == [staged]


def test_commands_are_presentation_only_and_results_are_detached() -> None:
    module = _module()
    result = module.tobkiri_packvm_invoke(OPERATION, {"kind": "commands"})
    assert len(result["commands"]) == 55
    assert all(command["availability"]["status"] == "unavailable" for command in result["commands"])
    expected = json.loads(json.dumps(result))
    result["commands"][0]["availability"]["status"] = "available"
    assert module.tobkiri_packvm_invoke(OPERATION, {"kind": "commands"}) == expected


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"kind": "execute"},
        {"kind": "ui", "approved": True},
        {"kind": "ui", "user_data_root": "/tmp"},
        {
            "kind": "ui",
            "model_options": [{"value": "model", "label": "Model", "api_key": "secret"}],
        },
        {"kind": "ui", "model_options": [{"value": True, "label": "Model"}]},
        {"kind": "ui", "model_options": [{}] * 257},
        {"kind": "commands", "model_options": []},
        {"kind": "ui", "_session_id": "x" * 65536},
    ],
)
def test_authority_paths_and_unbounded_display_data_are_rejected(payload: dict) -> None:
    with pytest.raises(ValueError):
        _module().tobkiri_packvm_invoke(OPERATION, payload)


def test_arbitrary_operation_is_rejected() -> None:
    with pytest.raises(ValueError):
        _module().tobkiri_packvm_invoke("defaultspack.execute", {"kind": "commands"})
