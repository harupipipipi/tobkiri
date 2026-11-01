"""Captured model configuration stays inside its selected runtime Profile."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.defaultspack.defaultspack.model_profile_presentation import (
    normalize_model_profile_save,
    present_model_profile_saved,
    present_model_profiles,
)
from ecosystem.rumi_model_registry_pack.runtime.process import ModelRegistryHostFactoryV4
from ecosystem.rumi_model_registry_pack.runtime.registry import (
    ModelRegistry,
    ModelRegistryConflict,
)
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


def _create_payload(expected_revision: int) -> dict[str, Any]:
    return normalize_model_profile_save({
        "model_profile_id": "daily",
        "model_id": "provider/model",
        "provider_instance_id": "provider.fixture",
        "display_name": "Daily",
        "expected_revision": expected_revision,
        "provider_registry_revision": 1,
    })


@pytest.mark.parametrize("provider_in_requirements", [False, True])
def test_repeated_create_preserves_full_configuration_and_redaction(
    tmp_path: Path, provider_in_requirements: bool,
) -> None:
    """A lossy selector projection cannot replace an owner's rich record."""
    provider_registry = _provider_registry(tmp_path)
    client = _ProviderRegistryClient(provider_registry)
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    record = {
        **_create_payload(0)["record"],
        "requirements": {"tool_calling": True, "minimum_context": 32000},
        "parameters": {"temperature": 0.25, "max_tokens": 4096},
        "credential_handle": "credential:opaque-model-binding",
    }
    record["metadata"]["purpose"] = "rich route"
    if provider_in_requirements:
        record["metadata"].pop("provider_connection_id")
        record["requirements"]["preferred_provider_instance_id"] = "provider.fixture"
    registry.save(record, expected_revision=0)
    before = registry.path.read_bytes()
    confirmed = registry.create(_create_payload(1)["record"], expected_revision=1)
    assert confirmed["profile"] == registry.get("daily")

    saved = _invoke(tmp_path, client=client)(_create_payload(1))

    assert saved["profile"] == {
        **_create_payload(1)["record"], "enabled": True,
    }
    assert registry.path.read_bytes() == before
    assert saved["store_revision"] == 1
    assert len(client.calls) == 1
    # The captured owner response is journaled before the UI's projection.
    from tobkiri_protocol.canonical import canonical_digest

    assert canonical_digest(saved).startswith("sha256:")
    projected = present_model_profile_saved(saved)
    assert projected["profiles"][0]["provider_id"] == "provider.fixture"
    assert not any(key in str(projected) for key in (
        "credential_handle", "parameters", "requirements", "purpose",
    ))


@pytest.mark.parametrize("change", [
    {"enabled": False}, {"model_id": "another/model"},
    {"display_name": "Another"},
    {"metadata": {"provider_connection_id": "another.provider"}},
])
def test_create_rejects_reserved_ids_including_hidden_disabled_records(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    registry.save({
        **_create_payload(0)["record"], "parameters": {"temperature": 0.25},
        "credential_handle": "opaque:binding", **change,
    }, expected_revision=0)
    before = registry.path.read_bytes()
    if change.get("enabled") is False:
        assert present_model_profiles(registry.snapshot())["profiles"] == []
    provider_registry = _provider_registry(tmp_path)

    with pytest.raises(ModelRegistryConflict, match="reserved"):
        _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))(
            _create_payload(1)
        )

    assert registry.path.read_bytes() == before


@pytest.mark.parametrize("enabled", [False, True])
def test_repeated_create_revalidates_provider_before_confirming_identity(
    tmp_path: Path, enabled: bool,
) -> None:
    provider_registry = _provider_registry(tmp_path)
    client = _ProviderRegistryClient(provider_registry)
    invoke = _invoke(tmp_path, client=client)
    invoke(_create_payload(0))
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    before = registry.path.read_bytes()
    provider_registry.save({
        **provider_registry.snapshot()["providers"][0], "enabled": enabled,
    }, expected_revision=1)

    with pytest.raises(PermissionError, match="provider connection"):
        invoke(_create_payload(1))
    if not enabled:
        with pytest.raises(PermissionError, match="provider connection"):
            invoke({**_create_payload(1), "provider_registry_revision": 2})

    assert registry.path.read_bytes() == before


def test_explicit_full_save_can_update_and_reenable_a_reserved_profile(
    tmp_path: Path,
) -> None:
    provider_registry = _provider_registry(tmp_path)
    invoke = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    registry.save({**_create_payload(0)["record"], "enabled": False}, expected_revision=0)
    payload = _create_payload(1)
    payload["operation"] = "save"
    payload["record"].update({
        "enabled": True, "model_id": "changed/model",
        "parameters": {"temperature": 0.5},
    })

    saved = invoke(payload)

    assert saved["store_revision"] == 2
    assert saved["profile"]["enabled"] is True
    assert saved["profile"]["model_id"] == "changed/model"
    assert saved["profile"]["parameters"] == {"temperature": 0.5}


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


def _disabled_route_registry(tmp_path: Path) -> ModelRegistry:
    """Create an imported disabled route without granting execution authority."""
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "provider/model",
            "enabled": False,
            "requirements": {"tool_calling": True},
            "parameters": {"temperature": 0.2},
            "metadata": {"provider_connection_id": "provider.fixture", "note": "keep"},
        },
        expected_revision=0,
    )
    return registry


def _route_registration(**changes: Any) -> dict[str, Any]:
    """Use the same public normalization as an explicit model registration."""
    return normalize_model_profile_save(
        {
            "model_profile_id": "daily",
            "model_id": "provider/model",
            "provider_instance_id": "provider.fixture",
            "display_name": "Daily",
            "max_output_tokens": 2048,
            "expected_revision": 1,
            "provider_registry_revision": 1,
            **changes,
        }
    )


def test_explicit_registration_activates_only_the_exact_disabled_route(
    tmp_path: Path,
) -> None:
    """Validated registration makes its saved route usable and keeps policy."""
    provider_registry = _provider_registry(tmp_path)
    registry = _disabled_route_registry(tmp_path)
    registry.save(
        {
            "model_profile_id": "unrelated",
            "model_id": "another/model",
            "enabled": False,
        },
        expected_revision=1,
    )
    unrelated = registry.get("unrelated")
    before = registry.snapshot()
    assert present_model_profiles(before)["profiles"] == []
    assert registry.snapshot() == before, "reading must never activate profiles"
    client = _ProviderRegistryClient(provider_registry)
    invoke = _invoke(tmp_path, client=client)

    saved = invoke(_route_registration(expected_revision=2))

    assert present_model_profile_saved(saved)["profiles"] == [
        {
            "profile_id": "daily",
            "display_name": "Daily",
            "model_id": "provider/model",
            "max_output_tokens": 2048,
            "provider_id": "provider.fixture",
            "route_configured": True,
        }
    ]
    assert saved["profile"]["enabled"] is True
    assert saved["profile"]["requirements"] == {"tool_calling": True}
    assert saved["profile"]["parameters"] == {"temperature": 0.2, "max_tokens": 2048}
    assert saved["profile"]["metadata"]["note"] == "keep"
    assert registry.get("unrelated") == unrelated
    assert invoke(_route_registration(expected_revision=3)) == saved
    assert len(client.calls) == 2, "even repeated saves revalidate the provider"


@pytest.mark.parametrize("existing_binding", ["metadata", "requirements"])
def test_explicit_registration_uses_the_exact_existing_connection_binding(
    tmp_path: Path,
    existing_binding: str,
) -> None:
    """Legacy preferred-connection bindings may activate only their same route."""
    provider_registry = _provider_registry(tmp_path)
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    binding = (
        {"metadata": {"provider_connection_id": "provider.fixture"}}
        if existing_binding == "metadata"
        else {"requirements": {"preferred_provider_instance_id": "provider.fixture"}}
    )
    registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "provider/model",
            "enabled": False,
            **binding,
        },
        expected_revision=0,
    )
    saved = _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))(
        _route_registration(),
    )
    assert saved["profile"]["enabled"] is True


@pytest.mark.parametrize(
    "existing_changes",
    [
        {"model_id": "unrelated/model"},
        {"metadata": {"provider_connection_id": "provider.other"}},
        {"metadata": {}},
        {
            "metadata": {"provider_connection_id": "provider.other"},
            "requirements": {"preferred_provider_instance_id": "provider.fixture"},
        },
    ],
)
def test_explicit_registration_rejects_a_different_existing_route(
    tmp_path: Path,
    existing_changes: dict[str, Any],
) -> None:
    """A hidden disabled ID collision cannot repoint or enable another route."""
    provider_registry = _provider_registry(tmp_path)
    registry = ModelRegistry("defaults", user_data_root=tmp_path)
    registry.save(
        {
            "model_profile_id": "daily",
            "model_id": "provider/model",
            "enabled": False,
            "metadata": {"provider_connection_id": "provider.fixture"},
            **existing_changes,
        },
        expected_revision=0,
    )
    before = registry.snapshot()
    with pytest.raises(ValueError, match="route identity changed"):
        _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))(
            _route_registration(),
        )
    assert registry.snapshot() == before


@pytest.mark.parametrize(
    "failure",
    ["model_revision", "provider_revision", "disabled_provider", "unknown_provider"],
)
def test_explicit_activation_retains_model_cas_and_provider_validation(
    tmp_path: Path,
    failure: str,
) -> None:
    """Activation failures leave the disabled registry and owner revision intact."""
    provider_registry = _provider_registry(
        tmp_path,
        enabled=failure != "disabled_provider",
    )
    registry = _disabled_route_registry(tmp_path)
    before = registry.snapshot()
    changes = {
        "model_revision": {"expected_revision": 0},
        "provider_revision": {"provider_registry_revision": 0},
        "disabled_provider": {},
        "unknown_provider": {"provider_instance_id": "provider.unknown"},
    }[failure]
    error = ModelRegistryConflict if failure == "model_revision" else PermissionError
    with pytest.raises(error):
        _invoke(tmp_path, client=_ProviderRegistryClient(provider_registry))(
            _route_registration(**changes),
        )
    assert registry.snapshot() == before
