from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEFAULTSPACK = ROOT / "ecosystem" / "defaultspack"
if str(DEFAULTSPACK) not in sys.path:
    sys.path.insert(0, str(DEFAULTSPACK))

from domain.ai_client.openai_compatible_connections import (  # noqa: E402
    connection_status,
    delete_connection,
    get_connection,
    list_connections,
    save_connection,
    save_connection_auth,
    select_connection,
    selected_connection,
)
from domain.ai_client.providers.generic_openai_compatible_provider import (  # noqa: E402
    GenericOpenAICompatibleProvider,
)


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def _connection(connection_id="alpha", **updates):
    value = {
        "connection_id": connection_id,
        "label": connection_id.title(),
        "base_url": "https://example.test/v1",
        "auth_mode": "none",
        "manual_models": [],
        "model_list": {"enabled": False},
    }
    value.update(updates)
    return value


def test_connection_store_has_stable_ids_and_never_persists_secrets(tmp_path):
    saved = save_connection(_connection(manual_models=["exact/model:tag"]), pack_root=tmp_path)
    assert saved["connection_id"] == "alpha"
    assert list_connections(pack_root=tmp_path)[0]["manual_models"] == ["exact/model:tag"]
    serialized = (tmp_path / "user_data" / "shared" / "openai_compatible_connections.json").read_text(encoding="utf-8")
    assert "exact/model:tag" in serialized
    with pytest.raises(ValueError, match="secret"):
        save_connection({**_connection(), "api_key": "do-not-save"}, pack_root=tmp_path)
    assert delete_connection("alpha", pack_root=tmp_path) is True


def test_connections_keep_credentials_in_secret_storage_and_select_runtime_endpoint(
    tmp_path,
):
    save_connection(_connection("first"), pack_root=tmp_path)
    save_connection(
        _connection(
            "second",
            base_url="https://second.test/v1",
            auth_mode="bearer",
        ),
        pack_root=tmp_path,
    )
    save_connection_auth("second", "secret-second", pack_root=tmp_path)
    selected = select_connection("second", pack_root=tmp_path)

    assert selected["connection_id"] == "second"
    assert selected_connection(pack_root=tmp_path)["base_url"] == "https://second.test/v1"
    status = connection_status(pack_root=tmp_path)
    assert status["selected_connection_id"] == "second"
    assert [item["credential_configured"] for item in status["connections"]] == [False, True]
    assert "secret-second" not in (
        tmp_path / "user_data" / "shared" / "openai_compatible_connections.json"
    ).read_text(encoding="utf-8")

    provider = GenericOpenAICompatibleProvider(
        get_connection("second", pack_root=tmp_path), pack_root=tmp_path
    )
    assert provider._headers()["Authorization"] == "Bearer secret-second"
    assert delete_connection("second", pack_root=tmp_path) is True
    assert connection_status(pack_root=tmp_path)["selected_connection_id"] == "first"


def test_ai_client_resolves_a_saved_connection_model_without_prefix_leakage(monkeypatch):
    from domain.ai_client import openai_compatible_connections
    from domain.ai_client.client import AIClient

    connection = _connection("selected", auth_mode="none", manual_models=["model-x"])
    monkeypatch.setattr(openai_compatible_connections, "selected_connection", lambda: None)
    monkeypatch.setattr(
        openai_compatible_connections,
        "get_connection",
        lambda connection_id: connection if connection_id == "selected" else None,
    )
    AIClient._instance = None
    try:
        provider, model_name = AIClient().resolve_provider(
            "openai_compatible/selected:model-x"
        )
    finally:
        AIClient._instance = None

    assert isinstance(provider, GenericOpenAICompatibleProvider)
    assert provider.connection_id == "selected"
    assert model_name == "model-x"


def test_connection_settings_route_is_registered_and_secret_mutations_are_sensitive():
    from ecosystem.defaultspack.transport.registry import canonical_http_route_specs

    routes = {
        (route.method, route.pattern): route for route in canonical_http_route_specs()
    }
    read_route = routes[("GET", "/api/connections/openai-compatible")]
    write_route = routes[("POST", "/api/connections/openai-compatible")]

    assert read_route.block_module == "blocks.connections.openai_compatible"
    assert write_route.block_module == "blocks.connections.openai_compatible"
    assert write_route.sensitive is True


@pytest.mark.parametrize(
    "field, value",
    [
        ("base_url", "https://token@example.test/v1"),
        ("base_url", "https://example.test/v1?api_key=secret"),
        ("base_url", "https://example.test/v1#secret"),
        ("model_list", {"enabled": True, "url": "https://token@example.test/models"}),
    ],
)
def test_connection_store_rejects_embedded_endpoint_secrets(
    tmp_path, field, value,
):
    definition = _connection()
    definition[field] = value

    with pytest.raises(ValueError, match="credentials|query or fragment"):
        save_connection(definition, pack_root=tmp_path)

    assert not (tmp_path / "user_data").exists()


def test_manual_inventory_keeps_unknown_capabilities_unknown():
    models = GenericOpenAICompatibleProvider(_connection(manual_models=["opaque-model"])).list_models()
    assert models[0]["id"] == "openai_compatible/alpha:opaque-model"
    assert models[0]["type"] == "unknown"
    assert models[0]["capabilities"]["streaming"] is None
    assert models[0]["metadata"]["capability_confidence"] == "unknown"


def test_configured_model_list_paginates_and_preserves_exact_ids(monkeypatch, tmp_path):
    requests = []

    def urlopen(request, **_kwargs):
        requests.append(request.full_url)
        if "after=page-2" in request.full_url:
            return _Response({"result": {"models": [{"id": "Case/Two:Q4"}], "next": None}})
        return _Response({"result": {"models": [{"id": "org/One"}], "next": "page-2"}})

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    monkeypatch.setattr(GenericOpenAICompatibleProvider, "_remote_model_cache_path", lambda self: tmp_path / f"{self.connection_id}.json")
    provider = GenericOpenAICompatibleProvider(_connection(model_list={"enabled": True, "url": "https://catalog.test/models", "items_path": "result.models", "next_path": "result.next", "cursor_param": "after", "max_pages": 3}))
    assert [item["model_id"] for item in provider.list_models()] == ["org/One", "Case/Two:Q4"]
    assert requests == ["https://catalog.test/models", "https://catalog.test/models?after=page-2"]


def test_auth_modes_and_connection_cache_paths_are_isolated(monkeypatch):
    monkeypatch.setenv("ALPHA_KEY", "secret-alpha")
    bearer = GenericOpenAICompatibleProvider(_connection(api_key_env="ALPHA_KEY", auth_mode="bearer"))
    header = GenericOpenAICompatibleProvider(_connection("beta", api_key_env="ALPHA_KEY", auth_mode="api_key_header", auth_header="X-Token"))
    assert bearer._headers()["Authorization"] == "Bearer secret-alpha"
    assert header._headers()["X-Token"] == "secret-alpha"
    assert bearer._remote_model_cache_path() != header._remote_model_cache_path()
    assert "secret-alpha" not in str(bearer._remote_model_cache_path())


def test_component_removes_invented_generic_default():
    from domain.ai_client.providers import get_all_known_models, get_provider_catalog_map
    from domain.components.registry import get_domain_component_registry

    get_domain_component_registry(force_reload=True)
    entry = get_provider_catalog_map()["openai_compatible"]
    assert entry["default_model"] == ""
    assert entry["availability"]["supports_invoke"] is True
    assert get_all_known_models("openai_compatible") == []
    assert not (ROOT / "ecosystem" / "rumi_model_catalog_pack" / "extensions" / "llm" / "providers" / "openai_compatible" / "manifest.json").exists()
