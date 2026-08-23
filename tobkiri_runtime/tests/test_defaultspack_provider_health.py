"""Compatibility specifications for contract-backed provider health."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_provider_health_projects_opaque_and_unknown_contract_state(
    monkeypatch,
) -> None:
    from domain.ai_client import provider_health

    def invoke(contract_id, operation, payload):
        del operation, payload
        if contract_id.endswith("provider.health.v1"):
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.example",
                        "status": "unknown",
                        "observed_at": None,
                        "verified": False,
                    }
                ]
            }
        return {
            "credentials": [
                {
                    "provider_instance_id": "provider.example",
                    "opaque_id": "credential-status:opaque",
                    "source": "provider_default",
                    "scopes": ["ai.generate"],
                    "reason_code": "not_verified",
                }
            ]
        }

    monkeypatch.setattr(provider_health, "_invoke", invoke)

    report = provider_health.provider_health_report(provider_ids=["example"])
    item = report["providers"][0]
    assert report["contract_version"] == "provider-health.v2-compat"
    assert item["status"] == "unknown"
    assert item["credential"]["source"] == "provider_default"
    assert item["credential"]["opaque_id"] == "credential-status:opaque"
    assert "handle" not in str(report).lower()
    assert "secret" not in str(report).lower()


def test_provider_health_uses_canonical_contracts_and_captured_profile(
    monkeypatch,
) -> None:
    from domain.ai_client import provider_health

    calls = []
    session = object()
    monkeypatch.setattr(
        provider_health,
        "get_container",
        lambda: type(
            "Container",
            (),
            {"get_or_none": lambda self, key: session},
        )(),
    )
    monkeypatch.setattr(
        provider_health,
        "captured_profile_id",
        lambda value: "profile-captured" if value is session else "wrong",
    )
    monkeypatch.setattr(
        provider_health,
        "invoke_global_contract",
        lambda active, contract, operation, payload: calls.append(
            (active, contract, operation, payload)
        ) or {},
    )

    provider_health._invoke(
        "tobkiri.resource.credential.status.v1",
        "list",
        {},
    )

    assert calls == [
        (
            session,
            "tobkiri.resource.credential.status.v1",
            "list",
            {"profile_id": "profile-captured"},
        )
    ]


def test_provider_health_invalidates_evidence_older_than_credential(
    monkeypatch,
) -> None:
    from domain.ai_client import provider_health

    def invoke(contract_id, operation, payload):
        del operation, payload
        if contract_id == "tobkiri.resource.ai.provider.health.v1":
            return {
                "providers": [
                    {
                        "provider_instance_id": "provider.example",
                        "status": "available",
                        "observed_at": 100.0,
                        "verified": True,
                    }
                ]
            }
        return {
            "credentials": [
                {
                    "provider_instance_id": "provider.example",
                    "opaque_id": "credential-status:changed",
                    "updated_at": "1970-01-01T00:03:20Z",
                }
            ]
        }

    monkeypatch.setattr(provider_health, "_invoke", invoke)

    item = provider_health.provider_health_report(
        provider_ids=["example"]
    )["providers"][0]

    assert item["status"] == "unknown"
    assert item["runtime"]["verified"] is False
