"""A consumed Host result cannot fork, retry or renew its root continuation."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json

import pytest

from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_session import ContinuationSession
from tobkiri_protocol.canonical import canonical_digest, canonical_json

IDENTITY = ChainIdentity("domain", "request", "sha256:" + "a" * 64, 60.0)
TARGET = ("owned.contract.v1", "owned.operation")


def _intent(hop: int = 0) -> bytes:
    return canonical_json({
        "kind": "tobkiri.packvm.continuation.intent.v2", "hop": hop,
        "target": dict(zip(("contract_id", "operation_id"), TARGET)),
        "payload": {}, "state": {"phase": hop},
    })


def _result(frame: bytes) -> bytes:
    return canonical_json({
        "kind": "tobkiri.packvm.continuation.result.v2", "version": 2,
        "request_digest": canonical_digest(json.loads(frame)),
        "outcome": {"status": "ok", "value": {"answer": "owned"}},
    })


def _session(*, clock=lambda: 1.0, max_bytes: int = 1024 * 1024):
    chains = ContinuationChains(clock=clock, max_bytes=max_bytes)
    session = ContinuationSession(IDENTITY, (TARGET, TARGET), chains=chains)
    frame = session.start(_intent(), nonce="b" * 48)
    return chains, session, frame


def test_one_result_and_one_resume_input_win_under_concurrency() -> None:
    _, session, frame = _session()

    def receive(_):
        try:
            return session.receive(_result(frame))
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        permits = [value for value in pool.map(receive, range(4)) if value is not None]
    assert len(permits) == 1
    permit = permits[0]
    with pytest.raises(ValueError):
        session.resume_arguments(replace(permit))
    arguments = session.resume_arguments(permit)
    assert arguments == {"state": {"phase": 0}, "outcome": {"status": "ok", "value": {"answer": "owned"}}}
    arguments["state"]["phase"] = 99
    assert json.loads(permit.frame)["state"] == {"phase": 0}
    with pytest.raises(ValueError):
        session.resume_arguments(permit)
    next_frame = session.advance(permit, _intent(1), nonce="c" * 48)
    assert json.loads(next_frame)["previous_digest"] == canonical_digest(json.loads(_result(frame)))
    with pytest.raises(ValueError):
        session.advance(permit, _intent(1), nonce="d" * 48)


@pytest.mark.parametrize("bad", [b"{}", b"null", b'{"kind":"tobkiri.packvm.bridge.result.v1"}'])
def test_malformed_result_is_terminal_not_a_retry_opportunity(bad: bytes) -> None:
    chains, session, frame = _session()
    with pytest.raises(ValueError):
        session.receive(bad)
    with pytest.raises(ValueError):
        session.receive(_result(frame))
    with pytest.raises(ValueError):
        ContinuationSession(IDENTITY, (TARGET,), chains=chains).start(_intent(), nonce="c" * 48)


@pytest.mark.parametrize("stop", ["session", "ledger", "deadline"])
def test_cancellation_or_expiry_blocks_child_input_after_result_consumption(stop: str) -> None:
    now = [1.0]
    chains, session, frame = _session(clock=lambda: now[0])
    permit = session.receive(_result(frame))
    if stop == "session":
        session.cancel()
    elif stop == "ledger":
        chains.cancel(IDENTITY)
    else:
        now[0] = 60.0
    with pytest.raises(ValueError):
        session.resume_arguments(permit)
    with pytest.raises(ValueError):
        session.advance(permit, _intent(1), nonce="c" * 48)


def test_bad_next_step_fences_consumed_result_and_plan_cannot_be_extended() -> None:
    _, session, frame = _session()
    permit = session.receive(_result(frame))
    session.resume_arguments(permit)
    with pytest.raises(ValueError):
        session.advance(permit, _intent(0), nonce="c" * 48)
    with pytest.raises(ValueError):
        session.advance(permit, _intent(1), nonce="c" * 48)

    _, session, frame = _session()
    for hop in range(2):
        permit = session.receive(_result(frame))
        session.resume_arguments(permit)
        if hop == 0:
            frame = session.advance(permit, _intent(1), nonce="c" * 48)
    with pytest.raises(ValueError, match="exhausted"):
        session.advance(permit, _intent(2), nonce="d" * 48)


def test_final_bytes_share_cumulative_budget_and_failure_retains_tombstone() -> None:
    chains, session, frame = _session(max_bytes=1200)
    permit = session.receive(_result(frame))
    session.resume_arguments(permit)
    with pytest.raises(ValueError, match="byte budget"):
        session.finish(permit, canonical_json({"output": "x" * 800}))
    with pytest.raises(ValueError):
        session.finish(permit, b"{}")
    with pytest.raises(ValueError):
        chains.start(IDENTITY, frame=frame, nonce="b" * 48)


def test_terminal_result_is_required_and_cannot_be_an_unhandled_control_frame() -> None:
    _, session, frame = _session()
    permit = session.receive(_result(frame))
    with pytest.raises(ValueError, match="resume step"):
        session.finish(permit, b"{}")
    session.resume_arguments(permit)
    with pytest.raises(ValueError, match="not a final outcome"):
        session.finish(permit, _intent(1))
