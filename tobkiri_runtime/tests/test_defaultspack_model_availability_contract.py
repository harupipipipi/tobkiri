from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

pytestmark = pytest.mark.contract

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_provider_key_save_with_default_model_creates_available_api_bound_profile(tmp_path):
    from domain.ai_client.api_key_store import set_provider_api_key
    from domain.ai_client.model_availability import ModelAvailabilityService

    result = set_provider_api_key(
        "examplellm",
        "secret",
        pack_root=tmp_path,
        api_id="main",
        name="main",
        default_model="example-chat",
    )
    assert result["success"] is True

    availability = ModelAvailabilityService(tmp_path).after_provider_key_saved(
        "examplellm",
        "main",
        default_model="example-chat",
    )

    assert availability["status"] == "models_available"
    assert availability["selected_profile_id"] == "examplellm/main/example-chat"
    assert availability["profiles"][0]["availability"]["configured"] is True


def test_provider_key_save_without_model_binding_requires_explicit_route(tmp_path):
    from domain.ai_client.api_key_store import set_provider_api_key
    from domain.ai_client.model_availability import ModelAvailabilityService

    result = set_provider_api_key(
        "examplellm",
        "secret",
        pack_root=tmp_path,
        api_id="main",
        name="main",
    )
    assert result["success"] is True

    availability = ModelAvailabilityService(tmp_path).after_provider_key_saved("examplellm", "main")

    assert availability["status"] == "route_required"
    assert availability["provider_id"] == "examplellm"
    assert availability["api_id"] == "main"
    assert "Choose a default model" in availability["reason"]


def test_provider_key_save_auto_binds_every_live_discovered_model(tmp_path, monkeypatch):
    from domain.ai_client.api_key_store import set_provider_api_key
    from domain.ai_client.model_availability import ModelAvailabilityService

    # Keep the settings refresh independent of any developer-machine account
    # keys; live inventory is supplied below as a deterministic fixture.
    monkeypatch.setenv("RUMI_DEFAULTSPACK_SECRETS_DIR", str(tmp_path / "secrets"))
    result = set_provider_api_key(
        "examplellm",
        "secret",
        pack_root=tmp_path,
        api_id="main",
        name="main",
    )
    assert result["success"] is True
    service = ModelAvailabilityService(tmp_path)
    monkeypatch.setattr(
        service,
        "_catalog_models",
        lambda _provider_id: [
            {
                "model_id": "account/a",
                "metadata": {"source": "remote_models_endpoint"},
            },
            {
                "model_id": "account/b",
                "metadata": {"source": "remote_models_endpoint"},
            },
        ],
    )

    availability = service.after_provider_key_saved("examplellm", "main")

    assert availability["status"] == "models_available"
    assert {profile["profile_id"] for profile in availability["profiles"]} == {
        "examplellm/main/account/a",
        "examplellm/main/account/b",
    }


def test_openai_compatible_live_inventory_is_trusted_for_named_connection(monkeypatch):
    from domain.ai_client.model_availability import ModelAvailabilityService

    service = ModelAvailabilityService()
    monkeypatch.setattr(
        service,
        "_connection_models",
        lambda provider_id, api_id: [
            {
                "model_id": "qwen-live",
                "metadata": {"source": "openai_models_endpoint"},
            }
        ],
    )

    assert service._live_model_ids("opencode-zen", "main") == ["qwen-live"]


def test_provider_key_approval_binding_redacts_secret() -> None:
    from blocks.ai import provider_key

    approval = provider_key._approval_data(
        {"provider_id": "example", "value": "fixture-secret"}
    )

    assert "value" not in approval
    assert "fixture-secret" not in str(approval)
    assert len(approval["value_sha256"]) == 64


def test_provider_key_compatibility_store_preserves_named_connection_metadata(monkeypatch) -> None:
    from blocks.ai import provider_key

    captured = {}

    def fake_set_provider_api_key(provider_id, secret, **options):
        captured.update(provider_id=provider_id, secret=secret, **options)
        return {"success": True}

    monkeypatch.setattr(provider_key, "set_provider_api_key", fake_set_provider_api_key)

    provider_key._sync_legacy_provider_key(
        "examplellm",
        "fixture-secret",
        {
            "api_id": "work",
            "name": "Work account",
            "base_url": "https://example.invalid/v1",
            "allowed_models": ["example-chat"],
            "default_model": "example-chat",
            "notes": "fixture",
            "quota_label": "team",
            "kind": "openai-compatible",
        },
    )

    assert captured == {
        "provider_id": "examplellm",
        "secret": "fixture-secret",
        "api_id": "work",
        "name": "Work account",
        "base_url": "https://example.invalid/v1",
        "allowed_models": ["example-chat"],
        "default_model": "example-chat",
        "notes": "fixture",
        "quota_label": "team",
        "kind": "openai-compatible",
    }


def test_provider_key_compatibility_rollback_clears_the_same_named_connection(monkeypatch) -> None:
    from blocks.ai import provider_key

    captured = {}

    def fake_set_provider_api_key(provider_id, secret, **options):
        captured.update(provider_id=provider_id, secret=secret, **options)
        return {"success": True}

    monkeypatch.setattr(provider_key, "set_provider_api_key", fake_set_provider_api_key)

    provider_key._clear_legacy_provider_key("examplellm", api_id="work")

    assert captured == {
        "provider_id": "examplellm",
        "secret": "",
        "api_id": "work",
    }


def test_provider_key_upsert_returns_authoritative_model_availability(monkeypatch) -> None:
    from blocks.ai import provider_key

    monkeypatch.setattr(
        provider_key,
        "_invoke",
        lambda contract_id, operation, payload: {"handle": "credential-handle"}
        if operation == "create"
        else {},
    )
    monkeypatch.setattr(provider_key, "_sync_legacy_provider_key", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        provider_key,
        "_save_connection",
        lambda provider_id, data, *, credential_handle: {
            "success": True,
            "provider_id": provider_id,
        },
    )

    class FakeAvailabilityService:
        def after_provider_key_saved(self, provider_id, api_id, **options):
            assert provider_id == "examplellm"
            assert api_id == "work"
            assert options == {
                "default_model": "example-chat",
                "allowed_models": ["example-chat"],
            }
            return {
                "status": "models_available",
                "profiles": [{"profile_id": "examplellm/work/example-chat"}],
                "selected_profile_id": "examplellm/work/example-chat",
            }

    monkeypatch.setattr(provider_key, "ModelAvailabilityService", FakeAvailabilityService)

    result = provider_key._upsert(
        "examplellm",
        {
            "value": "fixture-secret",
            "api_id": "work",
            "default_model": "example-chat",
            "allowed_models": ["example-chat"],
        },
    )

    assert result["configured"] is True
    assert result["api_id"] == "work"
    assert result["model_availability"]["status"] == "models_available"
