"""Build-time command inventory must not consult personal settings."""

from __future__ import annotations

import json

from scripts.quality.scan_command_protocol import scan


def test_inventory_ignores_ambient_settings_and_leaves_them_untouched(
    tmp_path, monkeypatch,
):
    """Personal slash commands and corrupt settings cannot alter the CI catalog."""
    settings = tmp_path / "personal.json"
    settings.write_text(json.dumps({"commands": {"registered_slash_commands": [
        {"name": "personal-canary", "action": "toggle_yolo"},
    ]}}), encoding="utf-8")
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings))
    commands = tmp_path / "personal-commands"
    monkeypatch.setenv("RUMI_DEFAULTSPACK_COMMAND_STATE_DIR", str(commands))
    before = settings.read_bytes()
    first = scan()
    assert settings.read_bytes() == before
    settings.write_text("{corrupt", encoding="utf-8")
    assert scan() == first
    assert first["command_count"] == 55
    assert "personal-canary" not in str(first)
    assert settings.read_text(encoding="utf-8") == "{corrupt"
    assert not commands.exists()
    assert list(tmp_path.iterdir()) == [settings]
