"""Production invariants separating Tauri shell, runtime, and development roles."""

from __future__ import annotations

from typing import Any, Mapping

from .errors import ResolutionError


TAURI_SHELL_PREFIX = "shell.tauri."
TAURI_RUNTIME_PREFIX = "runtime.tauri.application."
TAURI_DEV_PREFIX = "dev.tauri.toolchain."


def validate_production_tauri_roles(
    profile: Mapping[str, Any],
    lock: Mapping[str, Any],
) -> None:
    """Reject development toolchains and enforce a selected runtime application."""
    selected = {
        str(item.get("pack_id"))
        for item in profile.get("packs", [])
        if isinstance(item, Mapping)
    }
    effective = {
        str(item.get("identity"))
        for item in lock.get("effective_set", [])
        if isinstance(item, Mapping)
    }
    shell = profile.get("shell")
    shell_pack = (
        str(shell.get("pack_id")) if isinstance(shell, Mapping) else ""
    )
    all_ids = selected | effective | ({shell_pack} if shell_pack else set())
    if any(item.startswith(TAURI_DEV_PREFIX) for item in all_ids):
        raise ResolutionError(
            "Development Realm Tauri toolchain cannot enter production effective set"
        )
    if shell_pack.startswith(TAURI_SHELL_PREFIX):
        runtimes = {
            item for item in effective if item.startswith(TAURI_RUNTIME_PREFIX)
        }
        if len(runtimes) != 1 or not runtimes <= selected:
            raise ResolutionError(
                "Tauri shell requires exactly one selected runtime application"
            )
        if shell_pack in runtimes:
            raise ResolutionError("Tauri shell and runtime application must be separate")


__all__ = ["validate_production_tauri_roles"]
