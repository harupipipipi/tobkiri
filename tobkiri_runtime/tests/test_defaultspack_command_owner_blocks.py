"""Trusted settings bindings at legacy command block entry points."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ecosystem" / "defaultspack"))

from ecosystem.tobkiri_ui_settings_pack.runtime.store import (  # noqa: E402
    FrontendSettingsStore,
)


@pytest.mark.parametrize("name", [
    "command_protocol_catalog", "command_protocol_states",
    "command_protocol_datasources", "command_protocol_invoke",
    "command_protocol_resume", "command_protocol_stream",
    "command_protocol_events", "command_protocol_offline", "commands",
])
def test_command_block_uses_only_explicit_owner(name, tmp_path, monkeypatch):
    """JSON/context cannot provide the trusted port or leak it across calls."""
    module = importlib.import_module(f"blocks.ui.{name}")
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    captured = []

    class Constructed(Exception):
        pass

    def registry(*, settings_owner):
        captured.append(settings_owner)
        raise Constructed

    monkeypatch.setattr(module, "CommandProtocolRegistry", registry)
    payload = {"invocation_id": "test", "settings_owner": "untrusted"}
    context = {"settings_owner": "untrusted"}
    with pytest.raises(Constructed):
        module.run(payload, context, settings_owner=owner)
    with pytest.raises(Constructed):
        module.run(payload, context)
    assert captured == [owner, None]
    assert not (tmp_path / "owned.json").exists()


def test_command_blocks_mutate_and_read_the_same_owner(tmp_path, monkeypatch):
    """Actual command invocation and state read preserve the bound owner."""
    invoke = importlib.import_module("blocks.ui.command_protocol_invoke")
    states = importlib.import_module("blocks.ui.command_protocol_states")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMMAND_STATE_DIR", str(tmp_path / "events"))
    legacy = tmp_path / "legacy.json"
    legacy.write_text('{"models":{"deepthink_enabled":false}}', encoding="utf-8")
    before = legacy.read_bytes()
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(legacy))
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    result = invoke.run({
        "command_ref": "defaultspack:deepthink",
        "args": {"enabled": True}, "mode": "chat",
        "invocation_id": "block-owner", "expected_revision": 0,
        "idempotency_key": "block-owner",
    }, {}, settings_owner=owner)
    assert result["data"]["status"] == "succeeded", result
    result = states.run({}, {}, settings_owner=owner)
    assert result["data"]["states"][0]["value"] is True
    assert result["data"]["states"][0]["revision"] == 1
    assert owner.read_snapshot()["models"]["deepthink_enabled"] is True
    with pytest.raises(RuntimeError, match="explicit settings owner"):
        states.run({"settings_owner": str(owner.path)}, {})
    assert legacy.read_bytes() == before


def test_fast_command_requires_owner_and_preserves_unrelated_settings(tmp_path):
    """Fast mode never trusts a payload owner and retains other preferences."""
    module = importlib.import_module("blocks.ai.fast_command")
    path = tmp_path / "owned.json"
    path.write_text('{"general":{"language":"ja"}}', encoding="utf-8")
    owner = FrontendSettingsStore(path)
    before = path.read_bytes()
    with pytest.raises(RuntimeError, match="explicit settings owner"):
        module.run({"enabled": True, "settings_owner": str(path)}, {})
    assert path.read_bytes() == before
    assert module.run({"enabled": True}, {}, settings_owner=owner)["status"] == "ok"
    assert owner.read_snapshot()["models"]["fast_mode_enabled"] is True
    assert module.run({"enabled": False}, {}, settings_owner=owner)["status"] == "ok"
    assert owner.read_snapshot()["models"]["fast_mode_enabled"] is False
    assert owner.read_snapshot()["general"]["language"] == "ja"


def test_command_http_routes_retain_the_setup_owner(tmp_path, monkeypatch):
    """Per-request payload/context cannot replace the setup-captured owner."""
    setup = importlib.import_module("blocks.ui.setup")
    states = importlib.import_module("blocks.ui.command_protocol_states")
    settings = importlib.import_module("blocks.ui.settings")
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    request_owner = FrontendSettingsStore(tmp_path / "request.json")
    captured = []

    def run_states(input_data, context, *, settings_owner=None):
        captured.append((input_data, context, settings_owner))
        return {"status": "ok"}

    monkeypatch.setattr(states, "run", run_states)
    monkeypatch.setattr(settings, "run", run_states)

    class Registry:
        def __init__(self):
            self.routes = []

        def register(self, kind, value, meta=None):
            if kind == "io.http.route":
                self.routes.append(value)

    registry = Registry()
    setup.run(
        {
            "interface_registry": registry,
            "_settings_owner_port": owner,
        }
    )
    for pattern in (
        "/api/ui/settings",
        "/api/command-protocol/v1/states/query",
    ):
        route = next(
            item for item in registry.routes if item["pattern"] == pattern
        )
        result = route["handler"](
            {"settings_owner": "payload-owner"},
            {"_settings_owner_port": request_owner},
        )
        assert result == {"status": "ok"}

    assert [item[2] for item in captured] == [owner, owner]
    assert all(item[0]["settings_owner"] == "payload-owner" for item in captured)
    assert all(
        item[1]["_settings_owner_port"] is request_owner for item in captured
    )
    assert not owner.path.exists()
    assert not request_owner.path.exists()


@pytest.mark.parametrize(
    ("pattern", "module_name"),
    [
        ("/api/tools/catalog", "blocks.tool.catalog"),
        ("/api/tools/selection/preview", "blocks.tool.selection_preview"),
    ],
)
def test_tool_http_routes_retain_the_setup_owner(
    pattern, module_name, tmp_path, monkeypatch
):
    """Tool settings reads use only the owner captured during setup."""
    setup = importlib.import_module("blocks.tool.setup")
    module = importlib.import_module(module_name)
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    request_owner = FrontendSettingsStore(tmp_path / "request.json")
    captured = []

    def handler(input_data, context, *, settings_owner=None):
        captured.append((input_data, context, settings_owner))
        return {"status": "ok"}

    monkeypatch.setattr(module, "run", handler)

    class Registry:
        def __init__(self):
            self.routes = []

        def register(self, kind, value, meta=None):
            if kind == "io.http.route":
                self.routes.append(value)

    registry = Registry()
    setup.run(
        {
            "interface_registry": registry,
            "_settings_owner_port": owner,
        }
    )
    route = next(item for item in registry.routes if item["pattern"] == pattern)
    result = route["handler"](
        {"settings_owner": "payload-owner"},
        {"_settings_owner_port": request_owner},
    )

    assert result == {"status": "ok"}
    assert captured == [
        (
            {"settings_owner": "payload-owner"},
            {"_settings_owner_port": request_owner},
            owner,
        )
    ]
    assert not owner.path.exists()
    assert not request_owner.path.exists()


def test_tool_catalog_uses_only_explicit_owner(tmp_path, monkeypatch):
    """Catalog permission projection cannot select an owner from JSON."""
    module = importlib.import_module("blocks.tool.catalog")
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    captured = []

    class Registry:
        def list_tools(self):
            return []

    class Resolver:
        def __init__(self, *, settings_owner=None):
            captured.append(settings_owner)

    monkeypatch.setattr(module, "ToolRegistry", Registry)
    monkeypatch.setattr(module, "ToolPermissionResolver", Resolver)

    assert module.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
        settings_owner=owner,
    )["status"] == "ok"
    assert module.run(
        {"settings_owner": "payload-owner"},
        {"settings_owner": "context-owner"},
    )["status"] == "ok"
    assert captured == [owner, None]
    assert not owner.path.exists()


def test_tool_selection_preview_uses_only_explicit_owner(tmp_path, monkeypatch):
    """Preview settings lookup receives only the trusted keyword owner."""
    module = importlib.import_module("blocks.tool.selection_preview")
    owner = FrontendSettingsStore(tmp_path / "owned.json")
    captured = []

    class Registry:
        def list_tools(self):
            return []

    class SelectionService:
        def __init__(self, *, call_handler=None, settings_owner=None):
            del call_handler
            captured.append(settings_owner)

        def select(self, *args, **kwargs):
            del args, kwargs
            raise RuntimeError("stop after owner capture")

    monkeypatch.setattr(module, "ToolRegistry", Registry)
    monkeypatch.setattr(module, "ToolSelectionService", SelectionService)

    for explicit_owner in (owner, None):
        result = module.run(
            {
                "user_text": "test",
                "settings_owner": "payload-owner",
                "context": {"settings_owner": "nested-owner"},
            },
            {"settings_owner": "context-owner"},
            settings_owner=explicit_owner,
        )
        assert result["status"] == "error"
        assert result["error"]["code"] == "SELECTION_PREVIEW_FAILED"

    assert captured == [owner, None]
    assert not owner.path.exists()
