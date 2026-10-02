"""Captured model configuration stays inside its selected runtime Profile."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_model_registry_pack.runtime.process import ModelRegistryHostFactoryV4
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry


class _ProviderRegistryClient:
    """Expose one redacted, Profile-local registry snapshot to the host test."""

    def __init__(self, registry: ProviderRegistry) -> None:
        self.registry = registry
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def invoke(
        self,
        contract_id: str,
        operation_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((contract_id, operation_id, dict(payload)))
        return self.registry.snapshot()


def _provider_registry(
    tmp_path: Path,
    *,
    enabled: bool = True,
    provider_instance_id: str = "provider.fixture",
) -> ProviderRegistry:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    registry.save({
        "provider_instance_id": provider_instance_id,
        "adapter_id": "openai-compatible",
        "endpoint": "https://provider.example/v1",
        "credential_handle": "credential:fixture",
        "enabled": enabled,
    }, expected_revision=0)
    return registry


def _save_payload(
    provider_registry_revision: int,
    *,
    provider: str = "provider.fixture",
    expected_revision: int = 0,
) -> dict[str, Any]:
    return {
        "operation": "save",
        "expected_revision": expected_revision,
        "provider_registry_revision": provider_registry_revision,
        "record": {
            "model_profile_id": "daily",
            "model_id": "provider/model",
            "metadata": {"provider_connection_id": provider},
        },
    }


def _invoke(
    tmp_path: Path,
    kind: str = "manage",
    client: _ProviderRegistryClient | None = None,
) -> Any:
    function = f"rumi_model_registry_pack.model-registry.{kind}"
    operation = "rumi_model_registry_pack.model-profile-" + ("manage" if kind == "manage" else "resource")
    contract = "tobkiri.action.ai.model.profile.manage.v1" if kind == "manage" else "tobkiri.resource.ai.model.profile.v1"
    binding = SimpleNamespace(
        function=SimpleNamespace(function_id=function, implementation_digest="sha256:impl"),
        operation=SimpleNamespace(contract_id=contract, operation_id=operation, contract_version="1.0.0"),
        principal_ref=SimpleNamespace(value="principal"), artifact=SimpleNamespace(digest="sha256:pack"),
    )
    context = SimpleNamespace(
        user_data_root=tmp_path, profile_id="defaults", provider_bindings=(binding,),
        domain_ids={(contract, operation, "principal"): "domain"},
    )
    captured = ModelRegistryHostFactoryV4(function).capture(context)
    invocation = SimpleNamespace(
        contract_client=lambda **kwargs: client,
        assert_current=lambda: None,
    )
    return lambda payload: captured.contributions[0].invoke(operation, payload, invocation)


def test_model_configuration_uses_captured_profile_and_owner_revision(tmp_path: Path) -> None:
    provider_registry = _provider_registry(tmp_path)
    client = _ProviderRegistryClient(provider_registry)
    invoke = _invoke(tmp_path, client=client)
    saved = invoke(_save_payload(provider_registry.snapshot()["revision"]))
    assert saved["store_revision"] == 1
    invoke({"operation": "alias.set", "expected_revision": 1, "alias": "default", "target_profile_id": "daily"})
    assert _invoke(tmp_path, "profile")({"identifier": "default"})["resolved_profile_id"] == "daily"
    assert len(_invoke(tmp_path, "profile")({"operation": "list"})["profiles"]) == 1
    assert ModelRegistry("other", user_data_root=tmp_path).snapshot()["profiles"] == []
    assert client.calls == [(
        "tobkiri.resource.ai.provider.registry.v1",
        "rumi_provider_registry_pack.provider-registry-resource",
        {},
    )]


def test_model_configuration_uses_the_exact_registered_connection_id(
    tmp_path: Path,
) -> None:
    provider_instance_id = "connection/openai:main"
    provider_registry = _provider_registry(
        tmp_path,
        provider_instance_id=provider_instance_id,
    )
    invoke = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))

    saved = invoke(_save_payload(
        provider_registry.snapshot()["revision"],
        provider=provider_instance_id,
    ))

    assert saved["profile"]["metadata"] == {
        "provider_connection_id": provider_instance_id,
    }


@pytest.mark.parametrize(
    ("provider", "revision"),
    [
        ("provider.unknown", 1),
        ("provider.fixture", 0),
    ],
)
def test_model_configuration_rejects_unknown_or_stale_provider_connection(
    tmp_path: Path,
    provider: str,
    revision: int,
) -> None:
    provider_registry = _provider_registry(tmp_path)
    invoke = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))

    with pytest.raises(PermissionError, match="provider connection"):
        invoke(_save_payload(revision, provider=provider))

    assert ModelRegistry("defaults", user_data_root=tmp_path).snapshot()["profiles"] == []


def test_model_configuration_rejects_a_disabled_provider_connection(tmp_path: Path) -> None:
    provider_registry = _provider_registry(tmp_path, enabled=False)
    invoke = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))

    with pytest.raises(PermissionError, match="provider connection"):
        invoke(_save_payload(provider_registry.snapshot()["revision"]))

    assert ModelRegistry("defaults", user_data_root=tmp_path).snapshot()["profiles"] == []


def test_existing_model_configuration_revalidates_provider_connection(tmp_path: Path) -> None:
    """An identical route cannot bypass a later provider registry change."""
    provider_registry = _provider_registry(tmp_path)
    invoke = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))

    invoke(_save_payload(provider_registry.snapshot()["revision"]))
    provider_registry.save({
        "provider_instance_id": "provider.fixture",
        "adapter_id": "openai-compatible",
        "endpoint": "https://provider.example/v1",
        "credential_handle": "credential:fixture",
        "enabled": False,
    }, expected_revision=provider_registry.snapshot()["revision"])

    with pytest.raises(PermissionError, match="provider connection"):
        invoke(_save_payload(1, expected_revision=1))
    with pytest.raises(PermissionError, match="provider connection"):
        invoke(_save_payload(2, expected_revision=1))

    snapshot = ModelRegistry("defaults", user_data_root=tmp_path).snapshot()
    assert snapshot["revision"] == 1
    assert len(snapshot["profiles"]) == 1


def test_identical_model_configuration_revalidates_without_rewriting(
    tmp_path: Path,
) -> None:
    """A current identical route is validated but does not create history churn."""
    provider_registry = _provider_registry(tmp_path)
    client = _ProviderRegistryClient(provider_registry)
    invoke = _invoke(tmp_path, client=client)

    first = invoke(_save_payload(provider_registry.snapshot()["revision"]))
    second = invoke(_save_payload(
        provider_registry.snapshot()["revision"],
        expected_revision=first["store_revision"],
    ))

    assert second == first
    assert ModelRegistry("defaults", user_data_root=tmp_path).snapshot()["revision"] == 1
    assert len(client.calls) == 2, "owner-side Provider validation still runs"


@pytest.mark.parametrize("change", [
    {"profile_id": "other"}, {"profile_id": None}, {"approved": True},
    {"expected_revision": True}, {"expected_revision": "0"},
    {"expected_revision": None}, {"expected_revision": -1},
    {"user_data_root": "/tmp/foreign"},
])
def test_model_configuration_rejects_foreign_authority_and_bad_revision(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    with pytest.raises(PermissionError):
        _invoke(tmp_path)({
            "operation": "save", "expected_revision": 0,
            "provider_registry_revision": 0,
            "record": {"model_profile_id": "daily", "model_id": "provider/model"},
            **change,
        })
    assert not (tmp_path / "packs").exists()


def test_model_read_rejects_foreign_profile_and_write_fields(tmp_path: Path) -> None:
    for payload in ({"operation": "list", "profile_id": "other"}, {"operation": "list", "record": {}}):
        with pytest.raises(PermissionError):
            _invoke(tmp_path, "profile")(payload)
    assert not (tmp_path / "packs").exists()
