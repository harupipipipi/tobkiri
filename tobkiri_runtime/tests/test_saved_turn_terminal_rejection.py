"""Terminal pre-guest rejections settle failed; unmarked failures still wait.

A Host-side saved preflight denial or deterministic domain-allocation refusal
provably precedes the guest invoke envelope, so no outcome receipt can ever
exist.  The supervisor marks only those definitive rejections with
``saved_terminal_error_code``; every unmarked failure keeps the fail-closed
``waiting``/reconciliation state.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from ecosystem.rumi_turn_runtime_pack.runtime.durable import DurableTurnRuntime
from tests.test_macos_vz_supervisor import _driver, _launch, _request
from tests.test_saved_turn_coordinator import _Session, _run
from tobkiri_host.errors import (
    BackendUnavailableError,
    ProviderExecutionError,
    SavedTurnRejectedError,
)
from tobkiri_protocol.saved_conversation import (
    SAVED_CONVERSATION_CONTRACT,
    SAVED_CONVERSATION_OPERATION,
)


def _fail_dispatch(session: _Session, reject: Any) -> None:
    """Raise the production-shaped exception chain at saved dispatch."""
    original = session.invoke

    def invoke(
        contract_id: str, operation: str, payload: dict, **kwargs: Any
    ) -> dict:
        if (contract_id, operation) == (
            SAVED_CONVERSATION_CONTRACT,
            SAVED_CONVERSATION_OPERATION,
        ):
            reject()
        return original(contract_id, operation, payload, **kwargs)

    session.invoke = invoke  # type: ignore[method-assign]


def test_terminal_preflight_denial_settles_failed_with_diagnostic_code(
    tmp_path: Path,
) -> None:
    """AuthorityDenied in preflight settles failed, never waits for a receipt."""
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)

    def reject() -> None:
        denied = AuthorityDenied(
            "saved bridge AI route is unavailable",
            code="PROVIDER_UNAVAILABLE",
        )
        transport = SavedTurnRejectedError(
            "macOS VZ saved preflight rejected request",
            error_code=denied.code,
        )
        transport.__cause__ = denied
        raise ProviderExecutionError("provider execution failed") from transport

    _fail_dispatch(session, reject)
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    turn = result["turn"]
    assert turn["status"] == "failed"
    event = turn["events"][-1]
    assert event["name"] == "turn.failed"
    assert event["details"] == {
        "phase": "saved_execution_failed",
        "error_code": "PROVIDER_UNAVAILABLE",
        "error": {
            "code": "PROVIDER_UNAVAILABLE",
            "message": "Saved conversation did not complete.",
        },
        "user_persistence": "not_written",
        "assistant_persistence": "not_written",
    }
    assert turn["error"] == {
        "code": "PROVIDER_UNAVAILABLE",
        "message": "Saved conversation did not complete.",
    }
    # The denial provably precedes the invoke envelope: no dispatch ran and
    # the conversation gained no messages.
    assert session.events == ["begin_saved", "claim_saved"]
    assert session.calls == session.ai_calls == 0
    assert session.conversations.get("conversation-1")["messages"] == []
    # A finite failed turn is terminal; it never retries or reconciles.
    assert _run(store, session) == {"status": "existing", "turn": turn}


def test_terminal_allocation_refusal_settles_failed(tmp_path: Path) -> None:
    """A deterministic VZ provisioning refusal is terminal for the turn."""
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)

    def reject() -> None:
        provisioning = ValueError(
            "PackVM VZ provisioning requires at least 3.50 GiB free"
        )
        transport = SavedTurnRejectedError(
            "macOS VZ domain allocation failed",
            error_code="BACKEND_UNAVAILABLE",
        )
        transport.__cause__ = provisioning
        raise ProviderExecutionError("provider execution failed") from transport

    _fail_dispatch(session, reject)
    result = _run(store, session)
    turn = result["turn"]
    assert turn["status"] == "failed"
    assert turn["events"][-1]["details"]["error_code"] == "BACKEND_UNAVAILABLE"
    assert session.conversations.get("conversation-1")["messages"] == []


def test_unmarked_preflight_failure_still_waits_for_reconciliation(
    tmp_path: Path,
) -> None:
    """An unmarked/unknown dispatch failure keeps the fail-closed wait."""
    session = _Session(tmp_path)
    store = DurableTurnRuntime("defaults", user_data_root=tmp_path)

    def reject() -> None:
        transport = BackendUnavailableError(
            "macOS VZ saved preflight rejected request"
        )
        transport.__cause__ = RuntimeError("unexpected host state")
        raise ProviderExecutionError("provider execution failed") from transport

    _fail_dispatch(session, reject)
    result = _run(store, session)
    assert result["status"] == "reconciliation_required"
    turn = result["turn"]
    assert turn["status"] == "waiting"
    assert turn["events"][-1]["name"] == "turn.waiting"
    assert turn["events"][-1]["details"] == {
        "phase": "reconciliation_required",
        "reason": "saved_execution_outcome_unconfirmed",
    }
    # No receipt exists, so reconciliation keeps waiting rather than
    # fabricating a completion or a terminal failure.
    repeated = _run(store, session)
    assert repeated["status"] == "existing"
    assert repeated["turn"]["status"] == "waiting"


def _saved_request() -> Any:
    request = _request("domain.provider.conversation")
    request.contract_id = "conversation.saved-turn.v1"
    request.operation_id = "saved_complete"
    request.deadline_monotonic = time.monotonic() + 50
    request.payload = {
        "request": {
            "turn_id": "turn",
            "conversation_id": "conversation",
            "conversation_revision": 1,
            "content": "Hello",
        }
    }
    return request


def test_supervisor_marks_terminal_preflight_denial(tmp_path: Path) -> None:
    """The supervisor tags a definitive preflight denial for the coordinator."""
    driver, allocator = _driver(tmp_path)

    def preflight(outer: object) -> None:
        raise AuthorityDenied(
            "saved bridge AI route is unavailable",
            code="PROVIDER_UNAVAILABLE",
        )

    driver.bind_saved_capability_bridge(lambda _outer, _frame: {}, preflight)
    _launch(driver)
    transport = allocator.transports["domain.provider.conversation"]
    with pytest.raises(SavedTurnRejectedError) as raised:
        driver.invoke(_saved_request())
    assert raised.value.saved_terminal_error_code == "PROVIDER_UNAVAILABLE"
    assert isinstance(raised.value, BackendUnavailableError)
    assert isinstance(raised.value.__cause__, AuthorityDenied)
    # The invoke envelope never reached the guest.
    assert [item["operation"] for item in transport.requests] == ["launch"]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("unexpected host state"),
        BackendUnavailableError("macOS VZ saved preflight acknowledgement is invalid"),
    ],
)
def test_supervisor_keeps_opaque_preflight_failure_unmarked(
    tmp_path: Path, failure: BaseException
) -> None:
    """An unclassified preflight failure stays ambiguous, never terminal."""
    driver, allocator = _driver(tmp_path)

    def preflight(_outer: object) -> None:
        raise failure

    driver.bind_saved_capability_bridge(
        lambda _outer, _frame: {}, preflight
    )
    _launch(driver)
    with pytest.raises(
        BackendUnavailableError, match="saved preflight rejected request"
    ) as raised:
        driver.invoke(_saved_request())
    assert not isinstance(raised.value, SavedTurnRejectedError)
    assert getattr(raised.value, "saved_terminal_error_code", None) is None


def test_supervisor_marks_deterministic_allocation_refusal(
    tmp_path: Path,
) -> None:
    """A provisioning ValueError is terminal; the guest never ran."""
    driver, allocator = _driver(tmp_path)

    def refuse(**kwargs: Any) -> Any:
        raise ValueError("PackVM VZ provisioning requires at least 3.50 GiB free")

    allocator.allocate = refuse  # type: ignore[method-assign]
    with pytest.raises(
        SavedTurnRejectedError, match="domain allocation failed"
    ) as raised:
        _launch(driver)
    assert raised.value.saved_terminal_error_code == "BACKEND_UNAVAILABLE"


def test_supervisor_keeps_gate_contention_retryable(tmp_path: Path) -> None:
    """Transient lifecycle-gate contention is never marked terminal."""

    class PackVMGateBusyError(ValueError):
        """Provisioner-side name match; the supervisor detects it by MRO."""

    driver, allocator = _driver(tmp_path)

    def refuse(**kwargs: Any) -> Any:
        raise ValueError("allocation failed") from PackVMGateBusyError("busy")

    allocator.allocate = refuse  # type: ignore[method-assign]
    with pytest.raises(
        BackendUnavailableError, match="domain allocation failed"
    ) as raised:
        _launch(driver)
    assert not isinstance(raised.value, SavedTurnRejectedError)
    assert getattr(raised.value, "saved_terminal_error_code", None) is None
