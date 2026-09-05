"""Compatibility entrypoint for the shared Defaultspack settings path."""

from __future__ import annotations

from pathlib import Path

from domain.frontend_settings_store import defaultspack_frontend_settings_path


def frontend_settings_path(pack_root: Path | None = None) -> Path:
    return defaultspack_frontend_settings_path(
        Path(pack_root) if pack_root is not None else None
    )
