"""Real registry/configuration owners keep policies in their saved namespace."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import uuid

import pytest

from ecosystem.rumi_provider_registry_pack.runtime.configuration import (
    execute_configuration,
    prepare_configuration,
)
from ecosystem.rumi_provider_registry_pack.runtime.model_access import VERSION
from ecosystem.rumi_provider_registry_pack.runtime.provider_filters import (
    CAPABILITY_REVISION,
)
from ecosystem.rumi_provider_registry_pack.runtime.registry import (
    ProviderRegistry,
    ProviderRegistryConflict,
)
from tests.test_provider_configuration import _Client
from tests.test_production_frontend_contract_http import (
    _authenticate,
    _contract,
    _request,
    production_server as production_server,
)


def _record() -> dict[str, Any]:
    return {
        "provider_instance_id": "provider.fixture",
        "adapter_id": "openai-compatible",
        "endpoint": "https://provider.example/v1",
        "credential_handle": None,
        "metadata": {"catalog_provider_id": "openai"},
    }


def _policy(kind: str) -> dict[str, Any]:
    policy: dict[str, Any] = {
        "version": VERSION,
        "mode": "all" if kind == "all" else "explicit",
        "model_ids": ["vendor/model"] if kind == "ids" else [],
    }
    if kind == "native":
        policy["native_filters"] = {"revision": CAPABILITY_REVISION}
    return policy


def _seed(root: Path, kind: str = "ids") -> ProviderRegistry:
    owner = ProviderRegistry("defaults", user_data_root=root)
    source = _record()
    if kind == "native":
        source.update(
            {
                "endpoint": "https://openrouter.ai/api/v1",
                "metadata": {"catalog_provider_id": "openrouter"},
            }
        )
    owner.save({**source, "model_access": _policy(kind)}, expected_revision=0)
    if kind.startswith("legacy"):
        state = json.loads(owner.path.read_text())
        record = state["providers"]["provider.fixture"]
        record.pop("model_access")
        record["allowed_models"] = [] if kind == "legacy-empty" else ["vendor/model"]
        owner.path.write_text(json.dumps(state))
    return owner


def _configure_request(local: bool = False) -> dict[str, str]:
    return {
        "connection_name": "fixture",
        "protocol": "local-openai-compatible" if local else "openai-compatible",
        "endpoint": "http://127.0.0.1:1234/v1" if local else _record()["endpoint"],
        "key_value": "" if local else "synthetic-namespace-secret",
        "catalog_provider_id": "ollama" if local else "openai",
    }


@pytest.mark.parametrize("kind", ["all", "empty", "ids", "legacy-empty", "legacy-ids", "native"])
@pytest.mark.parametrize(
    "change",
    [
        {"adapter_id": "anthropic"},
        {"metadata": {"catalog_provider_id": "other"}},
        {"endpoint": "https://other.example/v1"},
    ],
)
def test_implicit_policy_carry_rejects_each_namespace_change_without_writes(
    tmp_path: Path,
    kind: str,
    change: dict[str, Any],
) -> None:
    owner = _seed(tmp_path, kind)
    before = owner.path.read_bytes()
    current = owner.snapshot()["providers"][0]
    incoming = {
        **_record(),
        **{key: current[key] for key in ("adapter_id", "endpoint", "metadata")},
        **change,
    }
    for operation in (owner.prepare_save, owner.save):
        with pytest.raises(ValueError, match="provider route changed"):
            operation(incoming, expected_revision=1)
        assert owner.path.read_bytes() == before
        assert owner.snapshot()["revision"] == 1


def test_same_namespace_rotation_keeps_policy_and_preflight_never_writes(
    tmp_path: Path,
) -> None:
    owner = _seed(tmp_path)
    before = owner.path.read_bytes()
    record = {**_record(), "credential_handle": "opaque:rotated", "display_name": "Work"}
    prepared = owner.prepare_save(record, expected_revision=1)
    assert owner.path.read_bytes() == before
    assert prepared["model_access"] == _policy("ids")
    assert prepared["record_revision"] == 2
    saved = owner.save(record, expected_revision=1)
    assert saved["store_revision"] == 2
    assert saved["provider"]["model_access"] == _policy("ids")


def test_registry_explicit_replacement_is_validated_for_new_namespace(
    tmp_path: Path,
) -> None:
    owner = _seed(tmp_path)
    before = owner.path.read_bytes()
    changed = {**_record(), "endpoint": "https://other.example/v1"}
    with pytest.raises(ValueError):
        owner.save({**changed, "model_access": None}, expected_revision=1)
    assert owner.path.read_bytes() == before
    replacement = _policy("empty")
    result = owner.save({**changed, "model_access": replacement}, expected_revision=1)
    assert result["store_revision"] == 2
    assert result["provider"]["model_access"] == replacement


def test_stale_revision_precedes_namespace_validation(tmp_path: Path) -> None:
    owner = _seed(tmp_path)
    before = owner.path.read_bytes()
    for operation in (owner.prepare_save, owner.save):
        with pytest.raises(ProviderRegistryConflict):
            operation({**_record(), "adapter_id": "anthropic"}, expected_revision=0)
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("local", [False, True])
def test_configure_catalog_removal_is_rejected_before_approval_or_credentials(
    tmp_path: Path,
    local: bool,
) -> None:
    owner = ProviderRegistry("defaults", user_data_root=tmp_path)
    request = _configure_request(local)
    record = {
        **_record(),
        "adapter_id": request["protocol"],
        "endpoint": request["endpoint"],
        "metadata": {"catalog_provider_id": request["catalog_provider_id"]},
        "model_access": _policy("ids"),
    }
    owner.save(record, expected_revision=0)
    before = owner.path.read_bytes()
    valid_plan = prepare_configuration(owner, request)
    request.pop("catalog_provider_id")
    client = _Client(tmp_path)
    with pytest.raises(ValueError, match="provider route changed"):
        prepare_configuration(owner, request)
    with pytest.raises(ValueError, match="provider route changed"):
        execute_configuration(
            owner,
            client,
            {"request": request, "plan": valid_plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == []
    assert owner.path.read_bytes() == before


@pytest.mark.parametrize("local", [False, True])
@pytest.mark.parametrize("unrelated_commit", [False, True])
def test_matching_old_record_cannot_confirm_a_save_that_never_committed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    local: bool,
    unrelated_commit: bool,
) -> None:
    owner = ProviderRegistry("defaults", user_data_root=tmp_path)
    request = _configure_request(local)
    first = _Client(tmp_path)
    execute_configuration(
        owner,
        first,
        {"request": request, "plan": prepare_configuration(owner, request)},
        consumer_pack_id="fixture.consumer",
    )
    before = owner.path.read_bytes()
    plan = prepare_configuration(owner, request)
    second = _Client(tmp_path)
    original_save = owner.save

    def fail(*_args: Any, **_kwargs: Any) -> None:
        if unrelated_commit:
            original_save(
                {**_record(), "provider_instance_id": "provider.other"},
                expected_revision=plan["expected_revision"],
            )
        raise OSError("lost ACK without commit")

    monkeypatch.setattr(owner, "save", fail)
    with pytest.raises(RuntimeError, match="connection save was not confirmed"):
        execute_configuration(
            owner,
            second,
            {"request": request, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    if unrelated_commit:
        saved = owner.snapshot()
        assert saved["revision"] == plan["expected_revision"] + 1
        current = next(
            p for p in saved["providers"] if p["provider_instance_id"] == "provider.fixture"
        )
        assert current["record_revision"] == 1
    else:
        assert owner.path.read_bytes() == before
    assert second.calls == ([] if local else ["create", "revoke"])


@pytest.mark.parametrize(
    "later_write",
    [
        "policy",
        "other-record",
        "snapshot-failure",
        "tampered-metadata",
        "tampered-policy",
    ],
)
def test_uncertain_commit_never_revokes_a_live_handle_or_confirms_other_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    later_write: str,
) -> None:
    owner = ProviderRegistry("defaults", user_data_root=tmp_path)
    client = _Client(tmp_path)
    request = _configure_request()
    plan = prepare_configuration(owner, request)
    original_save = owner.save

    def save(record: dict[str, Any], *, expected_revision: int) -> None:
        original_save(record, expected_revision=expected_revision)
        if later_write == "policy":
            owner.set_model_access("provider.fixture", _policy("empty"), expected_revision=1)
        elif later_write == "other-record":
            original_save(
                {**_record(), "provider_instance_id": "provider.other"}, expected_revision=1
            )
        elif later_write.startswith("tampered-"):
            state = json.loads(owner.path.read_text())
            saved = state["providers"]["provider.fixture"]
            if later_write == "tampered-metadata":
                saved["metadata"] = {}
            else:
                saved["model_access"] = _policy("empty")
            owner.path.write_text(json.dumps(state))
        else:

            def unavailable() -> dict[str, Any]:
                raise OSError("unknown binding")

            monkeypatch.setattr(owner, "snapshot", unavailable)
        raise OSError("lost ACK after commit")

    monkeypatch.setattr(owner, "save", save)
    with pytest.raises(RuntimeError, match="connection save was not confirmed"):
        execute_configuration(
            owner,
            client,
            {"request": request, "plan": plan},
            consumer_pack_id="fixture.consumer",
        )
    assert client.calls == ["create"]
    state = json.loads(owner.path.read_text())
    assert state["providers"]["provider.fixture"]["credential_handle"].startswith("credential:")


def test_native_replacement_cannot_enable_filters_on_a_foreign_endpoint(
    tmp_path: Path,
) -> None:
    owner = _seed(tmp_path, "native")
    before = owner.path.read_bytes()
    record = owner.snapshot()["providers"][0]
    with pytest.raises(ValueError):
        owner.save(
            {**record, "endpoint": "https://other.example/v1"},
            expected_revision=1,
        )
    assert owner.path.read_bytes() == before


def test_captured_prepare_denies_namespace_change_without_secret_or_owner_write(
    production_server: Any,
    tmp_path: Path,
) -> None:
    server, _session, _authority = production_server
    owner = _seed(tmp_path / "user-data")
    before = owner.path.read_bytes()
    cookie, csrf, origin = _authenticate(server)
    request = {**_configure_request(), "endpoint": "https://other.example/v1"}
    status, result, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ai/provider-key"),
        body={
            "phase": "prepare",
            "effect_kind": "provider_configure",
            "correlation_id": str(uuid.uuid4()),
            "request": request,
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status != 200, result
    assert request["key_value"] not in json.dumps(result)
    assert owner.path.read_bytes() == before
    assert not (tmp_path / "user-data/credentials/material-store/credentials.store.json").exists()
    # A valid same-namespace request still reaches the existing approval path.
    status, valid, _ = _request(
        server,
        "POST",
        _contract("POST", "/api/ai/provider-key"),
        body={
            "phase": "prepare",
            "effect_kind": "provider_configure",
            "correlation_id": str(uuid.uuid4()),
            "request": _configure_request(),
        },
        headers={
            "Cookie": cookie,
            "Origin": origin,
            "X-Rumi-CSRF": csrf,
            "X-Tobkiri-Request-ID": str(uuid.uuid4()),
        },
    )
    assert status == 200, valid
    assert valid["data"]["state"] == "approval_pending", valid
    assert request["key_value"] not in json.dumps(valid)
    assert owner.path.read_bytes() == before
    assert not (tmp_path / "user-data/credentials/material-store/credentials.store.json").exists()
