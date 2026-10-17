"""Real pipe framing on the test host; this does not execute Windows/QEMU."""

import json
import os
import threading

import pytest

from tests.test_windows_whpx_support import config
from tobkiri_host.windows_whpx_process import WindowsWHPXProcess
from tobkiri_protocol.canonical import canonical_json
from tobkiri_protocol.packvm_serial import SERIAL_READY_FRAME


class PipeChild:
    def __init__(self):
        read, write = os.pipe()
        self.guest_input = os.fdopen(read, "rb", buffering=0)
        self.stdin = os.fdopen(write, "wb", buffering=0)
        read, write = os.pipe()
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.guest_output = os.fdopen(write, "wb", buffering=0)
        self.running = True
        self.pid = 123

    def poll(self):
        return None if self.running else 0

    def terminate(self):
        if self.running:
            self.running = False
            self.guest_output.close()
            self.guest_input.close()

    def wait(self, timeout):
        assert not self.running
        return 0

    def close(self):
        self.terminate()
        self.stdin.close()
        self.stdout.close()


@pytest.fixture
def pipes(tmp_path, request):
    transport = WindowsWHPXProcess(config(tmp_path))
    child = PipeChild()
    transport._process = child
    transport._started = True
    transport._reader = threading.Thread(target=transport._read_responses, daemon=True)
    transport._reader.start()
    if getattr(request, "param", True):
        child.guest_output.write(SERIAL_READY_FRAME + b"\n")
        transport.wait_for_guest_ready(2)
    yield transport, child
    transport.stop()


def test_canonical_pipe_round_trip_keeps_fresh_challenge(pipes):
    transport, child = pipes

    def guest():
        request = json.loads(child.guest_input.readline())
        child.guest_output.write(
            canonical_json({"guest_challenge": request["guest_challenge"], "test_only": True})
            + b"\n"
        )

    worker = threading.Thread(target=guest)
    worker.start()
    response = transport.exchange({"guest_challenge": "1" * 64, "operation": "invoke"}, timeout=2)
    worker.join(2)
    assert response == {"guest_challenge": "1" * 64, "test_only": True}
    with pytest.raises(Exception, match="replay"):
        transport.exchange({"guest_challenge": "1" * 64, "operation": "invoke"}, timeout=2)


def test_timeout_retires_channel_and_terminates_only_owned_child(pipes):
    transport, child = pipes
    with pytest.raises(TimeoutError):
        transport.exchange({"guest_challenge": "2" * 64, "operation": "invoke"}, timeout=0.05)
    assert child.running is False
    assert transport._failure is not None


def test_noncanonical_guest_response_is_never_promoted(pipes):
    transport, child = pipes

    def guest():
        request = json.loads(child.guest_input.readline())
        child.guest_output.write(
            json.dumps({"guest_challenge": request["guest_challenge"]}).encode() + b"\n"
        )

    worker = threading.Thread(target=guest)
    worker.start()
    with pytest.raises(Exception, match="channel failed"):
        transport.exchange({"guest_challenge": "3" * 64, "operation": "invoke"}, timeout=2)
    worker.join(2)
    assert child.running is False


@pytest.mark.parametrize("pipes", [False], indirect=True)
@pytest.mark.parametrize("split", [1, 2, 30, len(SERIAL_READY_FRAME)])
def test_real_pipe_reader_accepts_fragmented_ready_before_request(pipes, split):
    transport, child = pipes
    prefix_seen = threading.Event()
    original = transport._validate_partial_frame

    def partial(content):
        original(content)
        if bytes(content) == SERIAL_READY_FRAME[:split]:
            prefix_seen.set()

    transport._validate_partial_frame = partial
    child.guest_output.write(SERIAL_READY_FRAME[:split])
    assert prefix_seen.wait(1)
    child.guest_output.write(SERIAL_READY_FRAME[split:] + b"\n")
    transport.wait_for_guest_ready(1)
    assert transport._ready_received
    assert not transport._pending and not transport._used_challenges


@pytest.mark.parametrize("pipes", [False], indirect=True)
@pytest.mark.parametrize("payload", [SERIAL_READY_FRAME + b"\n" + SERIAL_READY_FRAME + b"\n", SERIAL_READY_FRAME + b"\ntrailing", b"{invalid\n"])
def test_real_pipe_reader_retires_duplicate_or_unsolicited_startup(pipes, payload):
    transport, child = pipes
    child.guest_output.write(payload)
    transport._reader.join(1)
    assert not transport._reader.is_alive()
    assert transport._failure is not None and child.running is False
