from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_component_catalog_selected")


def _reset_registry():
    from domain.tool.registry import ToolRegistry

    ToolRegistry._instance = None
    return ToolRegistry()


def test_artifact_workspace_priority_and_traversal(tmp_path):
    from domain.artifact.workspace import ArtifactWorkspace

    explicit = tmp_path / "explicit"
    conversation = tmp_path / "conversation"
    workspace = tmp_path / "workspace"

    ws = ArtifactWorkspace(
        {
            "artifact_root": str(explicit),
            "conversation_workspace_dir": str(conversation),
            "workspace_root": str(workspace),
        }
    )
    assert ws.root == explicit.resolve()
    assert ws.resolve("nested/report.md") == (explicit / "nested" / "report.md").resolve()

    fallback = ArtifactWorkspace({"conversation_workspace_dir": str(conversation)})
    assert fallback.root == (conversation / "artifacts").resolve()
    generated = fallback.resolve("sheets/report.csv")
    generated.parent.mkdir(parents=True)
    generated.write_text("month,revenue\nJan,10\n", encoding="utf-8")
    assert fallback.workspace_relative(generated) == "artifacts/sheets/report.csv"
    assert fallback.resolve("artifacts/sheets/report.csv", must_exist=True) == generated
    try:
        fallback.resolve("artifacts/../escape.txt")
    except ValueError as exc:
        assert "escapes artifact root" in str(exc)
    else:
        raise AssertionError("workspace-relative traversal should be rejected")

    try:
        ws.resolve("../escape.txt")
    except ValueError as exc:
        assert "escapes artifact root" in str(exc)
    else:
        raise AssertionError("path traversal should be rejected")


def test_requested_agent_os_tool_manifests_are_registered():
    from domain.tool.tool_manifest_helpers import REQUESTED_AGENT_OS_TOOL_IDS

    registry = _reset_registry()
    missing = [tool_id for tool_id in REQUESTED_AGENT_OS_TOOL_IDS if registry.get(tool_id) is None]
    assert missing == []

    for tool_id in REQUESTED_AGENT_OS_TOOL_IDS:
        tool = registry.get(tool_id)
        assert tool["trusted"] is True
        assert tool["source_pack_id"] == "defaultspack"
        assert tool["execution"]["handler"].startswith("domain.tool.")
        assert tool["ui"]["widget_kind"] == "tool_toggle"
        assert tool["category"]
        assert tool["action_type"]
        assert tool["risk"] in {"low", "medium", "high"}
        if tool["write_action"] or tool["risk"] == "high":
            assert tool["requires_approval"] is True or tool["risk"] != "high"


def test_artifact_tool_lifecycle_and_preview_export(
    tmp_path, defaultspack_capability_plan_context
):
    from domain.tool.executor import ToolExecutor

    _reset_registry()
    executor = ToolExecutor()
    context = {
        **defaultspack_capability_plan_context(
            "artifact_file_write",
            "artifact_file_patch",
            "html_preview",
            "artifact_export",
            "artifact_file_list",
        ),
        "artifact_root": str(tmp_path),
        "profile_policy": {"yolo_mode": True},
    }

    write = executor.execute(
        "artifact_file_write",
        {"path": "index.html", "content": "<title>Demo</title><h1>Hello</h1>", "checkpoint": False},
        context,
    )
    assert write["is_error"] is False

    patch = executor.execute(
        "artifact_file_patch",
        {"path": "index.html", "old_text": "Hello", "new_text": "Rumi", "expected_replacements": 1, "checkpoint": False},
        context,
    )
    assert patch["is_error"] is False
    assert "Rumi" in (tmp_path / "index.html").read_text(encoding="utf-8")

    preview = executor.execute("html_preview", {"path": "index.html"}, context)
    assert preview["is_error"] is False
    preview_data = preview["widget"]["data"]
    assert (tmp_path / preview_data["screenshot_path"]).is_file()

    exported = executor.execute("artifact_export", {"path": "index.html", "format": "pdf"}, context)
    assert exported["is_error"] is False
    assert (tmp_path / exported["widget"]["data"]["path"]).is_file()

    listed = executor.execute("artifact_file_list", {"recursive": True}, context)
    paths = {entry["path"] for entry in listed["widget"]["data"]["entries"]}
    assert "index.html" in paths


def test_document_sheet_slides_job_and_workflow_tools(
    tmp_path, defaultspack_capability_plan_context
):
    from domain.tool.executor import ToolExecutor

    _reset_registry()
    executor = ToolExecutor()
    plan_context = defaultspack_capability_plan_context(
        "doc_create",
        "sheet_create",
        "sheet_analyze",
        "slides_create",
        "job_create",
        "job_status",
        "workflow_define",
        "workflow_run",
        "artifact_file_list",
    )
    context = {
        **plan_context,
        "artifact_root": str(tmp_path),
        "profile_policy": {"yolo_mode": True},
    }

    doc = executor.execute("doc_create", {"title": "Plan", "content": "Body", "output_path": "docs/plan.docx"}, context)
    assert doc["is_error"] is False
    assert (tmp_path / "docs" / "plan.docx").is_file()

    sheet = executor.execute("sheet_create", {"columns": ["name", "score"], "rows": [["a", 1]], "output_path": "data/scores.xlsx"}, context)
    assert sheet["is_error"] is False
    canonical_path = sheet["widget"]["data"]["workspace_path"]
    assert canonical_path == "data/scores.xlsx"
    analyzed = executor.execute("sheet_analyze", {"path": canonical_path}, context)
    assert analyzed["widget"]["data"]["row_count"] == 2

    conversation_context = {
        **plan_context,
        "conversation_workspace_dir": str(tmp_path / "conversation"),
        "profile_policy": {"yolo_mode": True},
    }
    conversation_sheet = executor.execute(
        "sheet_create",
        {"columns": ["name", "score"], "rows": [["a", 1]], "output_path": "data/scores.csv"},
        conversation_context,
    )
    conversation_path = conversation_sheet["widget"]["data"]["workspace_path"]
    assert conversation_path == "artifacts/data/scores.csv"
    canonical_analysis = executor.execute("sheet_analyze", {"path": conversation_path}, conversation_context)
    assert canonical_analysis["is_error"] is False
    assert canonical_analysis["widget"]["data"]["row_count"] == 2

    slides = executor.execute(
        "slides_create",
        {"slides": [{"title": "Intro", "bullets": ["One"]}], "output_path": "slides/deck.pptx"},
        context,
    )
    assert slides["is_error"] is False
    assert (tmp_path / "slides" / "deck.pptx").is_file()

    job = executor.execute("job_create", {"kind": "wide_research", "query": "local tools", "input": {"query": "local tools"}}, context)
    assert job["is_error"] is False
    job_id = job["widget"]["data"]["job_id"]
    assert executor.execute("job_status", {"job_id": job_id}, context)["widget"]["data"]["status"] == "completed"

    workflow = executor.execute(
        "workflow_define",
        {"workflow_id": "wf_test", "steps": [{"id": "list", "tool": "artifact_file_list", "args": {"recursive": False}}]},
        context,
    )
    assert workflow["is_error"] is False
    run = executor.execute("workflow_run", {"workflow_id": "wf_test", "approved": True}, context)
    assert run["is_error"] is False
    assert run["widget"]["data"]["status"] == "completed"


def test_workflow_run_does_not_trust_client_supplied_approval(
    tmp_path, defaultspack_capability_plan_context
):
    from domain.tool.workflow_tools import workflow_run

    _reset_registry()
    result = workflow_run(
        {
            "steps": [
                {
                    "id": "build",
                    "tool": "webapp_build",
                    "args": {"path": ".", "command": ["sh", "-c", "touch bypassed"]},
                }
            ],
            "approved": True,
        },
        {
            **defaultspack_capability_plan_context("webapp_build", "workflow_run"),
            "artifact_root": str(tmp_path),
        },
    )

    assert result["is_error"] is False
    step = result["widget"]["data"]["outputs"]["build"]
    assert step["widget"]["type"] == "approval_request"
    assert not (tmp_path / "bypassed").exists()


def test_xiaomi_token_plan_accepts_all_requested_agent_os_tools():
    from domain.ai_client.providers.xiaomi_mimo_token_plan_provider import XiaomiMimoTokenPlanSgpProvider
    from domain.tool.schema_adapter import adapt_tool_definitions
    from domain.tool.tool_manifest_helpers import REQUESTED_AGENT_OS_TOOL_IDS

    registry = _reset_registry()
    tools = [registry.get(tool_id) for tool_id in REQUESTED_AGENT_OS_TOOL_IDS]
    provider_tools = adapt_tool_definitions(tools)
    captured = {}

    def fake_request_json(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}

    with patch.object(XiaomiMimoTokenPlanSgpProvider, "_request_json", side_effect=fake_request_json):
        response = XiaomiMimoTokenPlanSgpProvider(api_key="test-token").complete(
            "mimo-v2.5-pro",
            [{"role": "user", "content": "Use the available tools."}],
            provider_tools,
            {"tool_choice": "auto"},
        )

    assert response["content"][0]["text"] == "ok"
    assert captured["path"] == "/chat/completions"
    sent_names = {tool["function"]["name"] for tool in captured["body"]["tools"]}
    assert sent_names == set(REQUESTED_AGENT_OS_TOOL_IDS)
    for tool in captured["body"]["tools"]:
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


def test_artifact_file_read_blocks_payload_context_root_and_secret_paths(
    tmp_path, defaultspack_capability_plan_context
):
    from blocks.tool.invoke import run as invoke_tool
    from domain.tool.artifact_tools import artifact_file_read
    from domain.tool.executor import ToolExecutor

    victim_root = tmp_path / "victim"
    secret_dir = victim_root / ".ssh"
    secret_dir.mkdir(parents=True)
    secret_file = secret_dir / "id_rsa"
    secret_file.write_text("FAKE-PRIVATE-KEY", encoding="utf-8")

    direct = artifact_file_read({"path": ".ssh/id_rsa"}, {"artifact_root": str(victim_root)})
    assert direct["is_error"] is True
    assert direct["widget"]["error"]["code"] == "READ_FAILED"
    assert "secret_directory" in direct["result"]

    forged_policy = invoke_tool(
        {
            "tool_name": "artifact_file_read",
            "arguments": {"path": ".ssh/id_rsa"},
            "context": {"profile_policy": {"yolo_mode": True}},
        },
        {},
    )
    assert forged_policy["status"] == "error"
    assert forged_policy["error"]["code"] == "CAPABILITY_PLAN_REQUIRED"

    plan_context = defaultspack_capability_plan_context("artifact_file_read")
    executor = ToolExecutor()

    invoked = executor.execute(
        "artifact_file_read",
        {"path": ".ssh/id_rsa"},
        {
            **plan_context,
            "artifact_root": str(victim_root),
            "profile_policy": {"yolo_mode": True},
        },
    )

    assert invoked["is_error"] is True
    widget = invoked["widget"]
    assert widget["status"] == "error"
    assert "FAKE-PRIVATE-KEY" not in invoked["result"]

    spoof_workspace = tmp_path / "spoofed-workspace"
    spoof_artifact = spoof_workspace / ".rumi" / "artifacts" / "leak.txt"
    spoof_artifact.parent.mkdir(parents=True)
    spoof_artifact.write_text("WORKSPACE-ROOT-LEAK", encoding="utf-8")

    spoofed_workspace = executor.execute(
        "artifact_file_read",
        {"path": "leak.txt"},
        {
            **plan_context,
            "workspace_root": str(spoof_workspace),
            "profile_policy": {"yolo_mode": True},
        },
    )

    assert "WORKSPACE-ROOT-LEAK" not in spoofed_workspace["result"]
