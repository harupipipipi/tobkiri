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
