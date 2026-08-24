from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.ai_client.model_runtime_settings import (  # noqa: E402
    GLOBAL_THINKING_LEVEL_STATE_REF,
    PREFERRED_MODEL_STATE_REF,
    ModelRuntimeSettingsService,
)
from domain.frontend.command_protocol import CommandProtocolRegistry  # noqa: E402
from domain.frontend.command_registry import SlashCommandRegistry  # noqa: E402
from domain.frontend_settings_store import (  # noqa: E402
    FrontendSettingsRevisionConflict,
)
from domain.mobile.contract import match_mobile_route, required_device_scope  # noqa: E402


def test_model_controls_have_independent_idempotent_authoritative_revisions(
    tmp_path: Path,
) -> None:
    service = ModelRuntimeSettingsService(tmp_path)

    preferred = service.set_preferred_model(
        "stub/reasoning",
        expected_revision=0,
        idempotency_key="issue993-model-1",
    )
    preferred_replay = service.set_preferred_model(
        "stub/reasoning",
        expected_revision=0,
        idempotency_key="issue993-model-1",
    )
    thinking = service.set_thinking_level(
        "high",
        expected_revision=0,
        idempotency_key="issue993-thinking-1",
    )

    assert preferred["state_snapshot"] == {
        "state_ref": PREFERRED_MODEL_STATE_REF,
        "value": "stub/reasoning",
        "revision": 1,
        "freshness": "authoritative",
    }
    assert preferred_replay["idempotent_replay"] is True
    assert preferred_replay["revision"] == 1
    assert thinking["state_snapshot"] == {
        "state_ref": GLOBAL_THINKING_LEVEL_STATE_REF,
        "value": "high",
        "revision": 1,
        "freshness": "authoritative",
    }
    with pytest.raises(FrontendSettingsRevisionConflict):
        service.set_thinking_level("low", expected_revision=0)


def test_query_states_returns_one_model_control_snapshot_with_document_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(tmp_path / "settings.json")
    )
    service = ModelRuntimeSettingsService(DEFAULTSPACK_ROOT)
    service.set_preferred_model("stub/selected", expected_revision=0)
    service.set_thinking_level("xhigh", expected_revision=0)
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    snapshot = protocol.query_states()
    states = {item["state_ref"]: item for item in snapshot["states"]}

    assert states[PREFERRED_MODEL_STATE_REF] == {
        "state_ref": PREFERRED_MODEL_STATE_REF,
        "value": "stub/selected",
        "revision": 1,
        "freshness": "authoritative",
    }
    assert states[GLOBAL_THINKING_LEVEL_STATE_REF] == {
        "state_ref": GLOBAL_THINKING_LEVEL_STATE_REF,
        "value": "xhigh",
        "revision": 1,
        "freshness": "authoritative",
    }
    assert snapshot["snapshot_revision"] == snapshot["document_revision"]
    assert snapshot["document_revision"] >= 2
    assert snapshot["snapshot_id"] == (
        f"defaultspack-model-controls-{snapshot['document_revision']}"
    )


def test_command_transport_passes_concurrency_fields_to_model_controls() -> None:
    registry = SlashCommandRegistry(DEFAULTSPACK_ROOT)
    candidate = {"profile_id": "stub/selected"}
    with patch(
        "domain.ai_client.model_runtime_settings.ModelRuntimeSettingsService"
    ) as service_cls:
        service = service_cls.return_value
        service.resolve_model_candidates.return_value = {
            "query": "stub/selected",
            "exact": candidate,
            "candidates": [candidate],
        }
        service.set_preferred_model.return_value = {
            "profile_id": "stub/selected",
            "state_snapshot": {
                "state_ref": PREFERRED_MODEL_STATE_REF,
                "value": "stub/selected",
                "revision": 1,
                "freshness": "authoritative",
            },
        }

        result = registry.execute(
            {
                "command": "model",
                "mode": "chat",
                "args": {"query": "stub/selected"},
                "expected_revision": 0,
                "idempotency_key": "issue993-command-model-1",
                "client_sequence": 42,
            }
        )

    assert result["data"]["client_sequence"] == 42
    assert result["data"]["state_changes"] == [
        service.set_preferred_model.return_value["state_snapshot"]
    ]
    service.set_preferred_model.assert_called_once_with(
        "stub/selected",
        expected_revision=0,
        idempotency_key="issue993-command-model-1",
    )


def test_protocol_invocation_returns_global_thinking_authoritative_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_FRONTEND_SETTINGS_PATH", str(tmp_path / "settings.json")
    )
    protocol = CommandProtocolRegistry(DEFAULTSPACK_ROOT)

    result = protocol.invoke(
        {
            "command_ref": "defaultspack:think",
            "args": {"level": "high"},
            "expected_revision": 0,
            "idempotency_key": "issue993-command-thinking-1",
            "client_sequence": 43,
        }
    )

    assert result["status"] == "succeeded"
    assert result["client_sequence"] == 43
    assert result["state_changes"] == [
        {
            "state_ref": GLOBAL_THINKING_LEVEL_STATE_REF,
            "value": "high",
            "revision": 1,
            "freshness": "authoritative",
        }
    ]


def test_mobile_runtime_control_facade_is_scoped_and_forwards_trusted_context() -> None:
    from blocks.mobile.runtime_controls import run

    query_route = match_mobile_route("POST", "/api/mobile/v1/control-states/query")
    invoke_route = match_mobile_route("POST", "/api/mobile/v1/control-commands/invoke")

    assert query_route is not None
    assert query_route.block_module == "blocks.mobile.runtime_controls"
    assert (
        required_device_scope("POST", "/api/mobile/v1/control-states/query")
        == "chat.read"
    )
    assert invoke_route is not None
    assert (
        required_device_scope("POST", "/api/mobile/v1/control-commands/invoke")
        == "chat.write"
    )

    with patch("blocks.mobile.runtime_controls.CommandProtocolRegistry") as registry_cls:
        registry = registry_cls.return_value
        registry.invoke.return_value = {
            "status": "succeeded",
            "state_changes": [],
        }
        context = {"authenticated_principal_id": "device:mobile-1"}
        response = run(
            {
                "_mobile_control_operation": "invoke",
                "command_ref": "defaultspack:think",
                "args": {"level": "high"},
                "client_sequence": 7,
                "_owner_key": "attacker:override",
                "principal_id": "attacker",
            },
            context,
        )

    assert response["status"] == "ok"
    forwarded, forwarded_context = registry.invoke.call_args.args
    assert forwarded == {
        "command_ref": "defaultspack:think",
        "args": {"level": "high"},
        "client_sequence": 7,
    }
    assert forwarded_context == context


def test_shipping_mobile_entry_uses_authoritative_control_coordinator() -> None:
    source = (ROOT / "../tobkiri_mobile/lib/src/chat/chat_screen.dart").resolve()
    text = source.read_text(encoding="utf-8")

    assert "PcControlCoordinator? _pcControlCoordinator" in text
    assert "client.invokeControlCommand(" in text
    assert "client.fetchControlSnapshot(" in text
    assert "await _requestPcControl(connection, 'model', profileId);" in text
    assert "setState(() => _selectedPcModel = profileId)" not in text
    assert "PCモデル設定に失敗しました: $e" not in text
