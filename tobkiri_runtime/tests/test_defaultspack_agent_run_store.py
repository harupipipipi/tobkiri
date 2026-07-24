from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.agent.engine import AgentEngine  # noqa: E402
from domain.agent_runtime.models import AgentRun  # noqa: E402
from domain.agent_runtime.run_store import AgentRunStore, default_runtime_dir  # noqa: E402
from domain.agent_runtime.transcript import (  # noqa: E402
    TranscriptStore,
    default_transcript_dir,
)


@pytest.fixture(autouse=True)
def _isolate_agent_model_routing(monkeypatch):
    from domain.ai_client.model_router import ModelRoutingDecision

    monkeypatch.setattr(
        "domain.agent.engine.ModelRuntimeSettingsService.get_settings",
        lambda self: {
            "preferred_model": "stub/model",
            "preferred_model_group": "default",
            "auto_route_within_group": True,
        },
    )
    monkeypatch.setattr(
        "domain.agent.engine.get_model_capabilities",
        lambda model: {
            "profile_id": model,
            "supports_tool_calling": True,
            "supports_vision": True,
            "supports_image_input": True,
            "supports_thinking": True,
            "supports_fast": True,
        },
    )

    def fake_route(request):
        return ModelRoutingDecision(
            selected_model=request.preferred_model or "stub/model",
            original_model=request.preferred_model,
            selected_group=request.preferred_group or "default",
            reason_codes=["test_model_routing"],
            warnings=[],
        )

    monkeypatch.setattr("domain.agent.engine.route_model_request", fake_route)


def _tool(name: str) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "test tool",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def test_agent_runtime_defaults_to_launcher_user_data(tmp_path, monkeypatch):
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", raising=False)
    monkeypatch.delenv("RUMI_DEFAULTSPACK_AGENT_TRANSCRIPT_DIR", raising=False)
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user_data"))

    expected = tmp_path / "user_data" / "defaultspack" / "shared" / "agent_runtime"
    assert default_runtime_dir() == expected
    assert default_transcript_dir() == expected / "transcripts"


def test_agent_execution_persists_run_and_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    AgentRunStore._instance = None

    engine = AgentEngine()
    engine._ai_complete = lambda messages, model, context, tools=None: {
        "status": "ok",
        "data": {"content": "durable hello"},
    }

    result = engine.execute("say hi", [], "stub/model", None, {"agent_id": "agent"})

    assert result["status"] == "completed"
    stored = AgentRunStore().get_run(result["execution_id"])
    assert stored["task"] == "say hi"
    assert stored["status"] == "completed"
    transcript_id = stored["current_transcript_id"]
    assert TranscriptStore().read_tail(transcript_id, 10)


def test_agent_approval_can_resume_from_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_TRANSCRIPT_DIR", str(tmp_path / "transcripts"))
    AgentRunStore._instance = None

    calls = {"ai": 0}

    def fake_ai(self, messages, model, context, tools=None):
        calls["ai"] += 1
        if calls["ai"] == 1:
            return {
                "status": "ok",
                "data": {
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {"name": "search", "arguments": "{\"q\":\"rumi\"}"},
                        }
                    ]
                },
            }
        return {"status": "ok", "data": {"content": "used durable tool"}}

    def fake_execute_tool(self, tool_name, tool_args, context):
        return {"status": "ok", "data": {"result": "found"}}

    monkeypatch.setattr(AgentEngine, "_ai_complete", fake_ai)
    monkeypatch.setattr(AgentEngine, "_execute_tool", fake_execute_tool)

    engine = AgentEngine()
    started = engine.execute("find docs", [_tool("search")], "stub/model", None, {"agent_id": "agent"})
    assert started["status"] == "waiting_approval"

    import blocks.agent._state as state

    state._engines.clear()
    resumed_engine = state.get_engine(started["execution_id"])
    approved = resumed_engine.approve(started["execution_id"])

    assert approved["status"] == "completed"
    assert approved["result"]["result"] == "used durable tool"
    assert AgentRunStore().get_run(started["execution_id"])["status"] == "completed"


def test_agent_run_store_supports_parallel_thread_access(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    AgentRunStore._instance = None
    store = AgentRunStore()

    def write_and_read(index: int):
        run = AgentRun(
            run_id=f"run_{index}",
            session_key="agent:thread:main",
            task=f"task {index}",
            status="completed",
        )
        store.upsert_run(run)
        return store.get_run(run.run_id)["task"]

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(executor.map(write_and_read, range(24)))

    assert results == [f"task {index}" for index in range(24)]


def test_agent_run_store_redacts_tool_arguments_before_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("RUMI_DEFAULTSPACK_AGENT_RUNTIME_DIR", str(tmp_path / "agent_runtime"))
    AgentRunStore._instance = None
    store = AgentRunStore()

    store.record_tool_call("run_secret", "call_secret", "secret_tool", {"api_key": "sk-live"}, status="running")
    row = store.conn.execute(
        "SELECT arguments_json FROM agent_tool_calls WHERE tool_call_id = ?",
        ("call_secret",),
    ).fetchone()

    assert "sk-live" not in row["arguments_json"]
    assert "[REDACTED]" in row["arguments_json"]


def test_multi_agent_session_records_isolated_workspace_contracts(tmp_path, monkeypatch):
    from domain.agent.multi import MultiAgentOrchestrator
    from domain.coding.workspace_store import WorkspaceStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))
    WorkspaceStore().create(tmp_path, workspace_id="multi", trusted=True)

    orchestrator = MultiAgentOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_ai_complete",
        lambda messages, model, tools: {"status": "ok", "data": {"content": "[DONE] ok"}},
    )

    result = orchestrator.execute(
        "coordinate",
        [
            {"agent_id": "coder", "name": "coder", "role": "code", "model": "stub/default"},
            {"agent_id": "reviewer", "name": "reviewer", "role": "review", "model": "stub/default"},
        ],
        max_turns=1,
        workspace_id="multi",
        worktree_mode="metadata_only",
    )

    contexts = result["result"]["agent_contexts"]
    coder_workspace = contexts["coder"]["workspace"]
    reviewer_workspace = contexts["reviewer"]["workspace"]

    assert coder_workspace["contract_version"] == "rumi.agent_workspace.v1"
    assert coder_workspace["mode"] == "isolated_workspace"
    assert coder_workspace["workspace_root"] != reviewer_workspace["workspace_root"]
    assert Path(coder_workspace["workspace_root"]).is_dir()
    assert Path(reviewer_workspace["workspace_root"]).is_dir()
    assert result["result"]["shared_context"]["workspace"]["base_workspace_root"] == str(tmp_path.resolve())
    assert result["result"]["shared_context"]["workspace"]["workspace_id"] == "multi"


def test_multi_agent_workspace_copy_skips_symlink_targets(tmp_path, monkeypatch):
    from domain.agent.multi import MultiAgentOrchestrator
    from domain.coding.workspace_store import WorkspaceStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))

    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret-data", encoding="utf-8")
    link = tmp_path / "leak.txt"
    try:
        link.symlink_to(outside)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"symlink creation is unavailable on this platform: {exc}")
    (tmp_path / "real.txt").write_text("real-data", encoding="utf-8")
    WorkspaceStore().create(tmp_path, workspace_id="multi", trusted=True)

    orchestrator = MultiAgentOrchestrator()
    monkeypatch.setattr(
        orchestrator,
        "_ai_complete",
        lambda messages, model, tools: {"status": "ok", "data": {"content": "[DONE] ok"}},
    )

    result = orchestrator.execute(
        "coordinate",
        [{"agent_id": "coder", "name": "coder", "role": "code", "model": "stub/default"}],
        max_turns=1,
        workspace_id="multi",
        worktree_mode="copy",
    )

    workspace = result["result"]["agent_contexts"]["coder"]["workspace"]
    workspace_root = Path(workspace["workspace_root"])
    assert (workspace_root / "real.txt").read_text(encoding="utf-8") == "real-data"
    assert not (workspace_root / "leak.txt").exists()
    assert "leak.txt" not in workspace["base_manifest"]


def test_multi_agent_rejects_unregistered_workspace_root(tmp_path, monkeypatch):
    from domain.agent.multi import MultiAgentOrchestrator

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CODING_WORKSPACE_STORE_PATH", str(tmp_path / "workspaces.json"))

    result = MultiAgentOrchestrator().execute(
        "coordinate",
        [{"agent_id": "coder", "name": "coder", "role": "code", "model": "stub/default"}],
        max_turns=1,
        workspace_root=tmp_path,
        worktree_mode="copy",
    )

    assert result["status"] == "error"
    assert "registered trusted workspace required" in result["error"]
    assert not (tmp_path / ".rumi" / "multi_agent").exists()
