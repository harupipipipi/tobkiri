"""Model route creation only accepts current Provider registry identities."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ecosystem.defaultspack.defaultspack.model_profile_presentation import (
    normalize_model_profile_save,
)
from ecosystem.rumi_provider_registry_pack.runtime.process import (
    _provider_connection_snapshot,
)


def test_model_route_payload_requires_the_provider_registry_revision() -> None:
    with pytest.raises(ValueError, match="fields"):
        normalize_model_profile_save({
            "model_profile_id": "daily",
            "model_id": "gpt-4.1",
            "provider_instance_id": "connection/openai:main",
            "display_name": "Daily",
            "expected_revision": 2,
        })


def test_model_route_payload_keeps_the_registry_revision_outside_route_metadata() -> None:
    normalized = normalize_model_profile_save({
        "model_profile_id": "daily",
        "model_id": "gpt-4.1",
        "provider_instance_id": "connection/openai:main",
        "display_name": "Daily",
        "expected_revision": 2,
        "provider_registry_revision": 4,
    })

    assert normalized["provider_registry_revision"] == 4
    assert normalized["record"] == {
        "model_profile_id": "daily",
        "model_id": "gpt-4.1",
        "display_name": "Daily",
        "metadata": {"provider_connection_id": "connection/openai:main"},
    }


def test_provider_connection_snapshot_projects_only_safe_exact_identities() -> None:
    snapshot = _provider_connection_snapshot({
        "revision": 9,
        "providers": [
            {
                "provider_instance_id": "connection/openai:main",
                "display_name": "OpenAI main",
                "enabled": True,
                "credential_handle": "credential:must-not-reach-ui",
                "endpoint": "https://provider.example/v1",
                "health_evidence": {
                    "status": "available", "verified": True,
                    "observed_at": 123.5,
                },
                "metadata": {"legacy_api_id": "main"},
            },
            {
                "provider_instance_id": "disabled/connection",
                "display_name": "Disabled connection",
                "enabled": False,
                "credential_handle": None,
                "health_evidence": {
                    "status": "available", "verified": False,
                    "observed_at": 456.0,
                },
            },
        ],
    })

    assert snapshot == {
        "revision": 9,
        "providers": [
            {
                "provider_instance_id": "connection/openai:main",
                "display_name": "OpenAI main",
                "enabled": True,
                "credential_status": "configured",
                "health_status": "verified",
                "reachability": "available",
                "observed_at": 123.5,
            },
            {
                "provider_instance_id": "disabled/connection",
                "display_name": "Disabled connection",
                "enabled": False,
                "credential_status": "missing",
                "health_status": "unverified",
                "reachability": "unknown",
                "observed_at": None,
            },
        ],
    }
    assert "credential_handle" not in str(snapshot)
    assert "endpoint" not in str(snapshot)


def test_application_capability_map_binds_the_shell_to_the_exact_registry_read() -> None:
    runtime_root = Path(__file__).resolve().parents[1]
    intent = json.loads((
        runtime_root / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    ).read_text(encoding="utf-8"))
    requested_edges = intent["requested_edges"]
    expected_shell_edge = {
        "caller_function_id": "shell.tauri.default",
        "target_provider_id": "rumi_provider_registry_pack.provider-registry.resource",
        "contract_id": "tobkiri.resource.ai.provider.registry.v1",
        "operation_id": "rumi_provider_registry_pack.provider-registry-resource",
    }
    assert any(
        all(edge.get(key) == value for key, value in expected_shell_edge.items())
        for edge in requested_edges
    )

    frontend_map = json.loads((
        runtime_root / "ecosystem/defaultspack/defaultspack/frontend_contract_map.v4.json"
    ).read_text(encoding="utf-8"))
    capability_route = next(
        route for route in frontend_map["routes"]
        if route["method"] == "POST" and route["path"] == "/api/ui/capability/invoke"
    )
    assert {
        "contribution_id": "defaults.providers.connections.read",
        "contract_id": "tobkiri.resource.ai.provider.registry.v1",
        "operation_id": "rumi_provider_registry_pack.provider-registry-resource",
        "provider_id": "rumi_provider_registry_pack.provider-registry.resource",
        "function_id": "rumi_provider_registry_pack.provider-registry.resource",
        "allowed_payload_keys": [],
    } in capability_route["targets"]


def test_gateway_routes_bind_each_mode_to_the_exact_registry_read() -> None:
    """Saved model execution must revalidate the same owner projection."""
    runtime_root = Path(__file__).resolve().parents[1]
    intent = json.loads((
        runtime_root / "ecosystem/defaultspack/v4/defaults.profile.intent.v1.json"
    ).read_text(encoding="utf-8"))
    registry_contract = "tobkiri.resource.ai.provider.registry.v1"
    registry_target = "rumi_provider_registry_pack.provider-registry.resource"
    expected = {
        (
            "rumi_ai_gateway_pack.ai-gateway.preflight",
            "rumi_provider_registry_pack.provider-registry-resource.generate",
        ),
        (
            "rumi_ai_gateway_pack.ai-gateway.generate",
            "rumi_provider_registry_pack.provider-registry-resource.generate",
        ),
        (
            "rumi_ai_gateway_pack.ai-gateway.stream",
            "rumi_provider_registry_pack.provider-registry-resource.stream",
        ),
    }
    actual = {
        (edge["caller_function_id"], edge["operation_id"])
        for edge in intent["requested_edges"]
        if edge.get("contract_id") == registry_contract
        and edge.get("target_provider_id") == registry_target
    }

    assert expected <= actual
