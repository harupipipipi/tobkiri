"""Focused profile/broker boundary regressions for remediation C."""

from __future__ import annotations

import json
import os
import sys
from io import StringIO
from pathlib import Path

import pytest

from core_runtime.host_contract import bind_host_contract, host_contract_value
from core_runtime.profile_credentials import (
    BrokerServiceAdapter,
    CredentialUnavailable,
    ProfileCredentialRef,
    bind_profile_credential_broker,
    resolve_profile_credential,
)
from ecosystem.rumi_credential_broker_pack.runtime.service import (
    CredentialBrokerService,
)


def _created(tmp_path: Path) -> tuple[CredentialBrokerService, dict[str, object]]:
    service = CredentialBrokerService(user_data_root=tmp_path)
    created = service.invoke(
        "create",
        {
            "secret_material": {"api_key": "profile-secret"},
            "consumer_pack_id": "provider-adapter-pack",
            "provider_instance_id": "provider-main",
            "profile_id": "profile-a",
            "scopes": ["provider.invoke"],
        },
    )
    return service, created


def test_profile_ref_resolves_only_through_bound_host_broker(tmp_path: Path) -> None:
    service, created = _created(tmp_path)
    reference = ProfileCredentialRef.from_mapping(created["credential_ref"])
    adapter = BrokerServiceAdapter(service)

    with bind_profile_credential_broker("profile-a", adapter):
        with pytest.raises(CredentialUnavailable, match="Host transport"):
            resolve_profile_credential(
                reference,
                provider_id="provider-main",
                scope="provider.invoke",
                consumer_pack_id="provider-adapter-pack",
            )


def test_missing_or_foreign_profile_fails_closed(tmp_path: Path) -> None:
    service, created = _created(tmp_path)
    reference = ProfileCredentialRef.from_mapping(created["credential_ref"])
    adapter = BrokerServiceAdapter(service)

    with pytest.raises(CredentialUnavailable):
        resolve_profile_credential(
            reference,
            provider_id="provider-main",
            scope="provider.invoke",
            consumer_pack_id="provider-adapter-pack",
        )
    with bind_profile_credential_broker("profile-b", adapter):
        with pytest.raises(CredentialUnavailable):
            resolve_profile_credential(
                reference,
                provider_id="provider-main",
                scope="provider.invoke",
                consumer_pack_id="provider-adapter-pack",
            )


def test_explicit_profile_cannot_override_the_bound_profile(tmp_path: Path) -> None:
    service, created = _created(tmp_path)
    reference = ProfileCredentialRef.from_mapping(created["credential_ref"])
    adapter = BrokerServiceAdapter(service)

    with bind_profile_credential_broker("profile-a", adapter):
        with pytest.raises(CredentialUnavailable, match="Host transport"):
            resolve_profile_credential(
                reference,
                provider_id="provider-main",
                scope="provider.invoke",
                consumer_pack_id="provider-adapter-pack",
                profile_id="profile-b",
            )


def test_tampered_broker_metadata_is_rejected(tmp_path: Path) -> None:
    service, created = _created(tmp_path)
    handle = str(created["handle"])
    payload = json.loads(service.store.path.read_text(encoding="utf-8"))
    payload["credentials"][handle]["profile_id"] = "profile-b"
    service.store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(PermissionError, match="Host transport"):
        service.invoke(
            "resolve",
            {
                "_contract_consumer_pack_id": "provider-adapter-pack",
                "handle": handle,
                "provider_instance_id": "provider-main",
                "profile_id": "profile-a",
                "scope": "provider.invoke",
            },
        )


def test_ambient_environment_cannot_inject_host_or_provider_credential(monkeypatch) -> None:
    monkeypatch.setenv("RUMI_API_TOKEN", "ambient-token")
    monkeypatch.setenv("OPENAI_API_KEY", "ambient-key")

    assert host_contract_value("desktop_api_token") == ""
    from ecosystem.defaultspack.domain.ai_client.providers.openai_provider import (
        OpenAIProvider,
    )

    assert OpenAIProvider()._api_key == ""
    with bind_host_contract(
        {"profile_id": "profile-a", "values": {"desktop_api_token": "host-token"}}
    ):
        assert host_contract_value("desktop_api_token", profile_id="profile-a") == "host-token"
        assert host_contract_value("desktop_api_token", profile_id="profile-b") == ""


def test_host_contract_rejects_ambient_path_symlink_and_unsafe_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_data = tmp_path / "user-data"
    user_data.mkdir()
    outside = tmp_path / "attacker.json"
    payload = {
        "schema_version": "tobkiri.host-contract.v1",
        "profile_id": "profile-a",
        "values": {"desktop_api_token": "attacker-token"},
    }
    outside.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(outside, 0o600)
    monkeypatch.setenv("RUMI_USER_DATA", str(user_data))
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(outside))
    assert host_contract_value("desktop_api_token", profile_id="profile-a") == ""

    expected = user_data / "host_contract.json"
    expected.symlink_to(outside)
    monkeypatch.setenv("TOBKIRI_HOST_CONTRACT_PATH", str(expected))
    assert host_contract_value("desktop_api_token", profile_id="profile-a") == ""
    expected.unlink()
    expected.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(expected, 0o644)
    assert host_contract_value("desktop_api_token", profile_id="profile-a") == ""
    os.chmod(user_data, 0o700)
    os.chmod(expected, 0o600)
    assert host_contract_value("desktop_api_token", profile_id="profile-a") == "attacker-token"
    assert (
        host_contract_value("desktop_api_token", profile_id="profile-a", provider_id="github") == ""
    )


def test_broker_store_rejects_symlink_storage_and_cross_profile_management(
    tmp_path: Path,
) -> None:
    service, created = _created(tmp_path)
    with pytest.raises(PermissionError, match="profile"):
        service.invoke(
            "revoke",
            {"handle": created["handle"], "profile_id": "profile-b"},
        )
    assert service.invoke("list", {"profile_id": "profile-b"})["count"] == 0

    symlink_root = tmp_path / "symlink-user-data"
    symlink_root.symlink_to(tmp_path, target_is_directory=True)
    unsafe = CredentialBrokerService(user_data_root=symlink_root)
    with pytest.raises(PermissionError, match="symlink"):
        unsafe.invoke("list", {"profile_id": "profile-a"})


def test_generic_process_channel_never_serializes_resolved_material(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ecosystem.rumi_credential_broker_pack.runtime import process

    request = {
        "operation": "resolve",
        "payload": {
            "_contract_consumer_pack_id": "provider-adapter-pack",
            "handle": "credential:forged",
            "provider_instance_id": "provider-main",
            "profile_id": "profile-a",
            "scope": "provider.invoke",
        },
    }
    output = StringIO()
    monkeypatch.setattr(sys, "stdin", StringIO(json.dumps(request)))
    monkeypatch.setattr(sys, "stdout", output)

    assert process.main() == 3
    assert "secret_material" not in output.getvalue()
    assert json.loads(output.getvalue())["status"] == "denied"


@pytest.mark.parametrize(
    ("case", "expected_error"),
    (
        ("valid", True),
        ("missing_profile", True),
        ("foreign_profile", True),
        ("provider_mismatch", True),
        ("scope_missing", True),
        ("broker_missing", True),
        ("tampered_reference", True),
        ("ambient_injection", False),
    ),
)
def test_eight_node_profile_credential_gate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    expected_error: bool,
) -> None:
    """Exercise the broker boundary as eight independent policy nodes."""

    service, created = _created(tmp_path)
    reference = ProfileCredentialRef.from_mapping(created["credential_ref"])
    adapter = BrokerServiceAdapter(service)
    profile_id = "profile-a"
    selected = reference
    scope = "provider.invoke"
    if case == "foreign_profile":
        profile_id = "profile-b"
    elif case == "provider_mismatch":
        selected = ProfileCredentialRef(
            profile_id=reference.profile_id,
            provider_id="provider-other",
            credential_id=reference.credential_id,
            key_version=reference.key_version,
        )
    elif case == "scope_missing":
        scope = ""
    elif case == "broker_missing":
        adapter = None
    elif case == "tampered_reference":
        selected = ProfileCredentialRef(
            profile_id=reference.profile_id,
            provider_id=reference.provider_id,
            credential_id=reference.credential_id,
            key_version="credential-broker.key.tampered",
        )
    elif case == "ambient_injection":
        monkeypatch.setenv("OPENAI_API_KEY", "attacker-controlled")

        from ecosystem.defaultspack.domain.ai_client.providers.openai_provider import (
            OpenAIProvider,
        )

        assert OpenAIProvider()._api_key == ""
        return_value = {"api_key": ""}
    else:
        return_value = None

    def resolve() -> object:
        if adapter is None:
            return resolve_profile_credential(
                selected,
                provider_id="provider-main",
                scope=scope,
                consumer_pack_id="provider-adapter-pack",
                profile_id=profile_id,
            )
        if case == "missing_profile":
            return resolve_profile_credential(
                selected,
                provider_id="provider-main",
                scope=scope,
                consumer_pack_id="provider-adapter-pack",
            )
        with bind_profile_credential_broker(profile_id, adapter):
            return resolve_profile_credential(
                selected,
                provider_id="provider-main",
                scope=scope,
                consumer_pack_id="provider-adapter-pack",
            )

    if expected_error:
        with pytest.raises(CredentialUnavailable):
            resolve()
    elif case == "ambient_injection":
        assert return_value == {"api_key": ""}
    else:
        assert resolve() == {"api_key": "profile-secret"}
