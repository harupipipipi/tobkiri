"""Thread-safe access to the sole verified Pack v4 activation snapshot."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from threading import RLock
from typing import Any


@dataclass(frozen=True)
class V4PackView:
    """Non-authoritative compatibility view of one effective v4 Pack."""

    pack_id: str
    version: str
    manifest_hash: str
    content_hash: str


@dataclass(frozen=True)
class V4ProviderView:
    """Non-secret provider identity copied from one ResolvedPlan binding."""

    contract_id: str
    provider_instance_id: str
    source_pack_id: str
    version: str
    content_hash: str


@dataclass(frozen=True)
class V4ResolvedProfileView:
    """Read-only structural view backed only by a verified v4 activation."""

    profile_id: str
    profile_revision: str
    plan_hash: str
    effective_pack_set: tuple[str, ...]
    packs: tuple[V4PackView, ...]
    providers: tuple[V4ProviderView, ...]
    projections: tuple[Any, ...] = ()
    effective_permissions: tuple[str, ...] = ()


_ACTIVE_PROFILE: ContextVar[Any | None] = ContextVar(
    "tobkiri_active_resolved_profile",
    default=None,
)
_PERSISTED_PROFILE_LOCK = RLock()
_PERSISTED_PROFILE_CACHE: tuple[tuple[str, int], V4ResolvedProfileView] | None = None
_PERSISTED_PROFILE_INVALIDATION_REVISION = 0


def activate_resolved_profile(plan: Any) -> Token[Any | None]:
    """Bind an already-verified plan view to the current execution context."""

    return _ACTIVE_PROFILE.set(plan)


def restore_resolved_profile(token: Token[Any | None]) -> None:
    """Restore the prior verified plan view."""

    _ACTIVE_PROFILE.reset(token)


def active_resolved_profile() -> Any | None:
    """Return the explicitly bound plan view, if any."""

    return _ACTIVE_PROFILE.get()


def invalidate_persisted_resolved_profile() -> None:
    """Invalidate the worker cache after an Authority/Profile transaction."""

    global _PERSISTED_PROFILE_CACHE
    global _PERSISTED_PROFILE_INVALIDATION_REVISION
    with _PERSISTED_PROFILE_LOCK:
        _PERSISTED_PROFILE_INVALIDATION_REVISION += 1
        _PERSISTED_PROFILE_CACHE = None


def persisted_resolved_profile() -> Any | None:
    """Load only the committed, digest-bound Pack v4 activation.

    No startup JSON, active-ecosystem file, installed-Pack scan, approval
    registry, or legacy manifest participates in this recovery path.
    """

    global _PERSISTED_PROFILE_CACHE
    active = active_resolved_profile()
    if active is not None:
        return active
    try:
        from .bootstrap.profile_capture import capture_default_profile

        captured = capture_default_profile()
        activation_id = str(captured.activation["activation_id"])
        cache_key = (activation_id, _PERSISTED_PROFILE_INVALIDATION_REVISION)
        with _PERSISTED_PROFILE_LOCK:
            cached = _PERSISTED_PROFILE_CACHE
            if cached is not None and cached[0] == cache_key:
                return cached[1]
        view = _view_from_activation(captured)
        with _PERSISTED_PROFILE_LOCK:
            _PERSISTED_PROFILE_CACHE = (cache_key, view)
        return view
    except Exception:
        return None


def _view_from_activation(captured: Any) -> V4ResolvedProfileView:
    profile = captured.resolved.profile
    lock = captured.resolved.lock
    plan = captured.resolved.plan
    effective = tuple(str(item["identity"]) for item in lock["effective_set"])
    packs = tuple(
        V4PackView(
            pack_id=str(item["identity"]),
            version="",
            manifest_hash=str(item["artifact_digest"]),
            content_hash=str(item["artifact_digest"]),
        )
        for item in lock["effective_set"]
    )
    providers = tuple(
        V4ProviderView(
            contract_id=str(binding["contract_id"]),
            provider_instance_id=str(binding["function_principal"]["function_id"]),
            source_pack_id=str(binding["pack_id"]),
            version="",
            content_hash=str(binding["artifact_digest"]),
        )
        for binding in plan["bindings"]
    )
    return V4ResolvedProfileView(
        profile_id=str(profile["profile_id"]),
        profile_revision=str(plan["profile_revision"]),
        plan_hash=str(plan["plan_digest"]),
        effective_pack_set=effective,
        packs=packs,
        providers=providers,
    )


def effective_pack_ids() -> frozenset[str]:
    """Return exactly the effective set in the active Pack v4 lock."""

    plan = persisted_resolved_profile()
    return frozenset(plan.effective_pack_set) if plan is not None else frozenset()


def require_effective_pack(pack_id: str) -> None:
    """Fail closed when a Pack is outside the active Pack v4 lock."""

    plan = persisted_resolved_profile()
    if plan is None:
        raise RuntimeError("Pack v4 resolved profile is not active")
    if pack_id not in plan.effective_pack_set:
        raise PermissionError(
            f"Pack is outside resolved Profile {plan.plan_hash}: {pack_id}"
        )


__all__ = [
    "V4PackView",
    "V4ProviderView",
    "V4ResolvedProfileView",
    "activate_resolved_profile",
    "active_resolved_profile",
    "effective_pack_ids",
    "invalidate_persisted_resolved_profile",
    "persisted_resolved_profile",
    "require_effective_pack",
    "restore_resolved_profile",
]
