from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.command_protocol import CommandProtocolRegistry  # noqa: E402
from domain.frontend.invocation_events import InvocationEventStore  # noqa: E402
from domain.frontend.offline_queue import OfflineOperationQueue  # noqa: E402


def _workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(workspace), "config", "user.name", "Test"],
        check=True,
    )
    (workspace / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(workspace), "add", "seed.txt"], check=True)
    subprocess.run(["git", "-C", str(workspace), "commit", "-qm", "seed"], check=True)
    return workspace


def _protocol(tmp_path: Path) -> CommandProtocolRegistry:
    return CommandProtocolRegistry(
        DEFAULTSPACK_ROOT,
        event_store=InvocationEventStore(tmp_path / "events.sqlite3"),
        offline_queue=OfflineOperationQueue(tmp_path / "offline.sqlite3"),
    )


def test_catalog_exposes_high_risk_host_commands_to_the_signed_adapter(tmp_path):
    protocol = _protocol(tmp_path)
    high_risk = [
        command
        for command in protocol.catalog()["commands"]
        if command["authorization"]["approval_required"]
    ]

    assert {command["canonical_id"] for command in high_risk} == {
        "defaultspack:commit",
        "defaultspack:patch",
        "defaultspack:push",
        "defaultspack:restore",
        "defaultspack:terminal",
    }
    assert all(command["availability"] == {"status": "available"} for command in high_risk)


def test_generic_command_route_rejects_supplied_tokens_before_effect(
    tmp_path,
    monkeypatch,
):
    workspace = _workspace(tmp_path)
    protocol = _protocol(tmp_path)
    effects = []
    monkeypatch.setattr(
        protocol.operations,
        "invoke",
        lambda *args: effects.append(args) or {"status": "ok", "data": {}},
    )

    result = protocol.invoke(
        {
            "command_ref": "defaultspack:terminal",
            "args": {"cmd": "python -c 'print(1)'"},
            "conversation_id": "conversation-1",
            "invocation_id": "terminal-unavailable-1",
            "mode": "coding",
            "approval_token": "legacy-defaultspack-token",
            "authority_request_id": "legacy-authority-request",
            "authority_approval_token": "legacy-authority-token",
        },
        {
            "workspace_path": str(workspace),
            "authorized_workspace_roots": [str(workspace)],
            "_trusted_owner_key": "profile:work",
        },
    )

    assert result["status"] == "failed"
    assert result["error"]["code"] == "HIGH_RISK_COMMAND_ADAPTER_REQUIRED"
    assert "approval" not in result
    assert effects == []
