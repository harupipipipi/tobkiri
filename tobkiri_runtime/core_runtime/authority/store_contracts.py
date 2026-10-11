"""Shared durable-store contracts without authority-store import cycles."""

from __future__ import annotations
import functools
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Concatenate, ParamSpec, TypeVar

_P = ParamSpec("_P")
_R = TypeVar("_R")


class AuthorityStoreError(RuntimeError):
    """Raised when durable authority state cannot be read or committed."""


class AuditUnavailable(AuthorityStoreError):
    """Raised when an authoritative audit reservation cannot be committed."""


@dataclass(frozen=True)
class PendingEffectUpdate:
    """One Host pending-effect CAS fused into a lease lifecycle transaction.

    The update carries the same contract as
    ``compare_and_swap_host_pending_effect``: the row is rewritten only when
    its stored revision still equals ``expected_revision`` and the outcome is
    audited — but inside the enclosing lease transaction so a crash can never
    split the lease's terminal marker from the pending-effect state.
    """

    effect_id: str
    expected_revision: int
    payload: Mapping[str, Any]


def _process_owned(
    method: Callable[Concatenate[Any, _P], _R],
) -> Callable[Concatenate[Any, _P], _R]:
    """Fence every public store entry before validation or state access."""

    @functools.wraps(method)
    def guarded(store: Any, *args: _P.args, **kwargs: _P.kwargs) -> _R:
        store._assert_current_process()
        return method(store, *args, **kwargs)

    return guarded
