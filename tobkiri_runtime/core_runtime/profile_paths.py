"""Workspace paths derived exclusively from a verified Profile v4 activation."""

from __future__ import annotations

from pathlib import Path

from .profile_workspace import validate_profile_id


def _default_user_data_root() -> Path:
    from .bootstrap.profile_capture import runtime_user_data_root

    return runtime_user_data_root()


def active_profile_id(user_data_root: Path | None = None) -> str | None:
    """Return the active verified Profile identity, never an ambient override."""

    try:
        from .bootstrap.profile_capture import capture_default_profile

        captured = capture_default_profile(base_dir=user_data_root)
    except Exception:
        return None
    profile_id = str(captured.resolved.profile["profile_id"])
    return validate_profile_id(profile_id)


def profile_workspace_dir(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Return the sole workspace root bound to a Profile v4 activation."""

    root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()
    return root / "workspaces" / validate_profile_id(profile_id)


def profile_user_data_dir(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Compatibility name for the v4 Profile workspace root."""

    return profile_workspace_dir(profile_id, user_data_root)


def profile_database_path(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Return the Profile-owned state database below its v4 workspace."""

    return profile_workspace_dir(profile_id, user_data_root) / "state" / "rumi.sqlite"


def _required_profile_id(profile_id: str | None, root: Path) -> str:
    resolved = profile_id or active_profile_id(root)
    if not resolved:
        raise RuntimeError("verified Pack v4 Profile activation is required")
    return validate_profile_id(resolved)


def resolve_runtime_user_data_dir(*, profile_id: str | None = None) -> Path:
    """Resolve runtime state to one verified v4 Profile workspace."""

    root = _default_user_data_root()
    return profile_workspace_dir(_required_profile_id(profile_id, root), root)


def resolve_runtime_database_path(*, profile_id: str | None = None) -> Path:
    """Resolve the runtime database without a process-global fallback."""

    root = _default_user_data_root()
    return profile_database_path(_required_profile_id(profile_id, root), root)


__all__ = [
    "active_profile_id",
    "profile_database_path",
    "profile_user_data_dir",
    "profile_workspace_dir",
    "resolve_runtime_database_path",
    "resolve_runtime_user_data_dir",
]
