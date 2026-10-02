from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.function_runtime.dispatcher import run_defaultspack_function  # noqa: E402
from ecosystem.tobkiri_ui_settings_pack.runtime.store import (  # noqa: E402
    FrontendSettingsStore,
)


def test_model_runtime_function_uses_captured_settings_owner(tmp_path: Path) -> None:
    owner = FrontendSettingsStore(tmp_path / "frontend_settings.json")
    owner.update(
        lambda current: {
            **current,
            "models": {"preferred_model": "test-provider/test-model"},
        }
    )

    result = run_defaultspack_function(
        "ai_get_preferred_model",
        {},
        {"_settings_owner_port": owner},
    )

    assert result == {
        "status": "ok",
        "data": {"profile_id": "test-provider/test-model"},
    }


def test_block_function_receives_owner_only_from_captured_context(
    monkeypatch: Any,
) -> None:
    import blocks.agent.execute as execute_block

    captured_owner = object()
    payload_owner = object()
    received: list[object | None] = []

    def fake_run(
        input_data: dict[str, Any],
        context: dict[str, Any],
        *,
        settings_owner: object | None = None,
    ) -> dict[str, Any]:
        received.append(settings_owner)
        assert input_data["_settings_owner_port"] is payload_owner
        assert context["_settings_owner_port"] is captured_owner
        return {"status": "ok", "data": {"owner_bound": True}}

    monkeypatch.setattr(execute_block, "run", fake_run)

    result = run_defaultspack_function(
        "agent_execute",
        {"_settings_owner_port": payload_owner},
        {"_settings_owner_port": captured_owner},
    )

    assert result == {"status": "ok", "data": {"owner_bound": True}}
    assert received == [captured_owner]


def test_tool_function_binds_captured_settings_owner(monkeypatch: Any) -> None:
    import domain.tool.executor as executor_module

    captured_owner = object()
    received: list[object | None] = []

    class FakeToolExecutor:
        def __init__(
            self,
            *,
            subagent_factory: object | None = None,
            settings_owner: object | None = None,
        ) -> None:
            del subagent_factory
            received.append(settings_owner)

        def _execute_local(
            self,
            tool_name: str,
            arguments: dict[str, Any],
            context: dict[str, Any],
        ) -> dict[str, Any]:
            assert tool_name == "calculator"
            assert arguments == {"expression": "1 + 1"}
            assert context["_settings_owner_port"] is captured_owner
            return {"result": "2", "is_error": False}

    monkeypatch.setattr(executor_module, "ToolExecutor", FakeToolExecutor)

    result = run_defaultspack_function(
        "tool_calculator",
        {"expression": "1 + 1"},
        {"_settings_owner_port": captured_owner},
    )

    assert result["status"] == "ok"
    assert result["data"] == {"result": "2", "is_error": False}
    assert received == [captured_owner]
