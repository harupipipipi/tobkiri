from __future__ import annotations

import json
from pathlib import Path


DEFAULTSPACK_ROOT = Path(__file__).resolve().parent.parent / "ecosystem" / "defaultspack"


def _tool_parameters(tool_id: str) -> dict:
    manifest_path = DEFAULTSPACK_ROOT / "tools" / tool_id / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return manifest["config"]["schema"]["parameters"]


def test_desktop_operator_skill_applies_to_every_desktop_tool() -> None:
    manifest_path = DEFAULTSPACK_ROOT / "extensions" / "skills" / "desktop_operator" / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    desktop_tool_ids = {
        path.name
        for path in (DEFAULTSPACK_ROOT / "tools").iterdir()
        if path.is_dir() and path.name.startswith("desktop_")
    }

    assert desktop_tool_ids
    assert desktop_tool_ids <= set(manifest["applies_to_tools"])


def test_desktop_create_manifest_exposes_runtime_context_fields() -> None:
    parameters = _tool_parameters("desktop_create")
    properties = parameters["properties"]

    for field in [
        "starter",
        "browser_url",
        "workspace_id",
        "workspace_access",
        "assigned_agent",
        "assigned_agent_id",
        "access_request_required",
        "provisioning",
    ]:
        assert field in properties

    assert set(properties["starter"]["enum"]) == {"empty", "browser", "terminal", "browser_url"}
    assert set(properties["workspace_access"]["enum"]) == {"none", "read_only", "overlay", None}

    access = properties["access"]["properties"]
    assert "request_required" in access["mode"]["enum"]
    assert access["request_required"]["type"] == "boolean"
    assert properties["provisioning"]["properties"]["mcp_servers"]["items"]["type"] == "string"


def test_desktop_input_manifest_explains_text_requires_action() -> None:
    parameters = _tool_parameters("desktop_input")
    properties = parameters["properties"]

    assert "action" in parameters["required"]
    assert "type_text" in properties["action"]["description"]
    assert "Do not send text without action" in properties["action"]["description"]
    assert "action is type_text" in properties["text"]["description"]
    assert "action is key" in properties["key"]["description"]


def test_desktop_rules_manifest_exposes_request_required_access() -> None:
    properties = _tool_parameters("desktop_rules_update")["properties"]
    access = properties["access"]["properties"]

    assert "request_required" in access["mode"]["enum"]
    assert access["request_required"]["type"] == "boolean"
    assert properties["access_request_required"]["type"] == "boolean"


def test_desktop_access_manifests_expose_request_and_grant_identity() -> None:
    request_properties = _tool_parameters("desktop_access_request")["properties"]
    grant_parameters = _tool_parameters("desktop_access_grant")

    assert {"seat_id", "desktop_id", "requester_id", "owner_id", "reason"}.issubset(request_properties)
    assert grant_parameters["required"] == ["request_id"]
    assert {"seat_id"} in [set(item["required"]) for item in grant_parameters["anyOf"]]
    assert {"desktop_id"} in [set(item["required"]) for item in grant_parameters["anyOf"]]


def test_desktop_frame_evidence_is_a_separate_approval_gated_write_tool() -> None:
    manifest_path = (
        DEFAULTSPACK_ROOT / "tools" / "desktop_frame_evidence" / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))["config"]
    parameters = manifest["schema"]["parameters"]

    assert manifest["action_type"] == "write"
    assert manifest["write_action"] is True
    assert manifest["requires_approval"] is True
    assert manifest["risk"] == "high"
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]["action"]["enum"]) == {
        "persist",
        "export",
        "delete",
        "cleanup_run",
    }
    assert set(parameters["properties"]["purpose"]["enum"]) == {
        "visual_qa",
        "bug_report",
        "accessibility_qa",
    }
    assert manifest["ui"]["composer_label"] == "Desktop Frame Evidence"
    assert "private" in manifest["ui"]["composer_description"].lower()
