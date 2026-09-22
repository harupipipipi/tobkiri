"""Focused sealed-guest ABI coverage for the Shell Policy Pack."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, Mapping

import pytest

from ecosystem.rumi_shell_policy_pack.runtime import policy


@pytest.mark.parametrize(
    "path",
    (
        "~/notes.txt",
        "~root/notes.txt",
        "~unknown",
        "/etc/passwd",
        "//server/share/file",
        "C:/Users/notes.txt",
        r"C:\Users\notes.txt",
        r"\Windows\notes.txt",
        r"\\server\share\file",
    ),
)
def test_outside_paths_are_classified_without_host_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
    path: str,
) -> None:
    """POSIX and Windows paths require approval without a WASI user database."""

    def reject_lookup(value: str) -> str:
        raise AssertionError("pure classification must not resolve Host homes")

    monkeypatch.setattr(os.path, "expanduser", reject_lookup)
    result = policy.classify({"command": ["cat", path]})
    assert result["approval_required"] is True
    assert result["executed"] is False
    assert "outside_workspace_path" in result["risk_reasons"]


def test_packvm_shell_policy_uses_exact_catalog_and_service_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The guest admits one catalog target and one pure service action."""

    calls: list[tuple[object, str, Mapping[str, Any]]] = []

    def factory(client: object) -> Any:
        def operation(name: str, payload: Mapping[str, Any]) -> dict[str, Any]:
            calls.append((client, name, payload))
            return {"classification": "low", "executed": False}

        return operation

    monkeypatch.setattr(policy, "create_shell_policy_operation", factory)
    payload = {"operation": "classify", "command": ["git", "status"]}

    result = policy.tobkiri_packvm_invoke(
        "rumi_shell_policy_pack.shell-inspect",
        payload,
    )

    assert result == {"classification": "low", "executed": False}
    assert calls == [(None, "classify", payload)]


@pytest.mark.parametrize(
    "operation_id, payload",
    (
        ("classify", {"operation": "classify"}),
        ("rumi_shell_policy_pack.shell-inspect", []),
        ("rumi_shell_policy_pack.shell-inspect", {}),
        ("rumi_shell_policy_pack.shell-inspect", {"operation": "tokenize"}),
        ("rumi_shell_policy_pack.shell-inspect", {"operation": "execute"}),
    ),
)
def test_packvm_shell_policy_fails_closed_for_noncanonical_requests(
    operation_id: object,
    payload: object,
) -> None:
    """Guest input cannot select a legacy action or a different operation."""

    with pytest.raises(ValueError):
        policy.tobkiri_packvm_invoke(operation_id, payload)


def test_staged_shell_policy_is_importable_without_repo_or_site_packages() -> None:
    """The PackVM implementation depends only on its source and stdlib."""

    source = Path(policy.__file__).resolve()
    script = """
import importlib.util
import json
import sys

spec = importlib.util.spec_from_file_location("staged_shell_policy", sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
result = module.tobkiri_packvm_invoke(
    "rumi_shell_policy_pack.shell-inspect",
    {"operation": "classify", "command": ["git", "status"]},
)
print(json.dumps(result, sort_keys=True, separators=(",", ":")))
"""
    process = subprocess.run(
        (sys.executable, "-I", "-S", "-c", script, str(source)),
        capture_output=True,
        check=False,
        text=True,
        timeout=10,
    )

    assert process.returncode == 0, process.stderr
    result = json.loads(process.stdout)
    assert result["classification"] == "low"
    assert result["executed"] is False
