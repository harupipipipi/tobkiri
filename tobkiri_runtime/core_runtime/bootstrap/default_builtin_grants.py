"""Default built-in grants for Authority and host capability mediation."""

from __future__ import annotations

from typing import Any

AUTHORITY_WINDOW_PRINCIPAL = "system:authority-approval-window"
HOST_CAPABILITY_BROKER_PRINCIPAL = "system:host-capability-broker"
HOST_CAPABILITIES_PACK_ID = "rumi_host_capabilities_pack"


AUTHORITY_WINDOW_PERMISSIONS = (
    "authority.request.read",
    "authority.request.list",
    "authority.request.approve",
    "authority.request.deny",
    "authority.host_intent.approve",
    "authority.host_intent.deny",
)

HOST_BROKER_PERMISSIONS = (
    "host.permission.status",
    "host.permission.open_settings",
)

HOST_CAPABILITIES_PACK_DEFAULT_GRANT_EXCLUSIONS: frozenset[str] = frozenset()

# Kept as an empty compatibility surface.  Host capabilities are not granted
# by importing this module or by naming a built-in Pack principal.
HOST_CAPABILITIES_PACK_PERMISSIONS: tuple[str, ...] = ()


DEFAULT_BUILTIN_GRANTS: tuple[dict[str, Any], ...] = ()


def default_builtin_grants_enabled() -> bool:
    """Return false because grants require an explicit Host policy edge."""

    return False


def apply_default_builtin_grants(grant_manager: Any) -> list[dict[str, Any]]:
    """Do not create hidden grants during DI/bootstrap.

    ``grant_manager`` is intentionally unused.  A signed Profile/Policy edge
    must explicitly create a grant so it remains visible, revocable, and
    scoped to the active activation.
    """

    del grant_manager
    return []
