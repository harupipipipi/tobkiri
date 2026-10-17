"""Credential Host hook tests; Broker approval is tested separately."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from ecosystem.rumi_credential_broker_pack.runtime.process import (
    CredentialManagementHostFactoryV4,
)


def _capture(tmp_path: Path, *, readonly: bool = False) -> Any:
    factory = CredentialManagementHostFactoryV4(readonly=readonly)
    binding = SimpleNamespace(
        function=SimpleNamespace(
            function_id=factory.function_id, implementation_digest="sha256:hook",
        ),
        operation=SimpleNamespace(
            contract_id=factory.contract_id, operation_id=factory.operation_id,
            contract_version="1.0.0",
        ),
        principal_ref=SimpleNamespace(value="credential-principal"),
        artifact=SimpleNamespace(digest="sha256:artifact"),
    )
    context = SimpleNamespace(
        user_data_root=tmp_path, profile_id="defaults",
        provider_bindings=(binding,),
        domain_ids={
            (factory.contract_id, factory.operation_id, "credential-principal"):
                "credential-domain",
        },
    )
    return factory, context


def _invoke(factory: Any, context: Any, payload: dict[str, Any]) -> Any:
    contribution = factory.capture(context).contributions[0]
    return contribution.invoke(factory.operation_id, payload, None)


def test_captured_credential_create_status_revoke_use_encrypted_owner(
    tmp_path: Path,
) -> None:
    factory, context = _capture(tmp_path)
    secret = "fixture-provider-secret-never-public"
    created = _invoke(factory, context, {
        "operation": "create", "profile_id": "defaults",
        "secret_material": {"api_key": secret},
        "consumer_pack_id": "rumi_provider_adapters_pack",
        "provider_instance_id": "provider.fixture", "scopes": ["ai.generate"],
    })
    assert created["profile_id"] == "defaults"
    assert created["handle"].startswith("credential:")
    assert secret not in str(created)
    assert secret not in (
        tmp_path / "credentials/material-store/credentials.store.json"
    ).read_text()
    reader, reader_context = _capture(tmp_path, readonly=True)
    status = _invoke(reader, reader_context, {
        "operation": "list", "profile_id": "defaults",
    })
    assert created["handle"] in str(status)
    assert secret not in str(status)
    revoked = _invoke(factory, context, {
        "operation": "revoke", "profile_id": "defaults",
        "handle": created["handle"],
    })
    assert secret not in str(revoked)


@pytest.mark.parametrize("change", [
    {"profile_id": "other"}, {"profile_id": None},
    {"operation": "resolve"}, {"operation": "migration.apply"},
    {"operation": []}, {"approved": True},
    {"_contract_consumer_pack_id": "forged"}, {"user_data_root": "/tmp"},
])
def test_captured_management_rejects_authority_and_profile_injection(
    tmp_path: Path, change: dict[str, Any],
) -> None:
    factory, context = _capture(tmp_path)
    with pytest.raises(PermissionError, match="credential request is invalid"):
        _invoke(factory, context, {
            "operation": "revoke", "profile_id": "defaults", "handle": "unused",
            **change,
        })
    assert not (tmp_path / "credentials").exists()


def test_status_cannot_create_credentials(tmp_path: Path) -> None:
    factory, context = _capture(tmp_path, readonly=True)
    with pytest.raises(PermissionError):
        _invoke(factory, context, {"operation": "create", "profile_id": "defaults"})
    assert not (tmp_path / "credentials").exists()


@pytest.mark.parametrize("invalid", ["root", "profile", "bindings", "domain", "function"])
def test_capture_rejects_incomplete_or_unrelated_bindings(
    tmp_path: Path, invalid: str,
) -> None:
    factory, context = _capture(tmp_path)
    if invalid == "root":
        context.user_data_root = None
    elif invalid == "profile":
        context.profile_id = ""
    elif invalid == "bindings":
        context.provider_bindings = ()
    elif invalid == "domain":
        context.domain_ids = {}
    else:
        context.provider_bindings[0].function.function_id = "unrelated"
    with pytest.raises(PermissionError):
        factory.capture(context)
    assert not (tmp_path / "credentials").exists()
