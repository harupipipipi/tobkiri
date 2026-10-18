"""Canonical binding shared by Pack Grant issuance and approval revocation."""

from collections.abc import Mapping
from typing import Any

from tobkiri_protocol.canonical import canonical_digest


def pack_approval_snapshot_digest(
    *,
    profile_id: str,
    activation_id: str,
    plan_digest: str,
    profile_authority_digest: str,
    security_epoch: int,
    scope: Mapping[str, Any],
    approval_revision: str | None,
) -> str:
    """Keep the existing authenticated Approval snapshot format unchanged."""
    return canonical_digest({
        "ceremony": f"{profile_id}.activate",
        "activation_id": activation_id,
        "plan_digest": plan_digest,
        "profile_authority_snapshot_digest": profile_authority_digest,
        "security_epoch": security_epoch,
        "scope": dict(scope),
        "pack_approval_revision": approval_revision,
    })
