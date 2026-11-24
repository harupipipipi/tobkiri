"""Ordering-only guest readiness must never substitute for signed attestation."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from tobkiri_host.errors import BackendUnavailableError
from tobkiri_host.qemu_process_core import QemuProcessCore
from tobkiri_protocol.canonical import canonical_json, strict_loads
from tobkiri_protocol.packvm_serial import SERIAL_READY_FRAME


class Transport(QemuProcessCore):
    def __init__(self):
        super().__init__(SimpleNamespace(run_root=Path('/test'), max_request_bytes=4096, max_response_bytes=4096))
        self._process = SimpleNamespace(pid=42, poll=lambda: None)
        self._started = True
        self.connected = threading.Event()
        self.sent = []

    def wait_for_serial(self, timeout=60):
        assert timeout > 0
        self.connected.set()

    def _send_frame(self, encoded, deadline):
        assert self._ready_received, "Host request bytes preceded real guest readiness"
        value = strict_loads(encoded.rstrip(b'\n'))
        self.sent.append(value)
        self._deliver_frame(canonical_json({'guest_challenge': value['guest_challenge']}))

    def _fail_channel(self, error):
        with self._lock:
            self._failure = self._failure or error
            self._ready_event.set()
            for pending in self._pending.values():
                pending.error = error
                pending.event.set()


def test_connected_transport_cannot_send_before_guest_ready():
    transport = Transport()
    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(transport.exchange, {'guest_challenge': 'a' * 64}, 2)
        assert transport.connected.wait(1)
        assert not transport.sent
        transport._deliver_frame(SERIAL_READY_FRAME)
        assert result.result(2) == {'guest_challenge': 'a' * 64}
    assert len(transport.sent) == 1


@pytest.mark.parametrize('split', range(1, len(SERIAL_READY_FRAME)))
def test_ready_prefix_is_tightly_bounded_at_every_fragment(split):
    transport = Transport()
    transport._validate_partial_frame(SERIAL_READY_FRAME[:split])
    transport._deliver_frame(SERIAL_READY_FRAME)
    transport.wait_for_guest_ready(1)  # early ready is retained
    assert transport._pending == {} and transport._used_challenges == set()
    with pytest.raises(BackendUnavailableError, match='unsolicited'):
        transport._validate_partial_frame(b'{')
    with pytest.raises(BackendUnavailableError, match='repeated'):
        transport._deliver_frame(SERIAL_READY_FRAME)


@pytest.mark.parametrize('bad', [b'{}', b'[]', b' ready', SERIAL_READY_FRAME + b' ', b'{"kind":"wrong"}'])
def test_unknown_noncanonical_or_oversized_startup_is_rejected(bad):
    transport = Transport()
    with pytest.raises(BackendUnavailableError):
        transport._deliver_frame(bad)
    assert not transport._ready_received


def test_ready_timeout_retires_channel_without_registering_request():
    transport = Transport()
    with pytest.raises(TimeoutError, match='readiness'):
        transport.exchange({'guest_challenge': 'a' * 64}, .02)
    assert transport._failure is not None
    assert not transport.sent and not transport._pending and not transport._used_challenges
    with pytest.raises(BackendUnavailableError, match='retired'):
        transport._deliver_frame(SERIAL_READY_FRAME)


def test_transport_failure_wakes_all_readiness_waiters():
    transport = Transport()
    with ThreadPoolExecutor(max_workers=2) as pool:
        waiters = [pool.submit(transport.wait_for_guest_ready, 2) for _ in range(2)]
        assert transport.connected.wait(1)
        transport._fail_channel(OSError('closed'))
        for waiter in waiters:
            with pytest.raises(BackendUnavailableError, match='retired'):
                waiter.result(1)
    assert not transport._ready_received


def test_two_startup_callers_share_one_barrier_then_keep_distinct_challenges():
    transport = Transport()
    arrived = threading.Barrier(3)
    original = transport.wait_for_serial

    def connected(timeout=60):
        original(timeout)
        arrived.wait(1)

    transport.wait_for_serial = connected
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(transport.exchange, {"guest_challenge": letter * 64}, 2) for letter in ("a", "b")]
        arrived.wait(1)
        assert not transport.sent
        transport._deliver_frame(SERIAL_READY_FRAME)
        assert [result.result(2) for result in results] == [{"guest_challenge": letter * 64} for letter in ("a", "b")]
    assert len(transport.sent) == 2
