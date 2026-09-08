"""Captured Provider configuration uses the existing revisioned registry owner."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_provider_registry_pack.runtime.process import (
    ProviderRegistryHostFactoryV4,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
    ProviderRegistryConflict,
)


def _capture(tmp_path: Path, *, readonly: bool = False) -> Any:
    factory = ProviderRegistryHostFactoryV4(readonly=readonly)
    bindings = tuple(SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id, implementation_digest="sha256:hook",
        ),
        operation=SimpleNamespace(
            contract_id=factory.contract_id, operation_id=operation,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="registry-principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    ) for operation in sorted(factory.operations))
    context = SimpleNamespace(
        user_data_root=tmp_path, profile_id="defaults", provider_bindings=bindings,
        domain_ids={
            (factory.contract_id, operation, "registry-principal"): "registry-domain"
            for operation in factory.operations
        },
    )
    return factory.capture(context).contributions


def _save_payload() -> dict[str, Any]:
    return {
        "operation": "save", "expected_revision": 0,
        "record": {
            "provider_instance_id": "provider.fixture",
            "adapter_id": "openai-compatible",
            "endpoint": "https://provider.example/v1",
            "credential_handle": "credential:fixture",
        },
    }


def test_captured_configuration_save_read_delete_and_revision_conflict(
    tmp_path: Path,
) -> None:
    contribution = _capture(tmp_path)[0]
    saved = contribution.invoke(contribution.operation_id, _save_payload(), None)
    assert saved["store_revision"] == 1
    for reader in _capture(tmp_path, readonly=True):
        result = reader.invoke(reader.operation_id, {}, None)
        assert result["profile_id"] == "defaults"
        assert result["providers"][0] == saved["provider"]
        assert result["providers"][0]["health_evidence"]["verified"] is False
    with pytest.raises(ProviderRegistryConflict):
        contribution.invoke(contribution.operation_id, _save_payload(), None)
    result = contribution.invoke(contribution.operation_id, {
        "operation": "delete", "expected_revision": 1,
        "provider_instance_id": "provider.fixture",
    }, None)
    assert result["store_revision"] == 2
    assert ProviderRegistry("defaults", user_data_root=tmp_path).snapshot()["providers"] == []


@pytest.mark.parametrize("change", [
    {"profile_id": "other"}, {"profile_id": None},
    {"operation": "migration.apply"}, {"operation": "health"},
    {"approved": True}, {"expected_revision": True},
    {"expected_revision": "0"}, {"expected_revision": -1},
    {"expected_revision": None}, {"user_data_root": "/tmp"},
])
def test_captured_configuration_rejects_forged_or_incomplete_request(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    contribution = _capture(tmp_path)[0]
    with pytest.raises(PermissionError):
        contribution.invoke(contribution.operation_id, {**_save_payload(), **change}, None)
    assert not (tmp_path / "packs").exists()


@pytest.mark.parametrize("field", ["health_evidence", "api_key", "approved", "profile_id"])
def test_configuration_record_cannot_forge_health_or_include_secret_fields(
    tmp_path: Path, field: str,
) -> None:
    contribution = _capture(tmp_path)[0]
    payload = _save_payload()
    payload["record"][field] = "untrusted"
    with pytest.raises(PermissionError):
        contribution.invoke(contribution.operation_id, payload, None)
    assert not (tmp_path / "packs").exists()


def test_registry_reader_cannot_select_foreign_profile_or_mutate(tmp_path: Path) -> None:
    for reader in _capture(tmp_path, readonly=True):
        for payload in ({"profile_id": "other"}, _save_payload()):
            with pytest.raises(PermissionError):
                reader.invoke(reader.operation_id, payload, None)
    assert not (tmp_path / "packs").exists()
