"""Captured resource cleanup must remain retryable after partial failure."""

from unittest.mock import Mock

import pytest

from tobkiri_host.runtime import V4DispatchSession


def _session(*callbacks, stop_callbacks=()):
    return V4DispatchSession(
        broker=Mock(), context_for=Mock(), effect_scope_for=Mock(), providers={},
        profile_id="defaults", plan_digest="plan", profile_revision="revision",
        activation_id="activation", owned_authority_store=Mock(),
        close_callbacks=callbacks, stop_callbacks=stop_callbacks,
    )


def test_failed_provider_does_not_skip_other_cleanup_or_close_authority() -> None:
    failed, healthy = Mock(), Mock()
    failed.side_effect = OSError("owned child remains alive")
    session = _session(failed, healthy)
    with pytest.raises(RuntimeError, match="cleanup is incomplete"):
        session.close()
    healthy.assert_called_once_with()
    session.owned_authority_store.close.assert_not_called()
    session.broker.close.assert_called_once_with()
    failed.side_effect = None
    session.close()
    session.close()
    assert failed.call_count == 2
    healthy.assert_called_once_with()
    session.owned_authority_store.close.assert_called_once_with()


def test_authority_close_failure_retries_without_repeating_provider_cleanup() -> None:
    provider = Mock()
    session = _session(provider)
    session.owned_authority_store.close.side_effect = OSError("database cleanup failed")
    with pytest.raises(OSError, match="database cleanup failed"):
        session.close()
    session.owned_authority_store.close.side_effect = None
    session.close()
    provider.assert_called_once_with()
    assert session.owned_authority_store.close.call_count == 2


def test_server_stop_cancels_broker_requests_before_read_owners() -> None:
    events = []
    session = _session(
        stop_callbacks=(lambda: events.append("read_owner_cancelled"),)
    )
    session.broker.cancel_pending_requests.side_effect = lambda: events.append(
        "broker_requests_cancelled"
    )

    session.cancel_pending_reads()

    assert events == ["broker_requests_cancelled", "read_owner_cancelled"]
