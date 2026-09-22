"""Expose the registry Pack's built-in connection provider catalog."""

from __future__ import annotations

from core_runtime.connections.models import ConnectionProvider

from .providers import (
    CLOUDFLARE_PROVIDER,
    CODEX_PROVIDER,
    GITHUB_PROVIDER,
    GOOGLE_PROVIDER,
)


def builtin_connection_providers() -> tuple[ConnectionProvider, ...]:
    """Return the immutable built-in catalog in user-visible priority order."""

    return tuple(
        sorted(
            (
                CLOUDFLARE_PROVIDER,
                GOOGLE_PROVIDER,
                GITHUB_PROVIDER,
                CODEX_PROVIDER,
            ),
            key=lambda provider: provider.priority,
        )
    )


__all__ = [
    "CLOUDFLARE_PROVIDER",
    "CODEX_PROVIDER",
    "GITHUB_PROVIDER",
    "GOOGLE_PROVIDER",
    "builtin_connection_providers",
]
