"""Legacy application access to the shared settings owner."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .frontend_settings_store import (
    FrontendSettingsCorruptError,
    FrontendSettingsStore,
    defaultspack_frontend_settings_path,
)


def frontend_settings_path(pack_root: Path | None = None) -> Path:
    """Resolve the legacy path; callers must not implement their own file reads."""
    return defaultspack_frontend_settings_path(
        Path(pack_root) if pack_root is not None else None
    )


def read_optional_frontend_settings(pack_root: Path | None = None) -> dict[str, Any]:
    """Read legacy optional preferences without recovery or filesystem writes.

    Preserve the existing optional-reader fallback for unreadable/corrupt local
    preferences. This is not an authorization decision or a public Host API;
    captured contract failures must not be translated into this fallback.
    """
    try:
        return FrontendSettingsStore(frontend_settings_path(pack_root)).read_snapshot()
    except (OSError, FrontendSettingsCorruptError):
        return {}
