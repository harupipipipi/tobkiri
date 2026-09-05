from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_isolated_frontend_settings_selects_cerebras_without_persisting_credential(tmp_path, monkeypatch):
    """All model-selection consumers use the debug run's secret-free settings."""
    settings_path = tmp_path / "isolated" / "frontend_settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps({"models": {"preferred_model": "cerebras/gemma-4-31b"}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(settings_path))

    from domain.ai_client.client import AIClient
    from domain.ai_client.model_runtime_settings import ModelRuntimeSettingsService
    from domain.frontend_settings_store import (
        defaultspack_frontend_settings_path,
    )

    service = ModelRuntimeSettingsService(DEFAULTSPACK_ROOT)
    assert service._settings_path == settings_path.resolve()
    assert service.get_preferred_model() == "cerebras/gemma-4-31b"
    assert (
        defaultspack_frontend_settings_path(DEFAULTSPACK_ROOT)
        == settings_path.resolve()
    )

    AIClient._instance = None
    client = AIClient()
    assert client._settings_path() == settings_path.resolve()
    assert client._settings_data()["models"]["preferred_model"] == "cerebras/gemma-4-31b"
    AIClient._instance = None

    stored = json.loads(settings_path.read_text(encoding="utf-8"))
    assert stored == {"models": {"preferred_model": "cerebras/gemma-4-31b"}}


def test_all_frontend_settings_consumers_use_isolated_path(tmp_path, monkeypatch):
    """Runtime policy, integration, registry, and debug readers stay isolated."""
    settings_path = tmp_path / "debug-state" / "frontend_settings.json"
    settings_path.parent.mkdir(parents=True)
    isolated_settings = {
        "debug": {"ai_request_logging": True},
        "external_output": {"mode": "isolated"},
        "tools": {"selection_strategy": "all_schemas"},
        "triggers": {"mode": "llm", "model": "isolated/model"},
    }
    settings_path.write_text(json.dumps(isolated_settings), encoding="utf-8")
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH",
        str(settings_path),
    )

    from blocks.chat import send
    from domain.chat import run_request, tool_recommender
    from domain.external import trigger_decision
    from domain.frontend.registry import FrontendRegistry
    from domain.integrations.line import addressing, inbound
    from domain.tool import permission_resolver

    assert tool_recommender._read_frontend_settings() == isolated_settings
    trigger_config = trigger_decision._frontend_trigger_config()
    assert trigger_config["llm"]["model"] == "isolated/model"
    assert permission_resolver.read_frontend_settings() == isolated_settings
    assert run_request._read_frontend_settings() == isolated_settings
    assert send._frontend_debug_settings_enabled() is True
    assert addressing._frontend_settings_path() == settings_path.resolve()
    assert inbound._frontend_settings_path() == settings_path.resolve()
    assert inbound._frontend_external_output_settings() == {"mode": "isolated"}
    registry = FrontendRegistry(tmp_path / "shared-pack")
    assert registry._settings_path == settings_path.resolve()
