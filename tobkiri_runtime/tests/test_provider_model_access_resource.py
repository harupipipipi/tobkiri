"""Real registry and envelope boundaries for public model access reads."""

from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest

from tobkiri_host.broker import RequestEnvelope
from ecosystem.rumi_provider_registry_pack.runtime.registry import ProviderRegistry
from ecosystem.rumi_provider_registry_pack.runtime.model_access_resource import (
    ModelAccessResourceHostFactoryV4,
    READ_FUNCTION,
    CATALOG_FUNCTION,
    CONTRACT_ID,
    READ_OPERATION,
    CATALOG_OPERATION,
    MODEL_CATALOG_CONTRACT,
    MODEL_CATALOG_OPERATION,
    MODEL_CATALOG_PROVIDER,
)
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import CAPABILITY_REVISION


def harness(tmp_path: Path, *, provider: str = "openrouter") -> tuple[Any, ...]:
    registry = ProviderRegistry("defaults", user_data_root=tmp_path)
    registry.save(
        {
            "provider_instance_id": "connection/main",
            "display_name": "Main",
            "adapter_id": "openai-compatible",
            "enabled": True,
            "endpoint": "https://openrouter.ai/api/v1",
            "credential_handle": "credential:synthetic-private-marker",
            "metadata": {"catalog_provider_id": provider},
        },
        expected_revision=0,
    )
    bindings = tuple(
        NS(
            function=NS(function_id=READ_FUNCTION if operation == READ_OPERATION else CATALOG_FUNCTION, implementation_digest="digest"),
            operation=NS(contract_id=CONTRACT_ID, operation_id=operation, contract_version="1.0.0"),
            principal_ref=NS(value="principal"),
            artifact=NS(digest="artifact"),
        )
        for operation in (READ_OPERATION, CATALOG_OPERATION)
    )
    context = NS(
        user_data_root=tmp_path,
        profile_id="defaults",
        provider_bindings=bindings,
        plan_digest="plan",
        security_epoch=1,
        domain_ids={
            (CONTRACT_ID, operation, "principal"): "domain"
            for operation in (READ_OPERATION, CATALOG_OPERATION)
        },
    )
    contributions = {}
    for binding in bindings:
        captured = NS(**vars(context))
        captured.provider_bindings = (binding,)
        factory = ModelAccessResourceHostFactoryV4(binding.operation.operation_id)
        contributions.update({item.operation_id: item
                              for item in factory.capture(captured).contributions})
    calls = []
    catalog = {
        "providers": [{"provider_id": provider}],
        "models": [
            {"provider_id": provider, "provider_model_id": "model-a", "display_name": "A"},
            {"provider_id": "foreign", "provider_model_id": "foreign-model"},
        ],
        "inventory": {provider: {"source": "openrouter_models_api", "stale": False}},
    }

    class Client:
        def invoke(self, *args: Any, **kwargs: Any) -> Any:
            calls.append((args, kwargs))
            return catalog

    def invocation(operation: str) -> Any:
        envelope = object.__new__(RequestEnvelope)
        for key, value in {
            "contract_id": CONTRACT_ID,
            "operation_id": operation,
            "target_principal": NS(value="principal"),
            "target_domain": NS(value="domain"),
            "context": NS(profile_id="defaults", plan_digest="plan", security_epoch=1),
        }.items():
            object.__setattr__(envelope, key, value)

        def client(**kwargs: Any) -> Any:
            assert kwargs == {
                "allowed_contract_ids": frozenset({MODEL_CATALOG_CONTRACT}),
                "consumer_pack_id": "rumi_provider_registry_pack",
                "include_credentials": False,
            }
            return Client()

        return NS(envelope=envelope, assert_current=lambda: None, contract_client=client)

    def invoke(operation: str, extra: dict | None = None, inv: Any = None) -> Any:
        return contributions[operation].invoke(
            operation,
            {
                "profile_id": "defaults",
                "provider_instance_id": "connection/main",
                **(extra or {}),
            },
            inv or invocation(operation),
        )

    return registry, context, invoke, invocation, calls, catalog


def test_read_projects_exact_scope_and_reviewed_official_capability(tmp_path: Path) -> None:
    registry, _, invoke, _, calls, _ = harness(tmp_path)
    before = registry.path.read_bytes()
    result = invoke(READ_OPERATION)
    assert result["profile_id"] == "defaults"
    assert result["provider_instance_id"] == "connection/main"
    assert result["native_capability"] == CAPABILITY_REVISION
    assert set(result) == {
        "profile_id",
        "provider_instance_id",
        "registry_revision",
        "model_access",
        "native_capability",
    }
    assert calls == []
    assert registry.path.read_bytes() == before
    assert "synthetic-private-marker" not in str(result)


def test_catalog_exact_nested_contract_and_no_provider_mix(tmp_path: Path) -> None:
    _, _, invoke, _, calls, catalog = harness(tmp_path)
    result = invoke(CATALOG_OPERATION, {"discovery_filters": {"category": "programming"}})
    assert result["models"] == [{"model_id": "model-a", "display_name": "A"}]
    assert result["status"] == "live"
    assert calls == [
        (
            (
                MODEL_CATALOG_CONTRACT,
                MODEL_CATALOG_OPERATION,
                {"provider_id": "openrouter", "discovery_filters": {"category": "programming"}},
            ),
            {"provider_instance_id": MODEL_CATALOG_PROVIDER},
        )
    ]
    catalog["models"] = []
    assert invoke(CATALOG_OPERATION)["models"] == []
    assert invoke(CATALOG_OPERATION)["status"] == "live"
    catalog["inventory"]["openrouter"]["stale"] = True
    assert invoke(CATALOG_OPERATION)["status"] == "unavailable"


@pytest.mark.parametrize(
    "extra",
    [
        {"profile_id": "foreign"},
        {"provider_instance_id": "deleted"},
        {"approved": True},
        {"endpoint": "https://foreign.invalid"},
        {"credential_handle": "synthetic-private"},
        {"discovery_filters": {}},
    ],
)
def test_read_invalid_payload_fails_before_catalog(tmp_path: Path, extra: dict) -> None:
    _, _, invoke, _, calls, _ = harness(tmp_path)
    with pytest.raises(PermissionError):
        invoke(READ_OPERATION, extra)
    assert calls == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("profile_id", "foreign"),
        ("plan_digest", "stale"),
        ("security_epoch", 2),
    ],
)
def test_envelope_capture_fence(tmp_path: Path, field: str, value: Any) -> None:
    _, _, invoke, invocation, calls, _ = harness(tmp_path)
    inv = invocation(READ_OPERATION)
    setattr(inv.envelope.context, field, value)
    with pytest.raises(PermissionError):
        invoke(READ_OPERATION, inv=inv)
    assert calls == []


def test_principal_and_current_fence(tmp_path: Path) -> None:
    _, _, invoke, invocation, calls, _ = harness(tmp_path)
    inv = invocation(READ_OPERATION)
    inv.envelope.target_principal.value = "foreign"
    with pytest.raises(PermissionError):
        invoke(READ_OPERATION, inv=inv)
    inv = invocation(CATALOG_OPERATION)

    def stale() -> None:
        raise PermissionError("stale")

    inv.assert_current = stale
    with pytest.raises(PermissionError):
        invoke(CATALOG_OPERATION, inv=inv)
    assert calls == []


def test_capture_rejects_missing_domain(tmp_path: Path) -> None:
    _, context, *_ = harness(tmp_path)
    context.domain_ids = {}
    context.provider_bindings = (context.provider_bindings[0],)
    with pytest.raises(PermissionError):
        ModelAccessResourceHostFactoryV4().capture(context)


def test_static_catalog_is_live_and_native_discovery_requires_official_connection(
    tmp_path: Path,
) -> None:
    registry, _, invoke, _, calls, _ = harness(tmp_path, provider="openai")
    assert invoke(READ_OPERATION)["native_capability"] is None
    assert invoke(CATALOG_OPERATION)["status"] == "live"
    with pytest.raises(ValueError):
        invoke(CATALOG_OPERATION, {"discovery_filters": {"category": "programming"}})
    assert len(calls) == 1
    record = registry.snapshot()["providers"][0]
    registry.save(
        {
            **record,
            "metadata": {"catalog_provider_id": "openrouter"},
            "endpoint": "https://arbitrary.invalid/api/v1",
        },
        expected_revision=1,
    )
    assert invoke(READ_OPERATION)["native_capability"] is None
    with pytest.raises(ValueError):
        invoke(CATALOG_OPERATION, {"discovery_filters": {"category": "programming"}})
