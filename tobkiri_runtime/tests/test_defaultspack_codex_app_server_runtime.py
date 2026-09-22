from __future__ import annotations

from pathlib import Path
import sys
from typing import Any

import pytest


DEFAULTSPACK_ROOT = Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK_ROOT) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK_ROOT))


class FakeClient:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.turn_status: dict[str, dict[str, Any]] = {}
        self.account = {"type": "chatgpt"}
        self.usage: dict[str, Any] = {}
        self.mcp_status: dict[str, dict[str, Any]] = {}
        self.resumed: list[str] = []
        self.started_roots: list[str] = []
        self.closed = False

    def list_models(self) -> list[dict[str, Any]]:
        return [{"id": "codex-test", "display_name": "Codex Test", "is_default": True}]

    def start_thread(self, root: str):
        from blocks.coding.codex_app_server import CodingSession

        self.started_roots.append(root)
        return CodingSession("session-new", root, thread_id="thread-new")

    def resume_thread(self, thread_id: str, root: str):
        from blocks.coding.codex_app_server import CodingSession

        self.resumed.append(thread_id)
        return CodingSession("session-resumed", root, thread_id=thread_id)

    def read_thread(self, thread_id: str, *, include_turns: bool):
        assert include_turns is True
        return {"thread": {"id": thread_id, "turns": []}}

    def read_account(self):
        return self.account

    def read_usage(self):
        return self.usage

    def start_turn(self, session, text: str, *, model=None, effort=None):
        assert session.thread_id
        assert text == "inspect changes"
        assert model == "codex-test"
        self.turn_status["turn-1"] = {"id": "turn-1", "status": "inProgress"}
        return {"turn": {"id": "turn-1"}}

    def interrupt_turn(self, session, turn_id: str, *, wait_for_terminal: bool):
        assert session.thread_id
        assert turn_id == "turn-1"
        assert wait_for_terminal is False
        return {"accepted": True}

    def poll(self, timeout: float):
        from blocks.coding.codex_app_server import RequestTimeout

        raise RequestTimeout(f"no event after {timeout}")

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    def __init__(self, client: FakeClient, calls: list[dict[str, Any]]) -> None:
        self.client = client
        self.calls = calls

    def connect(self, **kwargs: Any) -> FakeClient:
        self.calls.append(kwargs)
        return self.client


def _runtime(tmp_path: Path, *, trusted: bool = True, enabled: bool = True):
    from domain.coding.codex_app_server_runtime import CodexAppServerRuntime
    from domain.coding.workspace_resolver import WorkspaceResolver
    from domain.coding.workspace_store import WorkspaceStore

    root = tmp_path / "workspace"
    root.mkdir(exist_ok=True)
    store = WorkspaceStore(tmp_path / "workspaces.json")
    if store.get("workspace-1") is None:
        store.create(root, workspace_id="workspace-1", trusted=trusted)
    client = FakeClient()
    calls: list[dict[str, Any]] = []
    runtime = CodexAppServerRuntime(
        backend_factory=lambda: FakeBackend(client, calls),
        config_loader=lambda: {
            "enabled": enabled,
            "transport": "stdio" if enabled else "off",
            "required_mcp_servers": ["filesystem"],
            "approval_timeout_seconds": 30,
        },
        workspace_resolver=WorkspaceResolver(store),
        state_path=tmp_path / "sessions.json",
        background_polling=False,
    )
    return runtime, client, calls, root


def test_runtime_starts_turn_only_in_registered_trusted_workspace(tmp_path: Path) -> None:
    runtime, client, calls, root = _runtime(tmp_path)

    result = runtime.start_turn(
        "workspace-1", "inspect changes", model="codex-test", effort="high"
    )

    assert result["thread_id"] == "thread-new"
    assert result["active_turn_id"] == "turn-1"
    assert client.started_roots == [str(root.resolve())]
    assert calls[0]["cwd"] == str(root.resolve())
    assert calls[0]["command"][:2] == ["codex", "app-server"]
    assert calls[0]["required_mcp_servers"] == ("filesystem",)
    assert (tmp_path / "sessions.json").exists()


def test_runtime_resumes_durable_thread_after_process_restart(tmp_path: Path) -> None:
    runtime, _, _, _ = _runtime(tmp_path)
    first = runtime.status("workspace-1", drain_events=False)

    restarted, restarted_client, _, _ = _runtime(tmp_path)
    second = restarted.status("workspace-1", drain_events=False)

    assert first["thread_id"] == "thread-new"
    assert second["thread_id"] == "thread-new"
    assert restarted_client.resumed == ["thread-new"]
    assert restarted_client.started_roots == []


def test_runtime_rejects_untrusted_workspace_before_starting_backend(tmp_path: Path) -> None:
    from domain.coding.workspace_policy import WorkspaceTrustRequired

    runtime, _, calls, _ = _runtime(tmp_path, trusted=False)

    with pytest.raises(WorkspaceTrustRequired):
        runtime.status("workspace-1")

    assert calls == []


def test_runtime_explains_disabled_setup(tmp_path: Path) -> None:
    from domain.coding.codex_app_server_runtime import CodexRuntimeError

    runtime, _, calls, _ = _runtime(tmp_path, enabled=False)

    with pytest.raises(CodexRuntimeError, match="Settings > Coding Backends"):
        runtime.status("workspace-1")

    assert calls == []


def test_runtime_interrupts_active_turn(tmp_path: Path) -> None:
    runtime, _, _, _ = _runtime(tmp_path)
    runtime.start_turn("workspace-1", "inspect changes", model="codex-test")

    result = runtime.interrupt("workspace-1")

    assert result["interrupt"] == {"accepted": True}


def test_authority_waits_for_server_owned_approval(monkeypatch, tmp_path: Path) -> None:
    runtime, _, _, root = _runtime(tmp_path)
    statuses = iter([{"status": "pending"}, {"status": "approved"}])
    monkeypatch.setattr(
        "domain.coding.codex_app_server_runtime.approval.create_approval_request",
        lambda *args, **kwargs: {"request_id": "apr-test"},
    )
    monkeypatch.setattr(
        "domain.coding.codex_app_server_runtime.approval.get_approval_request",
        lambda request_id: next(statuses),
    )
    monkeypatch.setattr(
        "domain.coding.codex_app_server_runtime.record_approval", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr("domain.coding.codex_app_server_runtime.time.sleep", lambda value: None)

    authority = runtime._authority("workspace-1", str(root), 15)

    assert authority("terminal.exec", {"command": "git status"}) == {
        "decision": "accept"
    }
