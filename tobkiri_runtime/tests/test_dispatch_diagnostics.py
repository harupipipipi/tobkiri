"""Nested dispatch diagnostics must not disclose exception contents."""

import logging

import pytest

from core_runtime.dispatch_diagnostics import log_nested_dispatch_failure


def test_nested_failure_records_types_without_messages(caplog):
    inner = TimeoutError("secret-provider-payload")
    outer = RuntimeError("secret-request-body")
    outer.__cause__ = inner
    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure("tobkiri.test.v1", "test.invoke", outer)
    assert "RuntimeError>TimeoutError" in caplog.text
    assert "tobkiri.test.v1" in caplog.text
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_nested_failure_bounds_cycles_and_rejects_log_injection(caplog):
    error = RuntimeError("not logged")
    error.__cause__ = error
    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure("bad\nidentifier", "x" * 500, error)
    assert "contract=unknown operation=unknown error_types=RuntimeError" in caplog.text
    assert "bad" not in caplog.text


def test_nested_failure_respects_suppressed_context(caplog):
    error = ValueError("not logged")
    error.__context__ = TimeoutError("hidden")
    error.__suppress_context__ = True
    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure("test.v1", "test.invoke", error)
    assert "error_types=ValueError" in caplog.text
    assert "TimeoutError" not in caplog.text


def test_broken_logger_does_not_replace_dispatch_exception(monkeypatch):
    from core_runtime import dispatch_diagnostics

    def broken_handler(*args, **kwargs):
        raise ValueError("diagnostic handler failed")

    monkeypatch.setattr(dispatch_diagnostics._LOGGER, "warning", broken_handler)
    original = TimeoutError("private")
    with pytest.raises(TimeoutError) as caught:
        try:
            raise original
        except Exception as error:
            log_nested_dispatch_failure("test.v1", "test.invoke", error)
            raise
    assert caught.value is original


def test_nested_failure_limits_long_chains(caplog):
    error = RuntimeError("private")
    for _ in range(20):
        wrapper = RuntimeError("private")
        wrapper.__cause__ = error
        error = wrapper
    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure("test.v1", "test.invoke", error)
    assert caplog.text.count("RuntimeError") == 4


def test_diagnostic_does_not_format_string_subclasses(caplog):
    class UnsafeString(str):
        def __str__(self):
            return "secret-value"

    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure(UnsafeString("test.v1"), "test.invoke", ValueError())
    assert "contract=unknown" in caplog.text
    assert "secret-value" not in caplog.text


def test_diagnostic_does_not_evaluate_exception_truthiness(caplog):
    class FalseyError(Exception):
        def __bool__(self):
            raise AssertionError("must not evaluate exception truthiness")

    error = RuntimeError()
    error.__cause__ = FalseyError()
    with caplog.at_level(logging.WARNING):
        log_nested_dispatch_failure("test.v1", "test.invoke", error)
    assert "RuntimeError>FalseyError" in caplog.text
