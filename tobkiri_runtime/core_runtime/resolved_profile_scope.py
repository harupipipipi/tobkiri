"""Revision-bound access to the active immutable resolved profile."""

from __future__ import annotations

from contextvars import ContextVar, Token
from pathlib import Path
import json
from dataclasses import replace
from threading import RLock
from typing import TYPE_CHECKING

from backend_core.ecosystem.active_ecosystem import ActiveEcosystemManager

from .paths import USER_DATA_DIR

if TYPE_CHECKING:
    from .resolved_profile import ResolvedProfile


_ACTIVE_PROFILE: ContextVar[ResolvedProfile | None] = ContextVar(
    "tobkiri_active_resolved_profile",
    default=None,
)
_PERSISTED_PROFILE_LOCK = RLock()
_PERSISTED_PROFILE_CACHE: tuple[tuple[int, int], ResolvedProfile] | None = None


def activate_resolved_profile(plan: ResolvedProfile) -> Token[ResolvedProfile | None]:
    """Bind subsequent loaders and calls to one immutable plan revision."""
    return _ACTIVE_PROFILE.set(plan)


def restore_resolved_profile(token: Token[ResolvedProfile | None]) -> None:
    """Restore the prior plan after a revision-bound execution scope."""
    _ACTIVE_PROFILE.reset(token)


def active_resolved_profile() -> ResolvedProfile | None:
    """Return the current plan, or ``None`` before startup resolution."""
    return _ACTIVE_PROFILE.get()


def persisted_resolved_profile() -> ResolvedProfile | None:
    """Recover the verified active plan for a request worker.

    ``ContextVar`` bindings are intentionally thread-local. HTTP handlers run
    after startup on different threads, so they must reconstruct the same
    persisted profile rather than treating every global contract as absent.
    Pack approval remains part of the reconstructed plan.
    """
    global _PERSISTED_PROFILE_CACHE

    active = active_resolved_profile()
    if active is not None:
        return active
    try:
        from .approval_manager import get_approval_manager
        from .resolved_profile import (
            resolution_input_from_startup_profile,
            resolve_profile,
        )

        state_path = Path(USER_DATA_DIR) / "settings" / "startup_profiles.json"
        stat = state_path.stat()
        cache_key = (stat.st_mtime_ns, stat.st_size)
        with _PERSISTED_PROFILE_LOCK:
            cached = _PERSISTED_PROFILE_CACHE
            if cached is not None and cached[0] == cache_key:
                return cached[1]
        state = json.loads(state_path.read_text(encoding="utf-8"))
        active_profile_id = str(state.get("active_profile_id") or "")
        profiles = state.get("profiles")
        profile = next(
            (
                item for item in profiles if isinstance(item, dict)
                and item.get("profile_id") == active_profile_id
            ),
            None,
        ) if isinstance(profiles, list) else None
        if not isinstance(profile, dict):
            return None
        provisional_input = resolution_input_from_startup_profile(profile)
        provisional = resolve_profile(provisional_input)
        approval_manager = get_approval_manager()
        verified_pack_trust = approval_manager.get_verified_pack_trust(
            provisional.selected_pack_ids
        )
        resolution_input = resolution_input_from_startup_profile(
            profile,
            verified_pack_trust=verified_pack_trust,
        )
        resolved = resolve_profile(
            replace(
                resolution_input,
                authorized_pack_ids=tuple(verified_pack_trust),
            )
        )
        with _PERSISTED_PROFILE_LOCK:
            _PERSISTED_PROFILE_CACHE = (cache_key, resolved)
        return resolved
    except Exception:
        return None


def _persisted_startup_pack_ids() -> list[str]:
    """Read the launch profile from the configured user-data root.

    ``ActiveEcosystemManager()`` without a path uses the legacy mount root.
    That root is the bundled application directory, not ``RUMI_USER_DATA``
    in the desktop launcher.  Always use the explicit configured path here so
    worker threads recover the same Defaults Profile the launcher activated.
    """
    config_path = Path(USER_DATA_DIR) / "active_ecosystem.json"
    active = ActiveEcosystemManager(config_path=str(config_path))
    pack_ids = active.get_metadata("startup_packs", [])
    return [pack_id for pack_id in pack_ids if isinstance(pack_id, str)]


def effective_pack_ids() -> frozenset[str]:
    """Return the only pack scope runtime resource loaders may consume."""
    plan = persisted_resolved_profile()
    if plan is not None:
        return frozenset(plan.effective_pack_set)

    # Startup resources are initialized on worker threads where ContextVar
    # bindings are not inherited.  Until the immutable plan is re-bound in
    # that thread, recover the persisted startup-profile scope and keep the
    # same approval-and-hash verification boundary.  This prevents a valid
    # Defaults Profile from degrading to first-party memo tools only.
    try:
        from .approval_manager import get_approval_manager

        pack_ids = _persisted_startup_pack_ids()
        approval_manager = get_approval_manager()
        return frozenset(
            pack_id
            for pack_id in pack_ids
            if approval_manager.is_pack_approved_and_verified(pack_id)[0]
        )
    except Exception:
        return frozenset()


def require_effective_pack(pack_id: str) -> None:
    """Fail closed when a caller tries to consume an out-of-plan pack."""
    plan = active_resolved_profile()
    if plan is None:
        raise RuntimeError("resolved profile is not active")
    if pack_id not in plan.effective_pack_set:
        raise PermissionError(
            f"pack is outside resolved profile {plan.plan_hash}: {pack_id}"
        )
