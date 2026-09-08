"""Provider configuration owner writes, failure reconciliation and redaction."""

from pathlib import Path
from typing import Any

import pytest

from ecosystem.rumi_credential_broker_pack.runtime.service import CredentialBrokerService
from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
    CREDENTIAL_CONTRACT, CREDENTIAL_OPERATION, execute_configuration,
    prepare_configuration,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry


def _request() -> dict[str, str]:
    return {
        "connection_name": "fixture", "protocol": "openai-compatible",
        "endpoint": "https://provider.example/v1", "key_value": "fixture-secret-123",
    }


class _Client:
    def __init__(self, root: Path) -> None:
        self.service = CredentialBrokerService(user_data_root=root)
        self.calls: list[str] = []

    def invoke(self, contract: str, operation: str, payload: dict[str, Any]) -> Any:
        assert (contract, operation) == (CREDENTIAL_CONTRACT, CREDENTIAL_OPERATION)
        self.calls.append(payload["operation"])
        return self.service.invoke(payload["operation"], payload)


@pytest.mark.parametrize("lost_ack", [False, True])
def test_configuration_saves_once_and_recovers_connection_owner_ack(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, lost_ack: bool,
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = _request()
    plan = prepare_configuration(registry, request)
    assert request["key_value"] not in str(plan)
    assert not registry.path.exists()
    original_save = registry.save

    def save(*args: Any, **kwargs: Any) -> Any:
        result = original_save(*args, **kwargs)
        if lost_ack:
            raise OSError("fixture lost owner ACK")
        return result

    monkeypatch.setattr(registry, "save", save)
    result = execute_configuration(registry, client, {"request": request, "plan": plan})
    assert result == {"configured": True, "provider_instance_id": "provider.fixture"}
    assert client.calls == ["create"]
    assert registry.snapshot()["revision"] == 1
    assert request["key_value"] not in registry.path.read_text()
    with pytest.raises(PermissionError):
        execute_configuration(registry, client, {"request": request, "plan": plan})
    assert client.calls == ["create"]


def test_configuration_revokes_only_new_unused_handle_on_failed_connection_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = _request()
    plan = prepare_configuration(registry, request)

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise OSError("fixture failure")

    monkeypatch.setattr(registry, "save", fail)
    with pytest.raises(RuntimeError, match="connection save was not confirmed"):
        execute_configuration(registry, client, {"request": request, "plan": plan})
    assert client.calls == ["create", "revoke"]
    assert not registry.path.exists()


@pytest.mark.parametrize("change", [
    {"profile_id": "other"}, {"approved": True}, {"protocol": "unknown"},
    {"endpoint": "http://provider.example/v1"},
    {"endpoint": "https://user:password@provider.example/v1"},
    {"endpoint": "https://provider.example/v1?api_key=secret"},
    {"key_value": "bad\nheader"}, {"connection_name": "../outside"},
])
def test_configuration_prepare_rejects_invalid_input_without_writes(
    tmp_path: Path, change: dict[str, str],
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        prepare_configuration(registry, {**_request(), **change})
    assert not (tmp_path / "packs").exists()
