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
from core_runtime.interactive_effect_coordinator import (
    INTERACTIVE_EFFECT_SPECS,
    InteractiveEffectUnavailable,
    _execute_payload,
)


def _request() -> dict[str, str]:
    return {
        "connection_name": "fixture", "protocol": "openai-compatible",
        "endpoint": "https://provider.example/v1", "key_value": "fixture-secret-123",
    }


def _local_request() -> dict[str, str]:
    return {
        "connection_name": "local-fixture",
        "protocol": "local-openai-compatible",
        "endpoint": "http://127.0.0.1:1234/v1/",
        "key_value": "",
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


@pytest.mark.parametrize("lost_ack", [False, True])
def test_local_configuration_never_writes_a_credential_and_recovers_owner_ack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    lost_ack: bool,
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = {
        **_local_request(),
        "display_name": "ローカル Gemma 3",
        "catalog_provider_id": "openai_compatible",
    }
    plan = prepare_configuration(registry, request)
    assert plan["endpoint"] == "http://127.0.0.1:1234/v1"
    original_save = registry.save

    def save(*args: Any, **kwargs: Any) -> Any:
        result = original_save(*args, **kwargs)
        if lost_ack:
            raise OSError("fixture lost owner ACK")
        return result

    monkeypatch.setattr(registry, "save", save)
    result = execute_configuration(
        registry,
        client,
        {"request": request, "plan": plan},
        consumer_pack_id="fixture.consumer",
    )
    assert result["provider_instance_id"] == "provider.local-fixture"
    assert client.calls == []
    saved = registry.snapshot()["providers"][0]
    assert saved["credential_handle"] is None
    assert saved["display_name"] == "ローカル Gemma 3"
    assert saved["metadata"] == {"catalog_provider_id": "openai_compatible"}
    with pytest.raises(PermissionError, match="changed after preparation"):
        execute_configuration(
            registry,
            client,
            {"request": request, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == []


def test_local_trailing_slash_plan_binds_raw_approval_request(
    tmp_path: Path,
) -> None:
    """Canonical endpoint storage must preserve the coordinator request digest."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    request = _local_request()
    plan = prepare_configuration(registry, request)
    assert plan["endpoint"] == "http://127.0.0.1:1234/v1"
    execute_payload = _execute_payload(
        INTERACTIVE_EFFECT_SPECS["provider_configure"], request, plan,
    )
    assert execute_payload == {"request": request, "plan": plan}


@pytest.mark.parametrize(
    "change",
    [
        {"endpoint": "http://localhost:1234/v1"},
        {"endpoint": "http://127.0.0.1/v1"},
        {"endpoint": "http://127.0.0.1:80/v1"},
        {"endpoint": "https://127.0.0.1:1234/v1"},
        {"endpoint": "http://127.0.0.1:1234/v1/models"},
        {"endpoint": "http://user@127.0.0.1:1234/v1"},
        {"endpoint": "http://127.0.0.1:1234/v1?key=value"},
        {"key_value": "must-not-exist"},
    ],
)
def test_local_configuration_rejects_noncanonical_or_credentialed_input(
    tmp_path: Path,
    change: dict[str, str],
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        prepare_configuration(registry, {**_local_request(), **change})
    assert not (tmp_path / "packs").exists()


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


def test_configuration_keeps_unicode_name_and_provider_catalog_identity(
    tmp_path: Path,
) -> None:
    """The approved key remains bound to its exact named provider connection."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = {
        **_request(), "display_name": "仕事用 API",
        "catalog_provider_id": "openrouter",
    }
    plan = prepare_configuration(registry, request)
    execute_configuration(
        registry, client, {"request": request, "plan": plan},
        consumer_pack_id="fixture.consumer",
    )
    saved = registry.snapshot()["providers"][0]
    assert saved["display_name"] == "仕事用 API"
    assert saved["metadata"] == {"catalog_provider_id": "openrouter"}
    assert request["key_value"] not in registry.path.read_text()


@pytest.mark.parametrize("field,value", [
    ("catalog_provider_id", "openrouter"), ("display_name", "仕事用 API"),
])
def test_configuration_cannot_change_discovery_identity_after_approval(
    tmp_path: Path, field: str, value: str,
) -> None:
    """Catalog or label substitution fails before any credential creation."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    original = {**_request(), "catalog_provider_id": "openai", "display_name": "main"}
    plan = prepare_configuration(registry, original)
    with pytest.raises(PermissionError, match="changed after preparation"):
        execute_configuration(
            registry, client, {"request": {**original, field: value}, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == []


@pytest.mark.parametrize("change", [
    {"catalog_provider_id": "../other"}, {"display_name": "bad\nlabel"},
    {"display_name": "fixture-secret-123"}, {"display_name": ""},
])
def test_configuration_rejects_invalid_public_setup_metadata(
    tmp_path: Path, change: dict[str, str],
) -> None:
    """Public labels cannot leak a key or introduce unsafe identifiers."""
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        prepare_configuration(registry, {**_request(), **change})
    assert not registry.path.exists()


def test_audio_scopes_are_explicit_frozen_and_shown_in_approval(tmp_path: Path) -> None:
    from core_runtime.interactive_effect_coordinator import _presentation_metadata
    from tobkiri_protocol.canonical import canonical_digest

    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    request = {**_request(), "capabilities": ["ai.generate", "ai.audio.transcribe"]}
    before = registry.snapshot()
    plan = prepare_configuration(registry, request)
    assert plan["credential_scopes"] == ["ai.audio.transcribe", "ai.generate"]
    assert registry.snapshot() == before
    payload = _execute_payload(INTERACTIVE_EFFECT_SPECS["provider_configure"], request, plan)
    metadata = _presentation_metadata(
        INTERACTIVE_EFFECT_SPECS["provider_configure"],
        SimpleNamespace(request_digest=canonical_digest(payload), normalized_payload=payload),
    )
    assert "ai.audio.transcribe" in metadata["detail"]
    assert "ai.generate" in metadata["detail"]
    assert request["key_value"] not in str(metadata)
    with pytest.raises(InteractiveEffectUnavailable):
        _execute_payload(INTERACTIVE_EFFECT_SPECS["provider_configure"], request,
                         {**plan, "credential_scopes": ["ai.audio.speech"]})


@pytest.mark.parametrize("capabilities", [[], ["*"], ["ai.generate", "ai.generate"],
                                          [True], [None], "ai.audio.speech"])
def test_invalid_configuration_scopes_never_produce_a_plan(
    tmp_path: Path, capabilities: Any,
) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    with pytest.raises(ValueError):
        prepare_configuration(registry, {**_request(), "capabilities": capabilities})
    assert registry.snapshot()["revision"] == 0


def test_omitted_capabilities_keep_existing_text_only_credential_behavior(tmp_path: Path) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    request = _request()
    plan = prepare_configuration(registry, request)
    assert "credential_scopes" not in plan
    captured: list[dict[str, Any]] = []
    client = _Client(tmp_path)
    invoke = client.invoke

    def capture(contract: str, operation: str, payload: dict[str, Any]) -> Any:
        captured.append(payload)
        return invoke(contract, operation, payload)

    client.invoke = capture  # type: ignore[method-assign]
    execute_configuration(registry, client, {"request": request, "plan": plan},
                          consumer_pack_id="fixture.consumer")
    assert captured[0]["scopes"] == ["ai.generate", "ai.stream"]


def test_audio_scope_prepare_cannot_upgrade_a_previous_approval(tmp_path: Path) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    original = _request()
    plan = prepare_configuration(registry, original)
    client = _Client(tmp_path)
    with pytest.raises(PermissionError):
        execute_configuration(registry, client,
                              {"request": {**original, "capabilities": ["ai.audio.speech"]}, "plan": plan},
                              consumer_pack_id="fixture.consumer")
    assert client.calls == []


def test_audio_configuration_preserves_frozen_public_identity(tmp_path: Path) -> None:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = {**_request(), "display_name": "仕事用 API", "catalog_provider_id": "openai",
               "capabilities": ["ai.generate", "ai.audio.transcribe"]}
    plan = prepare_configuration(registry, request)
    assert plan["credential_scopes"] == ["ai.audio.transcribe", "ai.generate"]
    with pytest.raises(PermissionError, match="changed after preparation"):
        execute_configuration(
            registry, client,
            {"request": {**request, "capabilities": ["ai.audio.speech"]}, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == []
    execute_configuration(registry, client, {"request": request, "plan": plan},
                          consumer_pack_id="fixture.consumer")
    saved = registry.snapshot()["providers"][0]
    assert saved["display_name"] == request["display_name"]
    assert saved["metadata"] == {"catalog_provider_id": "openai"}
    assert request["key_value"] not in registry.path.read_text()
