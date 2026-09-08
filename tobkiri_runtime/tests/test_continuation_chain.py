"""Bounded continuation state, distinct from production dispatch acceptance."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains


def _identity() -> ChainIdentity:
    return ChainIdentity("domain", "request", "sha256:" + "a" * 64, 160.0)


def _nonce(index: int) -> str:
    return f"{index:048x}"


def test_four_hops_are_once_only_and_preserve_original_binding() -> None:
    chains = ContinuationChains(clock=lambda: 100.0)
    identity = _identity()
    chains.start(identity, frame=b"request", nonce=_nonce(0))
    for hop in range(4):
        permit = chains.take(identity, nonce=_nonce(hop), result=b"response")
        assert permit.hop == hop
        assert permit.identity is identity
        with pytest.raises(ValueError):
            chains.take(identity, nonce=_nonce(hop), result=b"response")
        with pytest.raises(ValueError):
            chains.finish(replace(permit))
        if hop < 3:
            chains.advance(permit, frame=b"next", nonce=_nonce(hop + 1))
        else:
            chains.finish(permit)
    with pytest.raises(ValueError):
        chains.start(identity, frame=b"retry", nonce=_nonce(9))


@pytest.mark.parametrize("phase", ["pending", "inflight"])
def test_cancel_blocks_late_results_and_advance(phase: str) -> None:
    chains = ContinuationChains(clock=lambda: 100.0)
    identity = _identity()
    chains.start(identity, frame=b"request", nonce=_nonce(0))
    permit = (
        chains.take(identity, nonce=_nonce(0), result=b"result") if phase == "inflight" else None
    )
    chains.cancel(identity)
    with pytest.raises(ValueError):
        chains.take(identity, nonce=_nonce(0), result=b"late")
    if permit is not None:
        with pytest.raises(ValueError):
            chains.advance(permit, frame=b"late", nonce=_nonce(1))
    with pytest.raises(ValueError):
        chains.start(identity, frame=b"retry", nonce=_nonce(2))


def test_limits_expiry_and_terminal_capacity_are_not_refreshed() -> None:
    now = [100.0]
    chains = ContinuationChains(clock=lambda: now[0], max_entries=1, max_hops=1)
    identity = _identity()
    chains.start(identity, frame=b"first", nonce=_nonce(0))
    permit = chains.take(identity, nonce=_nonce(0), result=b"result")
    with pytest.raises(ValueError):
        chains.advance(permit, frame=b"second", nonce=_nonce(1))
    with pytest.raises(ValueError):
        chains.start(replace(identity, request_id="other"), frame=b"other", nonce=_nonce(2))
    now[0] = 160.0
    with pytest.raises(ValueError):
        chains.finish(permit)
    chains.start(
        replace(identity, request_id="fresh", deadline=200.0), frame=b"fresh", nonce=_nonce(3)
    )


def test_cumulative_requests_and_results_share_the_byte_limit() -> None:
    chains = ContinuationChains(clock=lambda: 100.0, max_bytes=5)
    identity = _identity()
    chains.start(identity, frame=b"abc", nonce=_nonce(0))
    permit = chains.take(identity, nonce=_nonce(0), result=b"de")
    with pytest.raises(ValueError):
        chains.advance(permit, frame=b"f", nonce=_nonce(1))
    with pytest.raises(ValueError):
        chains.finish(permit)


def test_wrong_binding_nonce_and_concurrent_consumption() -> None:
    chains = ContinuationChains(clock=lambda: 100.0)
    identity = _identity()
    chains.start(identity, frame=b"request", nonce=_nonce(0))
    for altered in (
        replace(identity, domain_id="other"),
        replace(identity, binding_digest="sha256:" + "b" * 64),
        replace(identity, deadline=159.0),
    ):
        with pytest.raises(ValueError):
            chains.take(altered, nonce=_nonce(0), result=b"result")
    with pytest.raises(ValueError):
        chains.take(identity, nonce=_nonce(1), result=b"result")

    def consume() -> bool:
        try:
            chains.take(identity, nonce=_nonce(0), result=b"result")
            return True
        except ValueError:
            return False

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(lambda _: consume(), range(2))) == [False, True]


def test_advance_cannot_extend_deadline_or_reuse_nonce() -> None:
    now = [100.0]
    chains = ContinuationChains(clock=lambda: now[0])
    identity = _identity()
    chains.start(identity, frame=b"first", nonce=_nonce(0))
    permit = chains.take(identity, nonce=_nonce(0), result=b"result")
    now[0] = 159.0
    chains.advance(permit, frame=b"second", nonce=_nonce(1))
    now[0] = 160.0
    with pytest.raises(ValueError):
        chains.take(identity, nonce=_nonce(1), result=b"late")
    now[0] = 100.0
    chains = ContinuationChains(clock=lambda: now[0])
    chains.start(identity, frame=b"first", nonce=_nonce(0))
    permit = chains.take(identity, nonce=_nonce(0), result=b"result")
    with pytest.raises(ValueError, match="nonce"):
        chains.advance(permit, frame=b"second", nonce=_nonce(0))
    with pytest.raises(ValueError):
        chains.advance(permit, frame=b"retry", nonce=_nonce(1))


def test_oversize_result_fences_chain_without_retry() -> None:
    chains = ContinuationChains(clock=lambda: 100.0, max_bytes=5)
    identity = _identity()
    chains.start(identity, frame=b"abc", nonce=_nonce(0))
    with pytest.raises(ValueError, match="byte budget"):
        chains.take(identity, nonce=_nonce(0), result=b"def")
    with pytest.raises(ValueError):
        chains.take(identity, nonce=_nonce(0), result=b"x")
