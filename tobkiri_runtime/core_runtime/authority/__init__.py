"""Authority data types retained after the v4 execution-authority cutover."""

from __future__ import annotations

from .models import AuthorityDecision, AuthorityRequest, AuthorityResource
from .principal import build_principal_id


def get_authority_service() -> None:
    """Reject access to the removed execution-authority singleton."""
    raise RuntimeError(
        "legacy authority service is unavailable; use AuthorityV4Adapter "
        "through the captured V4DispatchSession"
    )


__all__ = [
    "AuthorityDecision",
    "AuthorityRequest",
    "AuthorityResource",
    "build_principal_id",
    "get_authority_service",
]
