"""Small test builders for the Launcher-owned Host contract."""

from __future__ import annotations

from typing import Any, Mapping


def host_contract(
    *,
    profile_id: str = "defaults",
    profile_revision: str = "sha256:" + "1" * 64,
    activation_id: str = "activation:test-fixture",
    plan_digest: str = "sha256:" + "2" * 64,
    values: Mapping[str, str] | None = None,
    provider_id: str | None = None,
) -> dict[str, Any]:
    """Build a schema-valid four-field contract for unit fixtures."""

    payload: dict[str, Any] = {
        "schema_version": "tobkiri.host-contract.v1",
        "profile_id": profile_id,
        "profile_revision": profile_revision,
        "activation_id": activation_id,
        "plan_digest": plan_digest,
        "values": dict(values or {}),
    }
    if provider_id is not None:
        payload["provider_id"] = provider_id
    return payload


def host_contract_for_session(
    session: object,
    *,
    values: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a contract whose identity exactly matches a captured session."""

    return host_contract(
        profile_id=str(getattr(session, "profile_id")),
        profile_revision=str(getattr(session, "profile_revision")),
        activation_id=str(getattr(session, "activation_id")),
        plan_digest=str(getattr(session, "plan_digest")),
        values=values,
    )


__all__ = ["host_contract", "host_contract_for_session"]
