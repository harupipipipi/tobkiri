"""Share physical capture checks only within synchronous Host guard traversal."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import get_ident
from typing import Callable, Iterator


def _execution_owner() -> tuple[int, int | None]:
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return get_ident(), id(task) if task is not None else None


@dataclass
class _Traversal:
    owner: tuple[int, int | None]
    active: bool = True
    checks: dict[Callable[[], None], None] = field(default_factory=dict)


_current: ContextVar[_Traversal | None] = ContextVar(
    "host_capture_guard_traversal", default=None
)


def _check_shared_capture(check: Callable[[], None]) -> None:
    """Run each physical capture check once during the current pure traversal."""
    state = _current.get()
    if state is None or not state.active or state.owner != _execution_owner():
        check()
        return
    if check not in state.checks:
        check()
        state.checks[check] = None


@contextmanager
def _capture_guard_traversal(entry: Callable[[], None]) -> Iterator[None]:
    """Recheck shared captures at entry and exit without retaining lease results.

    Callers must contain only synchronous guard inspection. Every envelope
    lease check still executes normally. State ends before the final capture
    checks, so exceptions, copied contexts and later assertions cannot reuse it.
    """
    prior = _current.get()
    owner = _execution_owner()
    if prior is not None and prior.active and prior.owner == owner:
        yield
        return
    state = _Traversal(owner)
    token = _current.set(state)
    restored = False
    try:
        entry()
        yield
        state.active = False
        _current.reset(token)
        restored = True
        for check in state.checks:
            check()
    finally:
        state.active = False
        if not restored:
            _current.reset(token)
