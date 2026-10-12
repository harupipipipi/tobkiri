"""Host bridge diagnostics must not disclose provider-controlled details."""

import logging

import pytest
from tobkiri_host.errors import ProviderExecutionError, ResolutionError

from core_runtime.bootstrap.bridge_diagnostics import record_bridge_failure
from core_runtime.global_contract_dispatch import GlobalContractInvocationError


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("secret prompt"), "internal_error"),
        (TimeoutError("secret URL"), "timeout"),
        *[
            (GlobalContractInvocationError(code, "secret payload"), code)
            for code in (
                "denied",
                "incompatible",
                "invalid_request",
                "not_configured",
                "route_binding_invalid",
                "route_binding_stale",
                "route_binding_conflict",
            )
        ],
        (
            GlobalContractInvocationError("missing_provider", "secret token"),
            "missing_provider",
        ),
        (
            GlobalContractInvocationError("secret-code\nforged", "secret token"),
            "provider_error",
        ),
    ],
)
def test_bridge_failure_logs_only_fixed_reason(
    error: Exception, reason: str, caplog: pytest.LogCaptureFixture
) -> None:
    """Neither exception text, arbitrary codes, nor tracebacks are logged."""
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(error)
    assert len(caplog.records) == 1
    record = caplog.records[0]
    assert record.getMessage().startswith(f"PackVM capability bridge failed: {reason} ")
    assert "request=unknown phase=unknown contract=unknown operation=unknown" in (
        record.getMessage()
    )
    assert record.exc_info is None
    assert "secret" not in caplog.text


def test_nested_broker_failure_and_cyclic_cause_are_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Broker wrapping preserves classification without exposing its cause."""
    wrapper = ProviderExecutionError("secret wrapper")
    wrapper.__cause__ = GlobalContractInvocationError("capability_mismatch", "secret prompt")
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(wrapper)
        wrapper.__cause__ = wrapper
        record_bridge_failure(wrapper)
    assert "capability_mismatch" in caplog.records[0].getMessage()
    assert "error_types=ProviderExecutionError>GlobalContractInvocationError" in (
        caplog.records[0].getMessage()
    )
    assert "internal_error" in caplog.records[1].getMessage()
    assert "error_types=ProviderExecutionError" in caplog.records[1].getMessage()
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_bridge_failure_correlates_only_host_identifiers(caplog) -> None:
    """A Host request can be found without dumping exception or request text."""
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(
            PermissionError("secret-path-and-prompt"),
            request_id="request." + "a" * 32,
            phase="provider_dispatch",
            contract_id="tobkiri.service.ai.generate.v1",
            operation_id="rumi_ai_gateway_pack.ai-gateway.generate",
        )
        record_bridge_failure(
            TypeError("secret-response"),
            request_id="secret\nrequest",
            phase="secret-phase",
            contract_id="secret\ncontract",
            operation_id="x" * 500,
        )
    assert "request=request." + "a" * 32 in caplog.records[0].getMessage()
    assert "phase=provider_dispatch" in caplog.text
    assert "error_types=PermissionError" in caplog.text
    assert "request=unknown phase=unknown contract=unknown operation=unknown" in (
        caplog.records[1].getMessage()
    )
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_bridge_failure_types_are_fixed_bounded_and_respect_suppression(caplog) -> None:
    """Custom class names and suppressed contexts never enter diagnostics."""
    unsafe_type = type("secret-token", (Exception,), {})
    inner = unsafe_type("secret")
    inner.__context__ = PermissionError("secret")
    inner.__suppress_context__ = True
    error = RuntimeError("secret")
    error.__cause__ = inner
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(error)
        for _ in range(20):
            wrapper = ValueError("secret")
            wrapper.__cause__ = error
            error = wrapper
        record_bridge_failure(error)
    assert "error_types=RuntimeError>OtherError" in caplog.records[0].getMessage()
    assert "error_types=ValueError>ValueError>ValueError>ValueError" in (
        caplog.records[1].getMessage()
    )
    assert "secret" not in caplog.text


def test_bridge_failure_preserves_known_host_cause_type(caplog) -> None:
    """Broker wrapping does not hide a concrete Host resolution failure."""
    error = ProviderExecutionError("secret")
    error.__cause__ = ResolutionError("secret binding")
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(error)
    assert "error_types=ProviderExecutionError>ResolutionError" in caplog.text
    assert "secret" not in caplog.text


def test_bridge_failure_does_not_format_untrusted_subclasses(caplog) -> None:
    """Diagnostics never invoke arbitrary string or exception formatters."""

    class UnsafeString(str):
        def __str__(self):
            raise AssertionError("must not format")

    class UnsafeError(Exception):
        def __str__(self):
            raise AssertionError("must not format")

        def __bool__(self):
            raise AssertionError("must not evaluate truthiness")

    with caplog.at_level(logging.WARNING):
        record_bridge_failure(
            UnsafeError(),
            request_id=UnsafeString("request." + "a" * 32),
            phase=UnsafeString("provider_dispatch"),
            contract_id=UnsafeString("contract"),
            operation_id=UnsafeString("operation"),
        )
    assert "request=unknown phase=unknown contract=unknown operation=unknown" in (caplog.text)
    assert "error_types=OtherError" in caplog.text


def test_broken_bridge_logger_does_not_escape(monkeypatch) -> None:
    """Log handler failures must not replace the sanitized bridge outcome."""
    from core_runtime.bootstrap import bridge_diagnostics

    def broken_handler(*args, **kwargs):
        raise RuntimeError("private logger failure")

    monkeypatch.setattr(bridge_diagnostics._LOGGER, "warning", broken_handler)
    record_bridge_failure(PermissionError("private bridge failure"))
