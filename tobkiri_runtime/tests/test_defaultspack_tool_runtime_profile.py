from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.tool.catalog import run as tool_catalog_run
from domain.tool.registry import ToolRegistry
from domain.tool.runtime_profile import tool_runtime_profile, tool_runtime_profile_summary
from domain.tool.service_catalog import ToolServiceCatalog


def _registry_tool(tool_id: str) -> dict:
    for tool in ToolRegistry().list_tools():
        if str(tool.get("tool_id") or tool.get("name") or "") == tool_id:
            return tool
    raise AssertionError(f"tool not found: {tool_id}")


def _profile_for_registered_tool(tool_id: str) -> dict:
    tool = _registry_tool(tool_id)
    record = ToolServiceCatalog.compact_record(tool)
    return tool_runtime_profile(tool, record=record)


def test_web_search_is_python_network_not_host_bound() -> None:
    profile = _profile_for_registered_tool("web_search")

    assert profile["kind"] == "python_network"
    assert "runtime:python" in profile["tags"]
    assert "cap:network" in profile["tags"]
    assert profile["host_bound"] is False


def test_function_web_search_manifest_keeps_subprocess_material() -> None:
    manifest_path = DEFAULTSPACK_ROOT / "functions" / "tool_web_search" / "manifest.json"
    tool = json.loads(manifest_path.read_text(encoding="utf-8"))

    profile = tool_runtime_profile(tool)

    assert profile["kind"] == "python_network"
    assert "runtime:subprocess" in profile["tags"]
    assert profile["requirements"] == ["network"]
    assert profile["host_bound"] is False


def test_browser_tools_are_marked_python_chrome_session() -> None:
    profile = _profile_for_registered_tool("browser_extract_table")

    assert profile["kind"] == "python_chrome"
    assert "runtime:chrome" in profile["tags"]
    assert "cap:browser_session" in profile["tags"]
    assert profile["host_bound"] is True


def test_computer_tools_are_marked_pc_computer() -> None:
    profile = _profile_for_registered_tool("desktop_input")

    assert profile["kind"] == "pc_computer"
    assert "runtime:computer" in profile["tags"]
    assert "cap:input" in profile["tags"]
    assert profile["host_bound"] is True


def test_sandbox_exec_tools_include_language_materials() -> None:
    python_profile = _profile_for_registered_tool("python_exec")
    node_profile = _profile_for_registered_tool("node_exec")
    shell_profile = _profile_for_registered_tool("sandbox_exec")

    assert python_profile["kind"] == "sandbox_python"
    assert "runtime:sandbox" in python_profile["tags"]
    assert "runtime:python" in python_profile["tags"]
    assert node_profile["kind"] == "sandbox_node"
    assert "runtime:node" in node_profile["tags"]
    assert shell_profile["kind"] == "sandbox_shell"
    assert "runtime:shell" in shell_profile["tags"]


def test_workspace_tools_are_marked_pc_workspace() -> None:
    profile = _profile_for_registered_tool("coding_file_read")

    assert profile["kind"] == "pc_workspace"
    assert "cap:workspace" in profile["tags"]
    assert profile["host_bound"] is True


def test_runtime_profile_summary_counts_materials() -> None:
    profiles = [
        _profile_for_registered_tool("web_search"),
        _profile_for_registered_tool("browser_extract_table"),
        _profile_for_registered_tool("python_exec"),
    ]

    summary = tool_runtime_profile_summary(profiles)

    assert summary["count"] == 3
    assert summary["by_kind"]["python_network"] == 1
    assert summary["by_kind"]["python_chrome"] == 1
    assert summary["by_kind"]["sandbox_python"] == 1
    assert summary["network_count"] == 1
    assert summary["browser_session_count"] == 1
    assert summary["sandbox_count"] == 1


def test_tool_catalog_includes_runtime_profiles() -> None:
    payload = tool_catalog_run({}, {})
    assert payload["status"] == "ok"

    data = payload["data"]
    by_id = {record["tool_id"]: record for record in data["tools"]}

    assert "runtime_profiles" in data
    assert data["runtime_profiles"]["count"] == data["count"]
    assert by_id["web_search"]["runtime_profile"]["kind"] == "python_network"
    assert by_id["desktop_input"]["runtime_profile"]["kind"] == "pc_computer"
