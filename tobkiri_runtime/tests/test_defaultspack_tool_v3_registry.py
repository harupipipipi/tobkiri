from __future__ import annotations

import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
PACK = ROOT / "ecosystem" / "defaultspack"
for item in (str(ROOT), str(PACK)):
    if item not in sys.path:
        sys.path.insert(0, item)

from domain.tool.registry import ToolRegistrationError, ToolRegistry  # noqa: E402


def _manifest() -> dict:
    return {
        "schema_version": "tobkiri.tool/v3",
        "kind": "tool",
        "id": "defaultspack.example_context",
        "version": "1.0.0",
        "display_name": {"ja": "文脈", "en": "Context"},
        "description": {"ja": "正規化", "en": "Normalize context"},
        "discovery": {
            "aliases": ["normalize"],
            "keywords": ["context", "task"],
            "activity_ids": ["defaultspack.agent_work"],
            "visibility": "public",
            "schema_loading": "on_demand",
        },
        "contract": {
            "input_schema": {
                "type": "object",
                "required": ["task"],
                "properties": {"task": {"type": "string"}},
            },
            "output_schema": {"type": "object"},
        },
        "effects": [],
        "risk": {"level": "low", "reasons": ["pure"]},
        "approval": {"default": "auto", "minimum": "auto"},
        "execution": {
            "type": "rumi_function",
            "qualified_name": "defaultspack:task_context",
        },
        "requirements": {
            "runtime_capabilities": ["json"],
            "model_capabilities": ["tools"],
            "connections": [],
            "env": [],
        },
        "security": {
            "sandbox": "required",
            "network": "deny",
            "filesystem": "deny",
        },
        "ui": {"icon": "task", "visibility": "public"},
    }


def test_tool_v3_projects_all_ai_selection_and_execution_fields() -> None:
    tool = ToolRegistry._tool_from_manifest(
        _manifest(),
        source_pack_id="defaultspack",
    )

    assert tool is not None
    assert tool["tool_id"] == "defaultspack.example_context"
    assert tool["display_name"] == "Context"
    assert tool["description"] == "Normalize context"
    assert tool["schema"]["parameters"]["required"] == ["task"]
    assert tool["execution"]["type"] == "rumi_function"
    assert tool["execution"]["qualified_name"] == "defaultspack:task_context"
    assert tool["risk"] == "low"
    assert tool["requires_approval"] is False
    assert tool["loading"] == "vector"
    assert "context" in tool["tags"]
    assert (
        tool["metadata"]["schema_version"]
        == "tobkiri.tool/v3"
    )
    assert tool["requires_model_capabilities"] == ["tools"]
    assert tool["requires_runtime_capabilities"] == ["json"]


def test_tool_v3_external_effect_requires_approval() -> None:
    manifest = _manifest()
    manifest["effects"] = [
        {
            "class": "write",
            "operation": "publish",
            "reversible": False,
            "external": True,
        }
    ]
    manifest["risk"] = {"level": "high"}
    manifest["approval"] = {"default": "confirm", "minimum": "confirm"}

    tool = ToolRegistry._tool_from_manifest(
        manifest,
        source_pack_id="defaultspack",
    )

    assert tool is not None
    assert tool["write_action"] is True
    assert tool["requires_approval"] is True
    assert tool["approval_policy"] == "confirm"


def test_tool_v3_preserves_critical_risk() -> None:
    manifest = _manifest()
    manifest["risk"] = {"level": "critical"}
    manifest["approval"] = {"default": "deny", "minimum": "deny"}

    tool = ToolRegistry._tool_from_manifest(
        manifest,
        source_pack_id="defaultspack",
    )

    assert tool is not None
    assert tool["risk"] == "critical"
    assert tool["requires_approval"] is True


def test_pack_discovery_deduplicates_only_the_same_manifest(
    tmp_path: Path,
) -> None:
    manifest_path = tmp_path / "same" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text("{}", encoding="utf-8")
    other_path = tmp_path / "other" / "manifest.json"
    other_path.parent.mkdir(parents=True)
    other_path.write_text("{}", encoding="utf-8")

    registry = object.__new__(ToolRegistry)
    registry._tools = {
        "same_tool": {
            "tool_id": "same_tool",
            "source_path": str(manifest_path),
            "source_pack_id": "defaultspack",
        }
    }
    registry._diagnostics = []
    registry._lock = threading.Lock()

    duplicate = {
        "tool_id": "same_tool",
        "source_path": str(manifest_path),
        "source_pack_id": "defaultspack",
        "display_name": "normalized differently",
    }
    shadow = {
        "tool_id": "same_tool",
        "source_path": str(other_path),
        "source_pack_id": "defaultspack",
    }

    assert registry._already_loaded_from_manifest(duplicate) is True
    assert registry._already_loaded_from_manifest(shadow) is False
    try:
        registry.register(shadow)
    except ToolRegistrationError as exc:
        assert "tool_id collision" in str(exc)
    else:
        raise AssertionError("a different manifest shadowed an existing Tool")
