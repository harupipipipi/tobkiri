"""Owned HTTP watchdog context and live fences over real local sockets."""

from contextvars import ContextVar
from pathlib import Path
import socket
from threading import Event, Thread, get_ident
from typing import Any

import pytest

from core_runtime.bootstrap import profile_capture
from core_runtime.http_request_lifetime import HttpRequestLifetime


def test_watchdog_retains_request_context_without_mutating_the_caller() -> None:
    """Every tick sees the captured binding and keeps thread-local changes local."""
    scope: ContextVar[str | None] = ContextVar("http-request-scope", default=None)
    scratch: ContextVar[str | None] = ContextVar("http-request-scratch", default=None)
    caller = get_ident()
    observations: list[tuple[str | None, str | None]] = []
    checked = Event()

    def authority() -> bool:
        if get_ident() != caller:
            observations.append((scope.get(), scratch.get()))
            scratch.set("watchdog")
            if len(observations) >= 3:
                checked.set()
        return True

    scope_token = scope.set("operation-a")
    scratch_token = scratch.set("caller")
    lifetime = HttpRequestLifetime(timeout=5, authority_check=authority)
    try:
        scope.set("operation-b")
        assert checked.wait(2)
        assert scope.get() == "operation-b"
        assert scratch.get() == "caller"
        assert observations[0] == ("operation-a", "caller")
        assert all(value == "operation-a" for value, _ in observations)
        assert all(value == "watchdog" for _, value in observations[1:])
    finally:
        lifetime.close()
        scope.reset(scope_token)
        scratch.reset(scratch_token)
    assert not lifetime._watcher.is_alive()
    assert scope.get() is None and scratch.get() is None


@pytest.mark.parametrize("ending", ["cancel", "deadline", "revoke", "guard-error"])
def test_watchdog_fences_real_socket_after_caller_context_ends(ending: str) -> None:
    """A copied binding never substitutes for fresh cancellation or authority."""
    scope: ContextVar[str | None] = ContextVar("http-socket-scope", default=None)
    caller = get_ident()
    checked, revoked, broken, cancelled = (Event() for _ in range(4))
    observed: list[str | None] = []
    clock = [1000.0]

    def authority() -> bool:
        if get_ident() != caller:
            observed.append(scope.get())
            if len(observed) >= 2:
                checked.set()
        if broken.is_set():
            raise RuntimeError("finite offline authority failed")
        return not revoked.is_set()

    left, right = socket.socketpair()
    left.settimeout(2)
    received: list[Any] = []
    read_done = Event()

    def read() -> None:
        try:
            received.append(left.recv(1))
        except OSError as error:
            received.append(type(error))
        finally:
            read_done.set()

    token = scope.set("operation-a")
    lifetime = HttpRequestLifetime(
        timeout=60, deadline=1120, clock=lambda: clock[0],
        cancellation=cancelled, authority_check=authority,
    )
    try:
        lifetime.attach(left)
    finally:
        scope.reset(token)
    reader = Thread(target=read, daemon=True)
    reader.start()
    try:
        assert scope.get() is None
        assert checked.wait(2)
        if ending == "cancel":
            cancelled.set()
        elif ending == "deadline":
            clock[0] = 1059
            assert lifetime.remaining() == 1
            clock[0] = 1060
        elif ending == "revoke":
            revoked.set()
        else:
            broken.set()
        assert read_done.wait(2)
        assert received == [b""]
        error_type = TimeoutError if ending == "deadline" else InterruptedError
        with pytest.raises(error_type):
            lifetime.check()
        assert len(observed) >= 2
        assert all(value == "operation-a" for value in observed)
    finally:
        lifetime.close()
        left.close()
        right.close()
        reader.join(2)
    assert not reader.is_alive() and not lifetime._watcher.is_alive()


@pytest.mark.parametrize(
    ("timeout", "deadline", "duration"),
    [(60, 1120, 60), (60, 1020, 20), (120, 1060, 60)],
)
def test_http_deadline_is_absolute_and_never_refreshed(
    timeout: float, deadline: float, duration: float,
) -> None:
    """Preserve the 60-second transport clamp and any shorter caller ceiling."""
    clock = [1000.0]
    lifetime = HttpRequestLifetime(
        timeout=timeout, deadline=deadline, clock=lambda: clock[0],
    )
    try:
        assert lifetime.remaining() == duration
        clock[0] += duration - 0.5
        assert lifetime.remaining() == 0.5
        assert lifetime.remaining() == 0.5
        clock[0] += 0.5
        with pytest.raises(TimeoutError):
            lifetime.check()
    finally:
        lifetime.close()
    assert not lifetime._watcher.is_alive()


def test_watchdog_reuses_profile_scope_but_rejects_a_changed_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Actual Profile scope reuse still performs each fresh pointer identity check."""
    pointer = tmp_path / "active.json"
    pointer.write_text("first-generation")
    snapshot = object()
    misses: list[None] = []

    class Pointer:
        def __init__(self, _root: Path) -> None:
            self.path = pointer

        def require(self, **_kwargs: Any) -> Any:
            misses.append(None)
            raise PermissionError("changed pointer requires fresh resolution")

    monkeypatch.setattr(profile_capture, "ActiveProfileStore", Pointer)
    monkeypatch.setattr(
        profile_capture, "_recover_bootstrap_publication", lambda **_kwargs: None,
    )
    monkeypatch.setattr(profile_capture, "_user_data_root", lambda _base: tmp_path)
    caller = get_ident()
    scopes: list[Any] = []
    checked = Event()

    def authority() -> bool:
        if get_ident() != caller:
            scopes.append(profile_capture._PROFILE_CAPTURE_SCOPE.get())
            if len(scopes) >= 3:
                checked.set()
        return profile_capture.capture_active_profile() is snapshot

    left, right = socket.socketpair()
    left.settimeout(2)
    with profile_capture.profile_capture_scope():
        profile_capture.cache_active_profile(snapshot, user_data=tmp_path)
        captured_scope = profile_capture._PROFILE_CAPTURE_SCOPE.get()
        lifetime = HttpRequestLifetime(timeout=5, authority_check=authority)
        lifetime.attach(left)
    try:
        assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None
        assert checked.wait(2)
        assert all(scope is captured_scope for scope in scopes)
        assert not misses
        pointer.write_text("changed-generation-with-different-size")
        assert left.recv(1) == b""
        assert misses
    finally:
        lifetime.close()
        left.close()
        right.close()
    assert not lifetime._watcher.is_alive()


def test_close_waits_for_finite_inflight_authority_and_reaps_the_watchdog() -> None:
    """Ending a caller cannot detach its already-running authority inspection."""
    caller = get_ident()
    entered, release, closed = (Event() for _ in range(3))

    def authority() -> bool:
        if get_ident() != caller:
            entered.set()
            assert release.wait(2)
        return True

    lifetime = HttpRequestLifetime(timeout=5, authority_check=authority)

    def close() -> None:
        lifetime.close()
        closed.set()

    closer = Thread(target=close, daemon=True)
    try:
        assert entered.wait(2)
        closer.start()
        assert not closed.wait(0.05)
        release.set()
        assert closed.wait(2)
    finally:
        release.set()
        if closer.ident is not None:
            closer.join(2)
        lifetime.close()
    assert not closer.is_alive() and not lifetime._watcher.is_alive()
