"""Defaultspack-owned HTTP targets for the Profile ceremony surface."""

from __future__ import annotations

from core_runtime.global_contracts.http_contract_dispatch import (
    HTTPContractBinding,
    HTTPContractTarget,
)


HOST_PROFILE_CONTROL_OPERATIONS = frozenset(
    {
        "profile.catalog.read",
        "profile.change.resolve",
        "profile.change.review",
        "profile.change.approve",
        "profile.change.activate",
        "operation.status.read",
    }
)


def host_profile_control_bindings() -> tuple[HTTPContractBinding, ...]:
    """Return Defaultspack's pre-activation Profile ceremony bindings.

    The generic Host only receives these immutable targets by explicit
    composition.  They cannot be inferred from an application Pack map.
    """

    routes = (
        ("GET", "/api/runtime-surface/profiles", "profile.catalog.read", ()),
        (
            "GET",
            "/api/runtime-surface/operation-status",
            "operation.status.read",
            ("request_id",),
        ),
        (
            "POST",
            "/api/runtime-surface/profile-change/resolve",
            "profile.change.resolve",
            (
                "profile_id",
                "expected_profile_revision",
                "expected_plan_digest",
                "desired_pack_ids",
                "profile_definition_digest",
                "profile_catalog_digest",
                "bundle_lock_digest",
            ),
        ),
        (
            "POST",
            "/api/runtime-surface/profile-change/review",
            "profile.change.review",
            ("candidate_id", "candidate_digest"),
        ),
        (
            "POST",
            "/api/runtime-surface/profile-change/approve",
            "profile.change.approve",
            ("candidate_id", "candidate_digest"),
        ),
        (
            "POST",
            "/api/runtime-surface/profile-change/activate",
            "profile.change.activate",
            ("approval_id", "approval_digest"),
        ),
    )
    return tuple(
        HTTPContractBinding(
            method=method,
            path=path,
            presentation="broker_result",
            targets=(
                HTTPContractTarget(
                    contribution_id=f"host.profile-control.{operation_id}",
                    contract_id="tobkiri.host.control-presentation.v4",
                    operation_id=operation_id,
                    provider_id="tobkiri.host.control-presentation",
                    function_id="tobkiri.host.control-presentation",
                    allowed_payload_keys=frozenset(allowed),
                    owner_pack_id="host",
                ),
            ),
        )
        for method, path, operation_id, allowed in routes
    )


__all__ = ["HOST_PROFILE_CONTROL_OPERATIONS", "host_profile_control_bindings"]
