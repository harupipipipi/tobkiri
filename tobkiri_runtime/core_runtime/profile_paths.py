"""Workspace paths derived exclusively from a verified Profile v4 activation."""

from __future__ import annotations

from pathlib import Path

from .profile_workspace import ProfileWorkspaceManager, validate_profile_id


def _default_user_data_root() -> Path:
    from .bootstrap.profile_capture import runtime_user_data_root

    return runtime_user_data_root()


def active_profile_id(user_data_root: Path | None = None) -> str | None:
    """Return the active verified Profile identity, never an ambient override."""

    try:
        from .active_profile_store_v4 import ActiveProfileStore

        root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()
        profile_id = ActiveProfileStore(root).require(verify_snapshot=True).profile_id
    except Exception:
        return None
    return validate_profile_id(profile_id)


def profile_workspace_dir(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Return the sole workspace root bound to a Profile v4 activation."""

    root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()
    return ProfileWorkspaceManager(root).root_for_profile(profile_id)


def profile_user_data_dir(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Return the Profile-owned state directory below its v4 workspace."""

    root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()
    return ProfileWorkspaceManager(root).profile_user_data_dir(profile_id)


def profile_database_path(
    profile_id: str,
    user_data_root: Path | None = None,
) -> Path:
    """Return the Profile-owned state database below its v4 workspace."""

    root = Path(user_data_root) if user_data_root is not None else _default_user_data_root()
    return ProfileWorkspaceManager(root).profile_database_path(profile_id)


def _required_profile_id(profile_id: str | None, root: Path) -> str:
    resolved = profile_id or active_profile_id(root)
    if not resolved:
        raise RuntimeError("verified Pack v4 Profile activation is required")
    return validate_profile_id(resolved)


def resolve_runtime_user_data_dir(*, profile_id: str | None = None) -> Path:
    """Resolve runtime state to one verified v4 Profile state directory."""

    root = _default_user_data_root()
    return profile_user_data_dir(_required_profile_id(profile_id, root), root)


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
