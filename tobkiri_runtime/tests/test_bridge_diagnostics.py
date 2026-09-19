"""Host bridge diagnostics must not disclose provider-controlled details."""

import logging

import pytest
from tobkiri_host.errors import ProviderExecutionError

from core_runtime.bootstrap.bridge_diagnostics import record_bridge_failure
from core_runtime.global_contract_dispatch import GlobalContractInvocationError


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (RuntimeError("secret prompt"), "internal_error"),
        (TimeoutError("secret URL"), "timeout"),
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
    assert record.getMessage() == f"PackVM capability bridge failed: {reason}"
    assert record.exc_info is None
    assert "secret" not in caplog.text


def test_nested_broker_failure_and_cyclic_cause_are_bounded(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Broker wrapping preserves classification without exposing its cause."""
    wrapper = ProviderExecutionError("secret wrapper")
    wrapper.__cause__ = GlobalContractInvocationError(
        "capability_mismatch", "secret prompt"
    )
    with caplog.at_level(logging.WARNING):
        record_bridge_failure(wrapper)
        wrapper.__cause__ = wrapper
        record_bridge_failure(wrapper)
    assert [record.getMessage() for record in caplog.records] == [
        "PackVM capability bridge failed: capability_mismatch",
        "PackVM capability bridge failed: internal_error",
    ]
    assert "secret" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)
