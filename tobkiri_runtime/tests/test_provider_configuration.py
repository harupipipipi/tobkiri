"""Provider configuration owner writes, failure reconciliation and redaction."""

from pathlib import Path
from types import SimpleNamespace
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


def test_configuration_approval_uses_existing_confirmation_and_redacts_key(tmp_path: Path) -> None:
    from core_runtime.interactive_effect_coordinator import (
        INTERACTIVE_EFFECT_SPECS, _presentation_metadata,
    )
    from tobkiri_protocol.canonical import canonical_digest

    request = _request()
    plan = prepare_configuration(ProviderRegistry("defaults", user_data_root=tmp_path), request)
    payload = {"request": request, "plan": plan}
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["provider_configure"],
        SimpleNamespace(request_digest=canonical_digest(payload), normalized_payload=payload),
    )
    assert metadata["confirmation_phrase"] == "EXECUTE"
    assert request["key_value"] not in str(metadata)
    assert request["endpoint"] in metadata["detail"]
    assert "provider.fixture" in metadata["detail"]


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
    result = execute_configuration(
        registry, client, {"request": request, "plan": plan}, consumer_pack_id="fixture.consumer",
    )
    assert result == {"configured": True, "provider_instance_id": "provider.fixture"}
    assert client.calls == ["create"]
    assert registry.snapshot()["revision"] == 1
    assert request["key_value"] not in registry.path.read_text()
    with pytest.raises(PermissionError):
        execute_configuration(
            registry, client, {"request": request, "plan": plan}, consumer_pack_id="fixture.consumer",
        )
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
        execute_configuration(
            registry, client, {"request": request, "plan": plan}, consumer_pack_id="fixture.consumer",
        )
    assert client.calls == ["create", "revoke"]
    assert not registry.path.exists()


def test_configuration_does_not_retry_or_revoke_an_unknown_credential_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ACK loss after credential commit must not trigger a second mutation."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = _request()
    plan = prepare_configuration(registry, request)
    original_invoke = client.invoke

    def lose_ack(contract: str, operation: str, payload: dict[str, Any]) -> Any:
        original_invoke(contract, operation, payload)
        raise OSError(request["key_value"])

    monkeypatch.setattr(client, "invoke", lose_ack)
    with pytest.raises(RuntimeError, match="credential save was not confirmed") as exc:
        execute_configuration(
            registry, client, {"request": request, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert request["key_value"] not in str(exc.value)
    assert client.calls == ["create"]
    assert not registry.path.exists()


@pytest.mark.parametrize("field,value", [
    ("profile_id", "foreign"),
    ("provider_instance_id", "provider.foreign"),
    ("adapter_id", "anthropic"),
    ("endpoint", "https://foreign.example/v1"),
    ("expected_revision", 1),
    ("request_digest", "sha256:" + "0" * 64),
])
def test_configuration_rejects_changed_plan_before_credential_creation(
    tmp_path: Path, field: str, value: Any,
) -> None:
    """Prepared owner, endpoint, protocol and revision cannot be substituted."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = _request()
    plan = {**prepare_configuration(registry, request), field: value}
    with pytest.raises(PermissionError, match="changed after preparation"):
        execute_configuration(
            registry, client, {"request": request, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == []
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
