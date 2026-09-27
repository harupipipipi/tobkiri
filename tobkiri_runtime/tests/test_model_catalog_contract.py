"""External-QA-oriented specifications for the Wave 5 model catalog."""

from __future__ import annotations

import json

import ecosystem.rumi_model_catalog_pack.runtime.catalog as catalog
import pytest

from ecosystem.defaultspack.backend.ai_client import provider_catalog
from ecosystem.rumi_model_catalog_pack.runtime.catalog import (
    CATALOG_REVISION,
    create_model_catalog_operation,
)

_FETCH_OPENROUTER_INVENTORY = catalog._fetch_openrouter_inventory
pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def isolate_openrouter_inventory(monkeypatch, tmp_path):
    """Keep catalog contract tests independent of the public network."""
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path / "user-data"))
    monkeypatch.setattr(catalog, "_OPENROUTER_MEMORY_INVENTORY", None)
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: [])


def _live_openrouter_model() -> dict[str, object]:
    return {
        "id": "acme/atlas-reasoner",
        "name": "Atlas Reasoner",
        "context_length": 262144,
        "architecture": {
            "input_modalities": ["text", "image"],
            "output_modalities": ["text"],
        },
        "supported_parameters": ["tools", "reasoning", "response_format"],
        "pricing": {"prompt": "0.000001", "completion": "0.000002"},
    }


def test_catalog_is_provider_neutral_and_credential_free() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {})

    assert result["catalog_revision"] == CATALOG_REVISION
    assert result["providers"]
    assert result["models"]
    for model in result["models"]:
        assert model["execution_provider_instance_id"].startswith("provider.")
        assert "credential" not in model
        assert "adapter" not in model


def test_catalog_filter_is_finite() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {"provider_id": "does-not-exist"})

    assert result["providers"] == []
    assert result["models"] == []


def test_expired_openrouter_free_variants_are_not_exposed_without_live_inventory() -> None:
    operation = create_model_catalog_operation(None)
    result = operation("list", {"provider_id": "openrouter"})
    models = {item["model_id"]: item for item in result["models"]}

    assert models == {}
    inventory = result["inventory"]["openrouter"]
    assert inventory["source"] == "unavailable"
    assert inventory["stale"] is False
    assert inventory["model_count"] == 0
    assert inventory["static_models_ignored"] > 0

    all_models = operation("list", {})["models"]
    assert all(item["provider_id"] != "openrouter" for item in all_models)
    assert any(item["provider_id"] != "openrouter" for item in all_models)


def test_openrouter_live_inventory_replaces_static_catalog(monkeypatch) -> None:
    captured = {}

    class Response:
        status = 200
        headers = {"Content-Length": "512"}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self, limit):
            assert limit == catalog._OPENROUTER_MAX_RESPONSE_BYTES + 1
            return json.dumps({"data": [_live_openrouter_model()]}).encode("utf-8")

    def urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(
        catalog,
        "_fetch_openrouter_inventory",
        _FETCH_OPENROUTER_INVENTORY,
    )

    operation = create_model_catalog_operation(None)
    all_models = operation("list", {})["models"]
    assert any(item["provider_id"] != "openrouter" for item in all_models)
    assert {
        item["model_id"] for item in all_models if item["provider_id"] == "openrouter"
    } == {"openrouter/acme/atlas-reasoner"}

    result = operation("list", {"provider_id": "openrouter"})
    models = {item["model_id"]: item for item in result["models"]}

    assert "openrouter/acme/atlas-reasoner" in models
    assert "openrouter/tencent/hy3:free" not in models
    assert "openrouter/tencent/hy3-preview:free" not in models
    assert models["openrouter/acme/atlas-reasoner"]["type"] == "reasoning"
    inventory = result["inventory"]["openrouter"]
    assert inventory["source"] == "live"
    assert inventory["stale"] is False
    assert inventory["model_count"] == 1
    assert inventory["static_models_ignored"] > 0
    assert captured["request"].full_url == (
        "https://openrouter.ai/api/v1/models?output_modalities=all"
    )
    assert captured["request"].get_header("Authorization") is None
    assert 1 <= captured["timeout"] <= 5


def test_openrouter_network_failure_uses_stale_last_known_good(monkeypatch) -> None:
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None
    catalog._save_openrouter_inventory_cache(
        {
            "version": 1,
            "saved_at": 1,
            "expires_at": 1,
            "models": [live_model],
        }
    )
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: [])

    result = create_model_catalog_operation(None)(
        "list", {"provider_id": "openrouter"}
    )
    models = {item["model_id"] for item in result["models"]}

    assert "openrouter/acme/atlas-reasoner" in models
    assert result["inventory"]["openrouter"]["source"] == "last_known_good"
    assert result["inventory"]["openrouter"]["stale"] is True


def test_runtime_catalog_keeps_live_openrouter_inventory_authoritative(monkeypatch) -> None:
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None

    class Client:
        @staticmethod
        def list_providers():
            return [{"provider_id": "openrouter"}]

        @staticmethod
        def list_models(provider=None):
            assert provider == "openrouter"
            return [live_model]

    monkeypatch.setattr(
        provider_catalog,
        "get_all_known_models",
        lambda provider_id=None, active_provider_ids=None: [
            {
                "provider_id": "openrouter",
                "qualified_model_id": "openrouter/static/model",
                "model_id": "openrouter/static/model",
            }
        ],
    )
    monkeypatch.setattr(provider_catalog, "_runtime_client", lambda: Client())
    monkeypatch.setattr(
        "core_runtime.resolved_profile_scope.effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )
    monkeypatch.setattr(
        "core_runtime.approval_manager.get_approval_manager",
        lambda: type(
            "ApprovedCatalog",
            (),
            {"is_pack_approved_and_verified": lambda self, pack_id: (True, "test")},
        )(),
    )
    provider_catalog._clear_runtime_inventory_cache()

    models = provider_catalog.list_model_catalog("openrouter")

    assert [model["qualified_model_id"] for model in models] == [
        "openrouter/acme/atlas-reasoner"
    ]
    assert [model["model_id"] for model in models] == ["acme/atlas-reasoner"]


@pytest.mark.parametrize("provider", ["", "openrouter"])
@pytest.mark.parametrize(
    "inventory_source", ["openrouter_models_api", "last_known_good_inventory"]
)
@pytest.mark.parametrize(
    "owner_state",
    [
        "unapproved",
        "approval_unavailable",
        "not_selected",
        "no_selection",
        "approved_unavailable",
    ],
)
def test_runtime_inventory_respects_selected_catalog_owner(
    monkeypatch, provider: str, inventory_source: str, owner_state: str
) -> None:
    """Runtime discovery cannot revive an unapproved owner's OpenRouter rows."""
    runtime_model = {
        "provider_id": "openrouter",
        "model_id": "runtime/available",
        "qualified_model_id": "openrouter/runtime/available",
        "metadata": {"source": inventory_source},
    }

    class RuntimeClient:
        _providers = {}

        @staticmethod
        def list_models(provider=None):
            return [runtime_model]

    class ApprovalManager:
        @staticmethod
        def is_pack_approved_and_verified(pack_id):
            assert pack_id == "rumi_model_catalog_pack"
            if owner_state == "approval_unavailable":
                raise RuntimeError("approval unavailable")
            return owner_state != "unapproved", "test"

    def unavailable(*_args, **_kwargs):
        raise provider_catalog.GlobalContractUnavailable("test")

    selected = (
        set()
        if owner_state == "no_selection"
        else {"other_pack"}
        if owner_state == "not_selected"
        else {"rumi_model_catalog_pack"}
    )
    monkeypatch.setattr(provider_catalog, "_invoke", unavailable)
    monkeypatch.setattr(provider_catalog, "_runtime_client", RuntimeClient)
    monkeypatch.setattr(
        provider_catalog,
        "get_all_known_models",
        lambda provider_id=None: [
            model
            for model in [
                {
                    "provider_id": "stub",
                    "model_id": "kept",
                    "qualified_model_id": "stub/kept",
                },
                {
                    "provider_id": "openrouter",
                    "model_id": "obsolete:free",
                    "qualified_model_id": "openrouter/obsolete:free",
                },
            ]
            if provider_id is None or model["provider_id"] == provider_id
        ],
    )
    monkeypatch.setattr(
        "core_runtime.resolved_profile_scope.effective_pack_ids",
        lambda: frozenset(selected),
    )
    monkeypatch.setattr(
        "core_runtime.approval_manager.get_approval_manager",
        lambda: ApprovalManager(),
    )
    provider_catalog._clear_runtime_inventory_cache()

    observed_ids = {
        model["qualified_model_id"]
        for model in provider_catalog.list_model_catalog(provider)
    }
    expected_ids = (
        {"stub/kept"} if not provider and owner_state != "not_selected" else set()
    )
    if owner_state == "approved_unavailable":
        expected_ids.add("openrouter/runtime/available")
    assert observed_ids == expected_ids


@pytest.mark.parametrize("provider", ["", "openrouter"])
@pytest.mark.parametrize("owner_result", ["unavailable", "empty", "unapproved", "live"])
def test_selected_catalog_fallback_never_restores_static_openrouter_models(
    monkeypatch, provider: str, owner_result: str
) -> None:
    """A missing owner inventory must not revive an expired free model."""
    static_models = [
        {
            "id": "stub/kept",
            "qualified_model_id": "stub/kept",
            "provider_id": "stub",
            "model_id": "kept",
        },
        {
            "id": "openrouter/obsolete:free",
            "qualified_model_id": "openrouter/obsolete:free",
            "provider_id": "openrouter",
            "model_id": "obsolete:free",
        },
    ]
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None

    class EmptyRuntimeClient:
        _providers = {}

        @staticmethod
        def list_models(provider=None):
            return []

    class ApprovalManager:
        @staticmethod
        def is_pack_approved_and_verified(pack_id):
            assert pack_id == "rumi_model_catalog_pack"
            return owner_result != "unapproved", "test"

    calls = 0

    def invoke(contract_id, name, payload):
        nonlocal calls
        calls += 1
        assert contract_id == provider_catalog._MODEL_CATALOG_CONTRACT
        assert name == "list"
        if owner_result == "unavailable":
            raise provider_catalog.GlobalContractUnavailable("test")
        if owner_result == "live" and calls == 2:
            assert payload == {"provider_id": "openrouter"}
            return {"models": [live_model]}
        return {"models": []}

    monkeypatch.setattr(provider_catalog, "_invoke", invoke)
    monkeypatch.setattr(provider_catalog, "_runtime_client", EmptyRuntimeClient)
    monkeypatch.setattr(
        provider_catalog,
        "get_all_known_models",
        lambda provider_id=None: [
            model
            for model in static_models
            if provider_id is None or model["provider_id"] == provider_id
        ],
    )
    monkeypatch.setattr(
        "core_runtime.resolved_profile_scope.effective_pack_ids",
        lambda: frozenset({"rumi_model_catalog_pack"}),
    )
    monkeypatch.setattr(
        "core_runtime.approval_manager.get_approval_manager",
        lambda: ApprovalManager(),
    )
    provider_catalog._clear_runtime_inventory_cache()

    models = provider_catalog.list_model_catalog(provider)
    observed_ids = {model["qualified_model_id"] for model in models}
    expected_ids = set() if provider else {"stub/kept"}
    if owner_result == "live":
        expected_ids.add("openrouter/acme/atlas-reasoner")
    assert observed_ids == expected_ids
    assert "openrouter/obsolete:free" not in observed_ids


def test_unfiltered_live_catalog_models_reach_profile_picker(monkeypatch) -> None:
    live_model = catalog._normalize_openrouter_model(_live_openrouter_model())
    assert live_model is not None
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: [live_model])
    operation = create_model_catalog_operation(None)

    def invoke(contract_id, name, payload):
        if contract_id == provider_catalog._MODEL_CATALOG_CONTRACT:
            return operation(name, payload)
        if contract_id == provider_catalog._MODEL_PROFILE_CONTRACT:
            return {
                "profiles": [
                    {
                        "profile_id": "stub/default",
                        "qualified_model_id": "stub/default",
                        "provider_id": "stub",
                        "model_id": "default",
                    }
                ]
            }
        raise AssertionError(contract_id)

    class EmptyRuntimeClient:
        _providers = {}

        @staticmethod
        def list_models(provider=None):
            return []

    monkeypatch.setattr(provider_catalog, "_invoke", invoke)
    monkeypatch.setattr(provider_catalog, "_runtime_client", EmptyRuntimeClient)
    provider_catalog._clear_runtime_inventory_cache()

    models = provider_catalog.list_model_catalog()
    profiles = provider_catalog.list_profile_catalog()
    openrouter_id = "openrouter/acme/atlas-reasoner"

    assert any(model["qualified_model_id"] == openrouter_id for model in models)
    assert any(
        profile["profile_id"] == openrouter_id
        and profile["model_id"] == "acme/atlas-reasoner"
        for profile in profiles
    )
