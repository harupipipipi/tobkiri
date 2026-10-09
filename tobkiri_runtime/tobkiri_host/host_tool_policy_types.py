"""Shared private Host policy capture and exact settlement types."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Callable, Mapping


@dataclass(frozen=True)
class HostCapturedToolPolicy:
    """Authenticated saved-root stamp and separately bound native policy."""

    mode: str
    selection_id: str
    workspace_root: str
    capture_digest: str
    turn_id: str
    assert_current: Callable[[], None]
    native_boundary_digest: str = ""


@dataclass(frozen=True)
class SettledPolicyOperation:
    """Exact tagged scope and repeatable proof for actual Broker execution."""

    scope: Mapping[str, Any]
    grant_id: str
    operation_capture_digest: str
    assert_current: Callable[[], None]
