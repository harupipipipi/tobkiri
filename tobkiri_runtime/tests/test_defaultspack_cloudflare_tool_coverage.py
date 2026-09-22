from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.tool.cloudflare_coverage import cloudflare_tool_record, cloudflare_tool_summary
from domain.tool.registry import ToolRegistry


def test_cloudflare_tool_coverage_marks_bridge_exec_supported() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "sandbox_exec",
            "category": "sandbox",
            "tags": ["sandbox", "agent_os"],
            "metadata": {"category": "sandbox"},
        },
        record={"tool_id": "sandbox_exec", "service_id": "terminal", "tags": ["sandbox"]},
    )

    assert record["compatible"] is True
    assert record["route"] == "cloudflare_sandbox_bridge"
    assert record["reason"] == "sandbox_bridge_supported"
    assert "sandbox.exec" in record["required_capabilities"]


def test_cloudflare_tool_coverage_marks_pc_local_tools_as_pc_bridge() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "desktop_input",
            "category": "desktop",
            "tags": ["desktop", "computer_use", "sandbox"],
        },
        record={"tool_id": "desktop_input", "service_id": "computer", "tags": ["desktop"]},
    )

    assert record["compatible"] is False
    assert record["route"] == "pc_bridge_required"
    assert record["reason"] == "pc_local_surface"
    assert record["runtime"] == "pc_bridge"


def test_cloudflare_tool_coverage_marks_connectors_as_external() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "github_search",
            "category": "connector",
            "tags": ["connector", "agent_os"],
        },
        record={"tool_id": "github_search", "service_id": "github", "tags": ["connector"]},
    )

    assert record["compatible"] is False
    assert record["route"] == "external_connector_or_pc_bridge"
    assert record["reason"] == "external_connector_required"


def test_cloudflare_tool_coverage_marks_host_workspace_as_pc_bridge() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "coding_file_read",
            "category": "filesystem",
            "tags": ["coding", "file", "read"],
        },
        record={"tool_id": "coding_file_read", "service_id": "coding", "tags": ["coding", "file"]},
    )

    assert record["compatible"] is False
    assert record["route"] == "pc_bridge_required"
    assert record["reason"] == "host_workspace_required"


def test_cloudflare_tool_coverage_marks_legacy_sandbox_handlers_as_pc_bridge() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "sandbox_terminal_exec",
            "category": "sandbox",
            "tags": ["sandbox", "coding", "terminal"],
        },
        record={"tool_id": "sandbox_terminal_exec", "service_id": "terminal", "tags": ["sandbox", "coding"]},
    )

    assert record["compatible"] is False
    assert record["route"] == "pc_bridge_required"
    assert record["reason"] == "local_sandbox_workspace_required"


def test_cloudflare_tool_coverage_marks_port_expose_as_preview_gap() -> None:
    record = cloudflare_tool_record(
        {
            "tool_id": "sandbox_port_expose",
            "category": "sandbox",
            "tags": ["sandbox", "port", "network"],
        },
        record={"tool_id": "sandbox_port_expose", "service_id": "other", "tags": ["sandbox", "port"]},
    )

    assert record["compatible"] is False
    assert record["route"] == "cloudflare_preview_not_enabled"
    assert record["reason"] == "preview_url_domain_required"
    assert "sandbox.port_expose" in record["required_capabilities"]


def test_cloudflare_tool_coverage_summary_never_claims_all_tools_native() -> None:
    records = [
        cloudflare_tool_record(
            {"tool_id": "sandbox_exec", "category": "sandbox", "tags": ["sandbox"]},
            record={"tool_id": "sandbox_exec", "service_id": "terminal", "tags": ["sandbox"]},
        ),
        cloudflare_tool_record(
            {"tool_id": "browser_save_page", "category": "browser", "tags": ["browser"]},
            record={"tool_id": "browser_save_page", "service_id": "browser", "tags": ["browser"]},
        ),
    ]

    summary = cloudflare_tool_summary(records)

    assert summary["count"] == 2
    assert summary["supported_count"] == 1
    assert summary["unsupported_count"] == 1
    assert summary["all_tools_cloudflare_native"] is False
    assert summary["pc_bridge_required"] is True


def test_real_tool_registry_has_cloudflare_coverage_for_every_tool(
    defaultspack_component_catalog_selected,
) -> None:
    tools = ToolRegistry().list_tools()
    records = [cloudflare_tool_record(tool) for tool in tools]
    by_id = {str(tool.get("tool_id") or tool.get("name")): record for tool, record in zip(tools, records)}
    summary = cloudflare_tool_summary(records)

    assert len(records) == len(tools)
    assert by_id["sandbox_exec"]["compatible"] is True
    assert by_id["sandbox_files_read"]["compatible"] is True
    assert by_id["sandbox_files_apply_patch"]["compatible"] is True
    assert by_id["sandbox_terminal_exec"]["compatible"] is False
    assert by_id["sandbox_file_write"]["compatible"] is False
    assert by_id["sandbox_file_patch"]["compatible"] is False
    assert by_id["sandbox_file_read"]["compatible"] is False
    assert by_id["sandbox_terminal_exec"]["reason"] == "local_sandbox_workspace_required"
    assert by_id["desktop_input"]["reason"] == "pc_local_surface"
    assert by_id["browser_save_page"]["reason"] == "pc_local_surface"
    assert by_id["github_search"]["reason"] == "external_connector_required"
    assert summary["all_tools_cloudflare_native"] is False
    assert summary["supported_count"] < summary["count"]
    assert summary["pc_bridge_required"] is True


def test_tool_catalog_includes_cloudflare_summary_and_per_tool_records() -> None:
    from blocks.tool import catalog

    response = catalog.run({}, {})
    data = response["data"]

    assert data["cloudflare"]["schema"] == "rumi.cloudflare.tool_coverage.v1"
    assert data["cloudflare"]["all_tools_cloudflare_native"] is False
    assert data["count"] == len(data["tools"])
    assert all("cloudflare" in tool for tool in data["tools"])
    assert all("compatible" in tool["cloudflare"] for tool in data["tools"])
