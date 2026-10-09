"""No network, credentials, or primary writes; exercise the candidate modules."""
import json

import pytest

from ecosystem.rumi_provider_registry_pack.runtime.model_access import (
    VERSION, compile_connection_parameters, effective_model_access,
    model_is_allowed, normalize_model_access,
)
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import (
    CAPABILITY_REVISION, discovery_url, normalize_native_filters,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry, ProviderRegistryConflict,
)
from ecosystem.rumi_provider_adapters_pack.runtime.adapter import (
    create_generate_operation, create_embedding_operation, create_image_operation,
    create_stream_operation,
)
from core_runtime.global_contract_dispatch import GlobalContractInvocationError


def connection(policy=None):
    record = {"provider_instance_id": "opaque.connection-a", "enabled": True,
              "adapter_id": "openai-compatible",
              "endpoint": "https://openrouter.ai/api/v1",
              "credential_handle": "opaque:already-bound",
              "metadata": {"catalog_provider_id": "openrouter"}}
    if policy is not None:
        record["model_access"] = policy
    return record


def policy(mode="explicit", ids=None, native=None):
    value = {"version": VERSION, "mode": mode, "model_ids": ids or []}
    if native is not None:
        value["native_filters"] = native
    return value


def native(discovery=None, routing=None):
    return {"revision": CAPABILITY_REVISION, "discovery": discovery or {},
            "routing": routing or {}}


def test_all_is_dynamic_without_catalog_snapshot_and_empty_explicit_denies():
    assert model_is_allowed(connection(policy("all")), "future/vendor-new-v99")
    assert not model_is_allowed(connection(policy()), "future/vendor-new-v99")
    assert not model_is_allowed(connection(policy(ids=["a/v1", "b/v2"])), "c/v3")
    assert model_is_allowed(connection(policy(ids=["a/v1", "b/v2"])), "b/v2")
    with pytest.raises(ValueError):
        normalize_model_access(policy("all", ["a/v1"]), connection=connection())


@pytest.mark.parametrize("mode", [
    ["all"], ["explicit"], {}, {"mode": "all"}, None,
    True, False, 0, 1, "unknown", " all ",
])
def test_policy_mode_requires_an_exact_string_enum(mode: object) -> None:
    """Malformed JSON modes raise the policy validation error consistently."""
    with pytest.raises(ValueError, match="model access mode is invalid"):
        normalize_model_access(
            {"version": VERSION, "mode": mode, "model_ids": []},
            connection=connection(),
        )


@pytest.mark.parametrize("ids", [[], ["stable/a", "stable/b"]])
def test_legacy_explicit_lists_are_preserved_even_empty(ids):
    record = connection()
    record["allowed_models"] = ids
    assert effective_model_access(record) == policy(ids=ids)
    assert effective_model_access(connection()) is None


def test_policy_persistence_revision_profiles_and_key_rotation(tmp_path):
    first = ProviderRegistry("default", user_data_root=tmp_path)
    first.save(connection(policy(ids=["a/v1", "a/v2"])), expected_revision=0)
    result = first.set_model_access("opaque.connection-a", policy(), expected_revision=1)
    assert result["store_revision"] == 2
    reloaded = ProviderRegistry("default", user_data_root=tmp_path)
    assert reloaded.snapshot()["providers"][0]["model_access"] == policy()
    reloaded.save(connection(), expected_revision=2)
    assert reloaded.snapshot()["providers"][0]["model_access"] == policy()
    assert reloaded.snapshot()["providers"][0]["credential_handle"] == "opaque:already-bound"
    other = ProviderRegistry("work", user_data_root=tmp_path)
    assert other.snapshot()["providers"] == []
    with pytest.raises(ProviderRegistryConflict):
        reloaded.set_model_access("opaque.connection-a", policy("all"), expected_revision=1)
    reloaded.delete("opaque.connection-a", expected_revision=3)
    with pytest.raises(KeyError):
        reloaded.set_model_access("opaque.connection-a", policy("all"), expected_revision=4)


@pytest.mark.parametrize("value", [
    {"version": VERSION, "mode": "explicit", "model_ids": [], "key_value": "blocked"},
    {"version": "future", "mode": "all"},
    {"version": VERSION, "mode": "explicit", "model_ids": ["a\nsecret"]},
    {"version": VERSION, "mode": "explicit", "model_ids": "all"},
])
def test_unknown_policy_fields_and_malformed_ids_rejected(value):
    with pytest.raises(ValueError):
        normalize_model_access(value, connection=connection())


@pytest.mark.parametrize("changes", [
    {"endpoint": "https://api.openai.com/v1"},
    {"endpoint": "https://openrouter.ai.example/api/v1"},
    {"endpoint": "https://openrouter.ai/api/v1?unknown=1"},
    {"metadata": {"catalog_provider_id": "anthropic"}},
    {"adapter_id": "anthropic"},
])
def test_native_filters_never_cross_provider_or_endpoint(changes):
    with pytest.raises(ValueError):
        normalize_model_access(policy("all", native=native()),
                               connection={**connection(), **changes})


@pytest.mark.parametrize("filters", [
    {"revision": "old", "routing": {}},
    native(routing={"unsupported": True}),
    native(discovery={"zdr": True}),
    native(routing={"data_collection": "sometimes"}),
    native(routing={"max_price": {"prompt": float("nan")}}),
    native(routing={"max_price": {"completion": -1}}),
    native(routing={"max_price": {"completion": True}}),
    native(routing={"only": ["a"], "ignore": ["a"]}),
])
def test_unknown_obsolete_or_unsafe_native_filters_rejected(filters):
    with pytest.raises(ValueError):
        normalize_native_filters(filters, verified_openrouter=True)


def test_native_discovery_query_is_separate_from_generation_payload():
    filters = native({"output_modalities": ["image", "text"],
                      "category": "programming"},
                     {"only": ["xiaomi"], "zdr": True,
                      "max_price": {"prompt": .435, "completion": .87}})
    url = discovery_url(filters)
    assert "output_modalities=image%2Ctext" in url and "category=programming" in url
    assert "xiaomi" not in url and "max_price" not in url
    params = compile_connection_parameters(
        connection(policy("all", native=filters)), "vendor/model", {"temperature": .2},
    )
    assert params == {"temperature": .2, "provider": {
        "only": ["xiaomi"], "zdr": True, "max_price": {
            "prompt": .435, "completion": .87}, "allow_fallbacks": False}}
    assert "discovery" not in json.dumps(params)


@pytest.mark.parametrize("key", ["model", "models", "provider", "extra_body", "fallbacks", "endpoint"])
def test_request_parameters_cannot_bypass_model_provider_or_price_constraints(key):
    with pytest.raises(ValueError):
        compile_connection_parameters(connection(policy("all")), "stable/model", {key: "override"})


@pytest.mark.parametrize("factory,name", [
    (create_generate_operation, "generate"), (create_stream_operation, "stream"),
    (create_embedding_operation, "embed"), (create_image_operation, "generate"),
])
def test_all_adapter_entrypoints_deny_before_credentials_or_transport(factory, name):
    class FakeHost:
        def invoke(self, *_args, **_kwargs):
            return {"providers": [connection(policy(ids=["allowed/model"]))]}

        def post_json_with_credential(self, **_kwargs):
            raise AssertionError("a denied model must never reach transport")

    with pytest.raises(GlobalContractInvocationError) as exc:
        factory(FakeHost())(name, {"profile_id": "default", "provider_id": "openrouter",
                                  "provider_connection_id": "opaque.connection-a",
                                  "model_id": "openrouter/forbidden/model"})
    assert exc.value.code == "denied"


def test_native_routing_reaches_exact_host_transport_model():
    captured = {}

    class FakeHost:
        def invoke(self, *_args, **_kwargs):
            return {"providers": [connection(policy(ids=["vendor/model"], native=native(
                routing={"only": ["xiaomi"], "max_price": {"completion": .87}},
            )))]}

        def post_json_with_credential(self, **kwargs):
            captured.update(kwargs)
            return {"choices": [{"message": {"content": "test"}}], "usage": {}}

    create_generate_operation(FakeHost())("generate", {
        "provider_id": "openrouter", "provider_connection_id": "opaque.connection-a",
        "model_id": "openrouter/vendor/model", "messages": [],
    })
    body = captured.get("body", captured.get("payload"))
    assert body["model"] == "vendor/model"
    assert body["provider"] == {"only": ["xiaomi"], "max_price": {"completion": .87},
                                "allow_fallbacks": False}


@pytest.mark.parametrize("code", ["denied", "invalid_request"])
def test_gateway_does_not_replay_policy_failure_on_another_provider(code):
    import runpy
    from pathlib import Path
    from ecosystem.rumi_ai_gateway_pack.runtime import gateway

    # Reuse the repository's deterministic catalog/router fake, not its tests.
    repo = Path(__file__).resolve().parents[2]
    fixtures = runpy.run_path(str(repo / "tobkiri_runtime/tests/test_ai_gateway_pack.py"))

    class Host(fixtures["FakeContractClient"]):
        def invoke(self, contract_id, operation, payload, **kwargs):
            if contract_id == gateway.GENERATE_PROVIDER_CONTRACT:
                self.calls.append((contract_id, operation,
                                   kwargs.get("provider_instance_id"), payload))
                raise GlobalContractInvocationError(code, "saved policy rejected")
            if contract_id == gateway.FAILOVER_CONTRACT:
                raise AssertionError("policy denial must never request fallback")
            return super().invoke(contract_id, operation, payload, **kwargs)

    host = Host()
    with pytest.raises(GlobalContractInvocationError) as exc:
        gateway.create_generate_operation(host)("generate", {
            "messages": [], "idempotency_key": "isolated-test",
            "allow_failover": True, "requirements": {
                "modalities": ["text"], "preferred_model_id": "model-a"},
        })
    assert exc.value.code == code
    assert len([call for call in host.calls
                if call[0] == gateway.GENERATE_PROVIDER_CONTRACT]) == 1


def test_native_routing_on_unverified_modality_is_explicitly_incompatible():
    class Host:
        def invoke(self, *_args, **_kwargs):
            return {"providers": [connection(policy("all", native=native(
                routing={"only": ["xiaomi"]},
            )))]}

    with pytest.raises(GlobalContractInvocationError) as exc:
        create_embedding_operation(Host())("embed", {
            "provider_id": "openrouter", "provider_connection_id": "opaque.connection-a",
            "model_id": "vendor/model", "input": "test",
        })
    assert exc.value.code == "incompatible"


def test_finite_projection_never_exposes_key_handle_or_other_provider_models():
    import importlib.util
    from pathlib import Path
    base = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "isolated_model_access_presentation", base / "ecosystem/rumi_provider_registry_pack/runtime/model_access_presentation.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    snapshot = {"profile_id": "default", "revision": 2,
                "providers": [connection(policy(ids=["a/model"]))]}
    projected = module.project_model_access(
        snapshot, "opaque.connection-a", native_capability_confirmed=True,
    )
    assert set(projected) == {"profile_id", "provider_instance_id",
                              "registry_revision", "model_access",
                              "native_capability"}
    assert "opaque:already-bound" not in json.dumps(projected)
    assert projected["native_capability"] == CAPABILITY_REVISION
    assert module.project_model_access(snapshot, "opaque.connection-a")["native_capability"] is None
    page = module.project_model_access_catalog(snapshot, "opaque.connection-a", {
        "models": [{"provider_id": "openrouter", "provider_model_id": "a/model"},
                   {"provider_id": "openai", "provider_model_id": "same/name"}],
        "inventory": {"openrouter": {"source": "openrouter_models_api", "stale": False}},
    })
    assert page["models"] == [{"model_id": "a/model", "display_name": "a/model"}]
    assert page["status"] == "live"
    with pytest.raises(KeyError):
        module.project_model_access(snapshot, "deleted.connection")
