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


def test_cached_profile_still_rechecks_pointer_identity(tmp_path: Path, monkeypatch):
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
    with profile_capture.profile_capture_scope():
        profile_capture.cache_active_profile(snapshot, user_data=tmp_path)
        assert profile_capture.capture_active_profile() is snapshot
        pointer.write_text("changed pointer")
        with pytest.raises(RuntimeError, match="fresh pointer"):
            profile_capture.capture_active_profile()
