"""Captured consumers obtain application data through explicit contract calls."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ecosystem.rumi_command_protocol_pack.runtime.catalog import (
    CONTRACT_ID as COMMAND_CONTRACT,
    FUNCTION_ID as COMMAND_FUNCTION,
    OPERATION_ID as COMMAND_OPERATION,
    CommandCatalogHostFactoryV4,
)
from ecosystem.tobkiri_ui_settings_pack.runtime.settings import (
    CONTRACT_ID,
    FUNCTION_ID,
    OPERATION_ID,
    MODEL_CONTRACT,
    MODEL_OPERATION,
    PRESENTATION_CONTRACT,
    PRESENTATION_OPERATION,
    SettingsReadHostFactoryV4,
    _values,
)
from scripts.generate_application_presentation import OUTPUT


class Client:
    def __init__(self) -> None:
        self.calls = []
        self.scopes = []

    def contract_client(self, **scope):
        self.scopes.append(scope)
        return self

    def invoke(self, contract, operation, payload):
        self.calls.append((contract, operation, payload))
        assert payload["profile_id"] == "defaults"
        if (contract, operation) == (MODEL_CONTRACT, MODEL_OPERATION):
            return {
                "profiles": [
                    {"model_profile_id": "selected", "display_name": "Selected", "enabled": True}
                ]
            }
        assert (contract, operation) == (PRESENTATION_CONTRACT, PRESENTATION_OPERATION)
        spec = importlib.util.spec_from_file_location("presentation_consumer_fixture", OUTPUT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.tobkiri_packvm_invoke(operation, payload)


def _context(root, function, contract, operation, *, high_risk=False):
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function, implementation_digest="impl"),
        operation=SimpleNamespace(
            contract_id=contract, operation_id=operation, contract_version="1.0.0"
        ),
        principal_ref=SimpleNamespace(value="principal"),
        artifact=SimpleNamespace(digest="artifact"),
    )
    high = SimpleNamespace(
        function=SimpleNamespace(
            function_id="rumi_command_protocol_pack.high-risk-command.service"
        ),
        operation=SimpleNamespace(
            contract_id="tobkiri.service.command.high-risk.v1",
            operation_id="high_risk_command.manage",
        ),
    )
    return SimpleNamespace(
        profile_id="defaults",
        user_data_root=root,
        provider_bindings=(binding,),
        domain_ids={(contract, operation, "principal"): "domain"},
        catalog_bindings=(high,) if high_risk else (),
    )


@pytest.mark.parametrize("high_risk", [False, True])
def test_command_consumer_keeps_host_approval_policy(tmp_path: Path, high_risk: bool) -> None:
    factory = CommandCatalogHostFactoryV4()
    captured = factory.capture(
        _context(
            tmp_path, COMMAND_FUNCTION, COMMAND_CONTRACT, COMMAND_OPERATION, high_risk=high_risk
        )
    )
    client = Client()
    result = captured.contributions[0].invoke(COMMAND_OPERATION, {"profile_id": "defaults"}, client)
    assert len(result["commands"]) == 50
    available = [
        item for item in result["commands"] if item["availability"]["status"] == "available"
    ]
    assert bool(available) is high_risk
    assert all(item["authorization"]["approval_required"] for item in available)
    assert all(
        item["authorization"]["permissions"] == ["host.process.exec_guarded"] for item in available
    )
    assert client.scopes == [
        {
            "allowed_contract_ids": frozenset({PRESENTATION_CONTRACT}),
            "consumer_pack_id": "rumi_command_protocol_pack",
        }
    ]
    assert len(client.calls) == 1
    assert list(tmp_path.iterdir()) == []


def test_settings_consumer_joins_models_and_public_saved_values(tmp_path: Path) -> None:
    path = tmp_path / "defaultspack" / "shared" / "frontend_settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"general": {"composer_placeholder": "Saved", "private_secret": "hidden"}})
    )
    before = path.read_bytes()
    captured = SettingsReadHostFactoryV4().capture(
        _context(tmp_path, FUNCTION_ID, CONTRACT_ID, OPERATION_ID)
    )
    client = Client()
    result = captured.contributions[0].invoke(OPERATION_ID, {"profile_id": "defaults"}, client)
    assert result["values"]["general"]["composer_placeholder"] == "Saved"
    assert "hidden" not in json.dumps(result)
    assert "selected" in json.dumps(result["sections"])
    assert [call[0] for call in client.calls] == [MODEL_CONTRACT, PRESENTATION_CONTRACT]
    assert client.scopes[0]["allowed_contract_ids"] == frozenset(
        {MODEL_CONTRACT, PRESENTATION_CONTRACT}
    )
    assert path.read_bytes() == before


def test_untrusted_ui_fields_cannot_expand_saved_data_disclosure() -> None:
    sections = [
        {
            "id": "general",
            "fields": [
                {"id": "private_secret", "type": "text"},
                {"id": "composer_placeholder", "type": "text"},
            ],
        },
        {"id": "_mutation_receipts", "fields": [{"id": "token", "type": "text"}]},
    ]
    values = _values(
        sections,
        {
            "general": {"private_secret": "hidden", "composer_placeholder": "Public"},
            "_mutation_receipts": {"token": "receipt"},
        },
    )
    assert values == {"general": {"composer_placeholder": "Public"}, "_mutation_receipts": {}}


def test_dependency_failure_has_no_direct_import_fallback(tmp_path: Path) -> None:
    captured = CommandCatalogHostFactoryV4().capture(
        _context(tmp_path, COMMAND_FUNCTION, COMMAND_CONTRACT, COMMAND_OPERATION)
    )

    class Denied(Client):
        def invoke(self, *args):
            raise PermissionError("unselected presentation binding")

    with pytest.raises(PermissionError, match="unselected"):
        captured.contributions[0].invoke(COMMAND_OPERATION, {"profile_id": "defaults"}, Denied())
    assert list(tmp_path.iterdir()) == []
