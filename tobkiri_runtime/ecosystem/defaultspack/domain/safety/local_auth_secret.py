"""Read legacy launcher authentication secrets at the safety boundary."""

from __future__ import annotations

import os


def configured_local_auth_environment_tokens() -> tuple[str, ...]:
    """Return deduplicated launcher tokens retained for migration compatibility."""
    tokens: list[str] = []
    seen: set[str] = set()
    for key in ("RUMI_DEFAULTSPACK_LOCAL_TOKEN", "RUMI_API_TOKEN", "RUMI_TOKEN"):
        value = os.environ.get(key, "").strip()
        if value and value not in seen:
            seen.add(value)
            tokens.append(value)
    return tuple(tokens)
