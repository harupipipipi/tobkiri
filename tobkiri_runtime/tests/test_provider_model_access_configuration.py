"""Real registry CAS for finite credential-free policy configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecosystem.rumi_provider_registry_pack.runtime.model_access import VERSION
from ecosystem.rumi_provider_registry_pack.runtime.model_access_configuration import (
    execute_model_access,
    prepare_model_access,
)
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import (
    CAPABILITY_REVISION,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
    ProviderRegistryConflict,
)


@pytest.fixture
def registry(tmp_path: Path) -> ProviderRegistry:
    """Create a synthetic official connection with an opaque fake credential."""
    owner = ProviderRegistry("defaults", user_data_root=tmp_path)
    owner.save(
        {
            "provider_instance_id": "connection/work",
            "adapter_id": "openai-compatible",
            "display_name": "Work",
            "endpoint": "https://openrouter.ai/api/v1",
            "credential_handle": "credential:synthetic-private-marker",
            "metadata": {"catalog_provider_id": "openrouter"},
        },
        expected_revision=0,
    )
    return owner


def request(policy: dict | None = None) -> dict:
    """Return an exact public request without credential material."""
    return {
        "profile_id": "defaults",
        "provider_instance_id": "connection/work",
        "expected_revision": 1,
        "model_access": policy
        or {
            "version": VERSION,
            "mode": "explicit",
            "model_ids": [],
        },
    }


@pytest.mark.parametrize(
    "mode,ids", [("all", []), ("explicit", []), ("explicit", ["vendor/model", "vendor/model"])]
)
def test_save_preserves_credentials_and_public_result(
    registry: ProviderRegistry,
    mode: str,
    ids: list[str],
) -> None:
    """Policy writes preserve all original connection fields and redact results."""
    original = registry.snapshot()["providers"][0]
    value = request({"version": VERSION, "mode": mode, "model_ids": ids})
    before = registry.path.read_bytes()
    plan = prepare_model_access(registry, value)
    assert registry.path.read_bytes() == before
    result = execute_model_access(registry, {"request": value, "plan": plan})
    saved = registry.snapshot()["providers"][0]
    for field in ("credential_handle", "endpoint", "adapter_id", "metadata"):
        assert saved[field] == original[field]
    assert result["registry_revision"] == 2
    assert result["model_access"]["mode"] == mode
    assert result["model_access"]["model_ids"] == sorted(set(ids))
    for public in (plan, result):
        encoded = json.dumps(public)
        assert "synthetic-private-marker" not in encoded
        assert "endpoint" not in public and "credential_handle" not in public


@pytest.mark.parametrize(
    "extra",
    [
        {"approved": True},
        {"credential_handle": "x"},
        {"native_capability_revision": CAPABILITY_REVISION},
    ],
)
def test_untrusted_fields_cannot_enter_plan(registry: ProviderRegistry, extra: dict) -> None:
    """Public flags cannot attest approval, capability, or replace a credential."""
    with pytest.raises(ValueError):
        prepare_model_access(registry, {**request(), **extra})
    assert registry.snapshot()["revision"] == 1


@pytest.mark.parametrize("changes", [{"profile_id": "foreign"}, {"expected_revision": True}])
def test_scope_and_boolean_revision_rejected(registry: ProviderRegistry, changes: dict) -> None:
    """The captured profile and integer CAS are mandatory."""
    with pytest.raises(ValueError):
        prepare_model_access(registry, {**request(), **changes})


def test_stale_and_deleted_connection_do_not_execute(registry: ProviderRegistry) -> None:
    """An approval cannot modify a removed or subsequently changed connection."""
    value = request()
    plan = prepare_model_access(registry, value)
    registry.delete("connection/work", expected_revision=1)
    with pytest.raises(ProviderRegistryConflict):
        execute_model_access(registry, {"request": value, "plan": plan})
    with pytest.raises(KeyError):
        prepare_model_access(registry, {**value, "expected_revision": 2})


def test_plan_tampering_and_unknown_policy_fail_closed(registry: ProviderRegistry) -> None:
    """Changed frozen plans and future policy schemas never write."""
    value = request()
    plan = prepare_model_access(registry, value)
    with pytest.raises(PermissionError):
        execute_model_access(registry, {"request": value, "plan": {**plan, "extra": True}})
    with pytest.raises(ValueError):
        prepare_model_access(registry, request({"version": "future", "mode": "all"}))
    assert registry.snapshot()["revision"] == 1


def test_native_capability_rechecked_on_execute(registry: ProviderRegistry) -> None:
    """Host capability evidence must remain available when approval executes."""
    value = request(
        {
            "version": VERSION,
            "mode": "all",
            "model_ids": [],
            "native_filters": {"revision": CAPABILITY_REVISION},
        }
    )
    with pytest.raises(PermissionError):
        prepare_model_access(registry, value)
    plan = prepare_model_access(registry, value, native_capability_revision=CAPABILITY_REVISION)
    with pytest.raises(PermissionError):
        execute_model_access(registry, {"request": value, "plan": plan})
    result = execute_model_access(
        registry, {"request": value, "plan": plan}, native_capability_revision=CAPABILITY_REVISION
    )
    assert result["native_capability"] == CAPABILITY_REVISION


def test_all_policy_retains_confirmed_native_capability(
    registry: ProviderRegistry,
) -> None:
    """Saving all models keeps native editing available without saved filters."""
    value = request({"version": VERSION, "mode": "all", "model_ids": []})
    plan = prepare_model_access(
        registry,
        value,
        native_capability_revision=CAPABILITY_REVISION,
    )
    assert plan["native_capability_revision"] == CAPABILITY_REVISION
    with pytest.raises(PermissionError):
        execute_model_access(registry, {"request": value, "plan": plan})
    result = execute_model_access(
        registry,
        {"request": value, "plan": plan},
        native_capability_revision=CAPABILITY_REVISION,
    )
    assert result["native_capability"] == CAPABILITY_REVISION
    assert "native_filters" not in result["model_access"]


def test_server_capability_does_not_attest_foreign_endpoint(
    registry: ProviderRegistry,
) -> None:
    """A matching server revision cannot enable native UI on an arbitrary host."""
    connection = registry.snapshot()["providers"][0]
    registry.save(
        {**connection, "endpoint": "https://provider.invalid/v1"},
        expected_revision=1,
    )
    value = {
        **request({"version": VERSION, "mode": "all", "model_ids": []}),
        "expected_revision": 2,
    }
    plan = prepare_model_access(
        registry,
        value,
        native_capability_revision=CAPABILITY_REVISION,
    )
    result = execute_model_access(
        registry,
        {"request": value, "plan": plan},
        native_capability_revision=CAPABILITY_REVISION,
    )
    assert plan["native_capability_revision"] is None
    assert result["native_capability"] is None


def test_revision_race_is_caught_by_registry_cas(
    registry: ProviderRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A change between execute validation and write cannot overwrite state."""
    value = request()
    plan = prepare_model_access(registry, value)
    original = registry.set_model_access

    def raced(identifier: str, policy: dict, *, expected_revision: int) -> dict:
        original(
            identifier,
            {"version": VERSION, "mode": "all", "model_ids": []},
            expected_revision=expected_revision,
        )
        return original(identifier, policy, expected_revision=expected_revision)

    monkeypatch.setattr(registry, "set_model_access", raced)
    with pytest.raises(ProviderRegistryConflict):
        execute_model_access(registry, {"request": value, "plan": plan})
    assert registry.snapshot()["providers"][0]["model_access"]["mode"] == "all"
