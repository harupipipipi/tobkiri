"""Privacy, boundedness and outcome regressions for Profile timing diagnostics."""

from __future__ import annotations

from contextvars import copy_context
from enum import Enum
import threading
from typing import Any, Callable

import pytest

import core_runtime.profile_operation_timing as timing


class _Control:
    def __init__(self, callback: Callable[[Any], Any]) -> None:
        self.callback = callback

    @timing.timed_profile_control_operation
    def invoke(self, contract_id: str, operation_id: str, payload: Any) -> Any:
        return self.callback(payload)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == timing.__name__]


def test_fixed_projection_excludes_all_private_input_and_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    private = "SECRET-payload-key-session-digest-path"
    result = {"code": "TIMEOUT", "message": private, "candidate_id": private}
    control = _Control(lambda payload: result)
    assert control.invoke(private, "profile.change.review", {private: private}) is result
    messages = _messages(caplog)
    assert len(messages) == 2
    assert "operation=profile_review" in messages[-1]
    assert "timeout_code=TIMEOUT" in messages[-1]
    assert private not in "\n".join(messages)


@pytest.mark.parametrize("code", ["SECRET-error-code", object(), {"TIMEOUT": True}])
def test_unrecognized_code_is_never_logged(
    code: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    control = _Control(lambda payload: {"code": code})
    control.invoke("private-contract", "profile.change.resolve", {})
    assert "timeout_code=none" in _messages(caplog)[-1]
    assert "SECRET" not in "\n".join(_messages(caplog))


def test_typed_timeout_preserves_original_exception_identity(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""

    class Code(str, Enum):
        TIMEOUT = "TIMEOUT"

    error = RuntimeError("SECRET-exception-message")
    error.code = Code.TIMEOUT

    def fail(payload: Any) -> None:
        with timing.profile_operation_phase("active_snapshot"):
            raise error

    with pytest.raises(RuntimeError) as caught:
        _Control(fail).invoke("private-contract", "profile.change.review", {})
    assert caught.value is error
    messages = _messages(caplog)
    assert "phase=active_snapshot.finish" in messages[-2]
    assert "timeout_code=TIMEOUT" in messages[-2]
    assert "SECRET" not in "\n".join(messages)


def test_builtin_timeout_has_fixed_code_without_exception_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    error = TimeoutError("SECRET-deadline")

    def fail(payload: Any) -> None:
        raise error

    with pytest.raises(TimeoutError) as caught:
        _Control(fail).invoke("private-contract", "profile.read", {})
    assert caught.value is error
    assert "timeout_code=TIMEOUT" in _messages(caplog)[-1]
    assert "SECRET" not in "\n".join(_messages(caplog))


def test_logs_are_bounded_with_terminal_record_reserved(caplog: pytest.LogCaptureFixture) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""

    def many_phases(payload: Any) -> Any:
        phases = ("artifact_catalog", "capability_binding", "packvm_readiness")
        for index in range(100):
            with timing.profile_operation_phase(phases[index % len(phases)]):
                pass
        return {"code": "TIMEOUT"}

    _Control(many_phases).invoke("private-contract", "profile.catalog.read", {})
    messages = _messages(caplog)
    assert len(messages) == timing.MAX_TIMING_LOG_LINES
    assert "phase=dispatch.finish" in messages[-1]
    assert "timeout_code=TIMEOUT" in messages[-1]


def test_no_trace_or_unknown_labels_cannot_emit_private_labels(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    with timing.profile_operation_phase("active_snapshot"):
        pass
    assert not _messages(caplog)

    def callback(payload: Any) -> Any:
        with timing.profile_operation_phase("SECRET-phase-name"):
            pass
        return payload

    payload = {"SECRET": "SECRET"}
    assert _Control(callback).invoke("contract", "SECRET-operation-name", payload) is payload
    assert not _messages(caplog)
    _Control(callback).invoke("contract", "profile.change.review", payload)
    assert len(_messages(caplog)) == 2
    assert "SECRET" not in "\n".join(_messages(caplog))


def test_reader_remains_fresh_and_none_preserves_composition_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    calls = []

    def reader() -> int:
        calls.append(len(calls))
        return calls[-1]

    wrapped = timing.timed_profile_reader(reader, "active_snapshot")
    assert timing.timed_profile_reader(None, "active_snapshot") is None
    assert wrapped is not None

    def callback(payload: Any) -> Any:
        return wrapped(), wrapped()

    assert _Control(callback).invoke("contract", "profile.change.review", {}) == (0, 1)
    assert calls == [0, 1]
    assert sum("phase=active_snapshot.finish" in m for m in _messages(caplog)) == 2


@pytest.mark.parametrize(
    "phase", ["active_snapshot", "capability_binding", "packvm_readiness"]
)
def test_copied_worker_context_closes_without_late_or_leaked_logs(
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    started = threading.Event()
    release = threading.Event()
    threads = []

    def callback(payload: Any) -> Any:
        def worker() -> None:
            with timing.profile_operation_phase(phase):
                started.set()
                assert release.wait(timeout=5)

        thread = threading.Thread(target=copy_context().run, args=(worker,))
        threads.append(thread)
        thread.start()
        assert started.wait(timeout=5)
        return {"code": "TIMEOUT"}

    try:
        _Control(callback).invoke("contract", "profile.change.review", {})
        completed = _messages(caplog)
        assert any(f"phase={phase}.start" in m for m in completed)
        assert "phase=dispatch.finish" in completed[-1]
    finally:
        release.set()
        for thread in threads:
            thread.join(timeout=5)
            assert not thread.is_alive()
    assert _messages(caplog) == completed
    with timing.profile_operation_phase(phase):
        pass
    assert _messages(caplog) == completed


@pytest.mark.parametrize("phase", ["capability_binding", "packvm_readiness"])
def test_presentation_reader_timing_preserves_fresh_values_without_private_output(
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Each wrapped callback runs fresh and projects only its fixed phase."""

    calls: list[object] = []

    def reader() -> object:
        result = {"SECRET-path-payload": "SECRET-header-error"}
        calls.append(result)
        return result

    wrapped = (
        timing.profile_operation_phase(phase)(reader)
        if phase == "capability_binding"
        else timing.timed_profile_reader(reader, phase)
    )
    assert wrapped is not None
    assert timing.timed_profile_reader(None, phase) is None
    assert wrapped() is calls[0]
    assert not _messages(caplog)
    control = _Control(lambda payload: wrapped())
    assert control.invoke("SECRET-contract", "profile.read", {}) is calls[1]
    assert control.invoke("SECRET-contract", "profile.read", {}) is calls[2]
    assert calls[1] is not calls[2]
    messages = _messages(caplog)
    assert sum(f"phase={phase}.finish" in item for item in messages) == 2
    assert "SECRET" not in "\n".join(messages)


@pytest.mark.parametrize("phase", ["capability_binding", "packvm_readiness"])
def test_presentation_reader_timing_preserves_exception_and_timeout_identity(
    phase: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Timing preserves callback errors while logging only a fixed timeout code."""

    error = TimeoutError("SECRET-private-path-header")

    def reader() -> None:
        raise error

    wrapped = (
        timing.profile_operation_phase(phase)(reader)
        if phase == "capability_binding"
        else timing.timed_profile_reader(reader, phase)
    )
    assert wrapped is not None
    with pytest.raises(TimeoutError) as caught:
        _Control(lambda payload: wrapped()).invoke("SECRET-contract", "profile.read", {})
    assert caught.value is error
    messages = _messages(caplog)
    assert f"phase={phase}.finish" in messages[-2]
    assert "timeout_code=TIMEOUT" in messages[-2]
    assert "SECRET" not in "\n".join(messages)


def test_logging_failure_does_not_change_return_or_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""

    def broken_logger(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("logger unavailable")

    monkeypatch.setattr(timing.logger, "warning", broken_logger)
    result = object()
    assert (
        _Control(lambda payload: result).invoke("contract", "profile.change.resolve", {}) is result
    )
    error = TimeoutError("deadline")

    def fail(payload: Any) -> None:
        raise error

    with pytest.raises(TimeoutError) as caught:
        _Control(fail).invoke("contract", "profile.change.review", {})
    assert caught.value is error


def test_elapsed_time_is_nonnegative_and_bounded(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    stamps = iter([10, 0, 100_000_000_000_000_000])
    monkeypatch.setattr(timing.time, "monotonic_ns", lambda: next(stamps))
    _Control(lambda payload: {}).invoke("contract", "profile.change.review", {})
    messages = _messages(caplog)
    assert "elapsed_ms=0" in messages[0]
    assert "elapsed_ms=86400000" in messages[-1]


def test_keyword_invocation_and_nested_calls_keep_operation_scope(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Preserve the stated privacy, lifecycle or outcome contract."""
    inner = _Control(lambda payload: {})

    def callback(payload: Any) -> Any:
        inner.invoke("contract", "profile.read", {})
        with timing.profile_operation_phase("artifact_catalog"):
            pass
        return payload

    _Control(callback).invoke(
        contract_id="contract", operation_id="profile.change.review", payload={}
    )
    messages = _messages(caplog)
    assert any("operation=profile_read" in m for m in messages)
    assert any("operation=profile_review phase=artifact_catalog.finish" in m for m in messages)
