"""Captured model configuration stays inside its selected runtime Profile."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_model_registry_pack.runtime.process import ModelRegistryHostFactoryV4
from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry


def _invoke(tmp_path: Path, kind: str = "manage") -> Any:
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
    invocation = SimpleNamespace(contract_client=lambda **kwargs: None)
    return lambda payload: captured.contributions[0].invoke(operation, payload, invocation)


def test_model_configuration_uses_captured_profile_and_owner_revision(tmp_path: Path) -> None:
    invoke = _invoke(tmp_path)
    saved = invoke({
        "operation": "save", "expected_revision": 0,
        "record": {"model_profile_id": "daily", "model_id": "provider/model"},
    })
    assert saved["store_revision"] == 1
    invoke({"operation": "alias.set", "expected_revision": 1, "alias": "default", "target_profile_id": "daily"})
    assert _invoke(tmp_path, "profile")({"identifier": "default"})["resolved_profile_id"] == "daily"
    assert len(_invoke(tmp_path, "profile")({"operation": "list"})["profiles"]) == 1
    assert ModelRegistry("other", user_data_root=tmp_path).snapshot()["profiles"] == []


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
            "record": {"model_profile_id": "daily", "model_id": "provider/model"},
            **change,
        })
    assert not (tmp_path / "packs").exists()


def test_model_read_rejects_foreign_profile_and_write_fields(tmp_path: Path) -> None:
    for payload in ({"operation": "list", "profile_id": "other"}, {"operation": "list", "record": {}}):
        with pytest.raises(PermissionError):
            _invoke(tmp_path, "profile")(payload)
    assert not (tmp_path / "packs").exists()
