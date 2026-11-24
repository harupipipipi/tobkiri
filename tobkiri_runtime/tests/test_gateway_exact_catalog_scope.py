"""A pinned model route must not discover unrelated remote inventories."""
from types import SimpleNamespace

import pytest

from ecosystem.rumi_ai_gateway_pack.runtime import gateway
from ecosystem.rumi_model_catalog_pack.runtime import catalog


@pytest.mark.parametrize("model,exact,expected,remote_reads", [
    ("local-review/model", True, {"provider_id": "local-review", "model_id": "model"}, 0),
    ("openrouter/acme/model", True, {"provider_id": "openrouter", "model_id": "acme/model"}, 1),
    ("local-review/model", False, {}, 1),
])
def test_catalog_query_preserves_exact_owner_and_failover_scope(
    monkeypatch, tmp_path, model, exact, expected, remote_reads,
):
    calls = []
    network = []
    monkeypatch.setenv("TOBKIRI_USER_DATA", str(tmp_path))
    monkeypatch.setenv("RUMI_USER_DATA", str(tmp_path))
    monkeypatch.setattr(catalog, "_OPENROUTER_MEMORY_INVENTORY", None)
    monkeypatch.setattr(catalog, "_fetch_openrouter_inventory", lambda: network.append("openrouter") or [])
    operation = catalog.create_model_catalog_operation(None)

    def invoke(contract, name, payload, **kwargs):
        if contract == gateway.CATALOG_CONTRACT:
            calls.append(dict(payload))
            return operation(name, payload)
        assert contract == gateway.ROUTING_CONTRACT
        return {"candidates": [], "excluded": []}

    client = SimpleNamespace(
        providers=lambda _: ({"provider_instance_id": "catalog.fixture"},),
        invoke=invoke,
    )
    requirement = gateway._requirement({"requirements": {
        "preferred_model_id": model,
        "preferred_provider_instance_id": "provider.compatibility.generate",
    }})
    gateway._catalog_candidates(client, {}, requirement, {}, streaming=False, exact_binding=exact)
    assert calls == [expected]
    assert len(network) == remote_reads
