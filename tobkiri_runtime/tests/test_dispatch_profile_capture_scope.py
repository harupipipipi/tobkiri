"""Request-local captured Profile reuse without weakening live guards."""

import contextvars
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from core_runtime.bootstrap import profile_capture
from tobkiri_host.runtime import V4DispatchSession


def _session(broker, check=lambda: None):
    return V4DispatchSession(
        broker=broker,
        context_for=lambda contract, operation: SimpleNamespace(),
        effect_scope_for=lambda contract, operation, payload: {},
        providers={},
        profile_id="profile",
        plan_digest="plan",
        profile_revision="revision",
        activation_id="activation",
        security_epoch=1,
        current_capture_check=check,
    )


def test_dispatch_scope_reaches_nested_broker_threads_and_ends_between_requests():
    scopes = []
    session = None

    def invoke(frame, context, **kwargs):
        cache = profile_capture._PROFILE_CAPTURE_SCOPE.get()
        assert cache is not None
        scopes.append(cache)
        if not frame.payload.get("nested"):
            copied = contextvars.copy_context()
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    copied.run, session.invoke, "contract", "operation", {"nested": True}
                ).result(timeout=2)
            assert result == {"nested": True}
        return dict(frame.payload)

    session = _session(SimpleNamespace(invoke=invoke))
    assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None
    session.invoke("contract", "operation", {"nested": False})
    assert scopes[0] is scopes[1]
    assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None
    session.invoke("contract", "operation", {"nested": False})
    assert scopes[2] is scopes[3] and scopes[2] is not scopes[0]
    assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None


def test_dispatch_scope_preserves_fresh_guard_errors_and_releases_on_failure():
    epoch = [1]
    checks = []
    session = None

    def check():
        checks.append(epoch[0])
        if epoch[0] != 1:
            raise PermissionError("security epoch changed")

    def invoke(*args, **kwargs):
        session.assert_current()
        epoch[0] = 2
        session.assert_current()
        return {}

    session = _session(SimpleNamespace(invoke=invoke), check)
    with pytest.raises(PermissionError, match="security epoch"):
        session.invoke("contract", "operation", {})
    assert checks == [1, 2]
    assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None


@pytest.mark.parametrize("fresh", [False, True])
def test_cached_profile_still_rechecks_pointer_identity(
    tmp_path: Path, monkeypatch, fresh: bool,
):
    pointer = tmp_path / "active.json"
    pointer.write_text("initial")
    snapshot = object()

    class Pointer:
        def __init__(self, root):
            self.path = pointer

        def require(self, **kwargs):
            raise RuntimeError("fresh pointer must be resolved")

    monkeypatch.setattr(profile_capture, "ActiveProfileStore", Pointer)
    monkeypatch.setattr(profile_capture, "_recover_bootstrap_publication", lambda **kwargs: None)
    monkeypatch.setattr(profile_capture, "_user_data_root", lambda base_dir: tmp_path)
    with profile_capture.profile_capture_scope(fresh=fresh):
        profile_capture.cache_active_profile(snapshot, user_data=tmp_path)
        assert profile_capture.capture_active_profile() is snapshot
        pointer.write_text("changed pointer")
        with pytest.raises(RuntimeError, match="fresh pointer"):
            profile_capture.capture_active_profile()


@pytest.mark.parametrize("fails", [False, True])
def test_fresh_scope_shares_with_nested_workers_and_restores_caller(fails):
    """Prepared dispatch cannot reuse a caller snapshot or leak it on failure."""
    scopes = []
    with ThreadPoolExecutor(max_workers=1) as pool:
        with profile_capture.profile_capture_scope():
            caller = profile_capture._PROFILE_CAPTURE_SCOPE.get()
            caller[Path("approval-time-pointer")] = (object(), (1, 2, 3, 4))
            for _ in range(2):
                try:
                    with profile_capture.profile_capture_scope(fresh=True):
                        current = profile_capture._PROFILE_CAPTURE_SCOPE.get()
                        assert current == {} and current is not caller
                        assert all(current is not previous for previous in scopes)
                        scopes.append(current)
                        copied = contextvars.copy_context()
                        observed = pool.submit(
                            copied.run, profile_capture._PROFILE_CAPTURE_SCOPE.get
                        ).result(timeout=2)
                        assert observed is current
                        if fails:
                            raise RuntimeError("fixture dispatch failed")
                except RuntimeError as error:
                    assert fails and str(error) == "fixture dispatch failed"
                assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is caller
                assert pool.submit(
                    profile_capture._PROFILE_CAPTURE_SCOPE.get
                ).result(timeout=2) is None
        assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None


@pytest.mark.parametrize("fresh", [False, True])
def test_scope_keeps_live_guard_failure_inside_caller_scope(fresh):
    """A reused Profile snapshot never substitutes for current Authority checks."""
    epoch = [1]
    checks = []

    def check():
        checks.append(epoch[0])
        if epoch[0] != 1:
            raise PermissionError("security epoch changed")

    def invoke(*args, **kwargs):
        session.assert_current()
        epoch[0] = 2
        session.assert_current()

    session = _session(SimpleNamespace(invoke=invoke), check)
    with profile_capture.profile_capture_scope():
        caller = profile_capture._PROFILE_CAPTURE_SCOPE.get()
        with pytest.raises(PermissionError, match="security epoch"):
            with profile_capture.profile_capture_scope(fresh=fresh):
                session.invoke("contract", "operation", {})
        assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is caller
    assert checks == [1, 2]
    assert profile_capture._PROFILE_CAPTURE_SCOPE.get() is None
