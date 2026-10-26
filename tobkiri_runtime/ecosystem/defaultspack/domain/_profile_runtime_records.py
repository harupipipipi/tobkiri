"""Pure Profile records shared before the native activation service is reached.

The service re-exports these owned definitions. Their public module names stay
compatible with existing error, record and pickle consumers.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import FunctionType as _FunctionType
from typing import Any, Mapping

from .profile_resolution import ProfileResolutionDenied, ResolvedDefaultProfile


class ProfileReconfirmationRequired(ProfileResolutionDenied):
    """Raised when a valid predecessor cannot authorize a changed Profile."""

    verified_profile_definition_digest: str | None = None
    verified_profile: Mapping[str, Any] | None = None
    verified_activation_identity: tuple[str, str, str, str] | None = None


class ActivationLockTimeout(ProfileResolutionDenied):
    """Raised when the activation process lock is unavailable by its deadline."""


class ArtifactVerificationTimeout(ActivationLockTimeout):
    """Raised when a shared artifact verification misses its bounded wait."""


@dataclass(frozen=True)
class ActiveDefaultProfile:
    """Restart-safe v4 records plus their exact ActivationRecord."""

    resolved: ResolvedDefaultProfile
    activation: Mapping[str, Any]


_LEGACY_MODULE = "ecosystem.defaultspack.domain.runtime_v4.service"
_OWNED_DEFINITIONS = (
    ProfileReconfirmationRequired,
    ActivationLockTimeout,
    ArtifactVerificationTimeout,
    ActiveDefaultProfile,
)
for _definition in _OWNED_DEFINITIONS:
    _definition.__module__ = _LEGACY_MODULE
    for _member in vars(_definition).values():
        if isinstance(_member, _FunctionType):
            _function = _member
            while (
                isinstance(_function, _FunctionType)
                and _function.__module__ == __name__
            ):
                _function.__module__ = _LEGACY_MODULE
                _function = getattr(_function, "__wrapped__", None)

del _definition, _member, _function
