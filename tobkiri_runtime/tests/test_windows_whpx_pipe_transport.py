"""Real pipe framing on the test host; this does not execute Windows/QEMU."""

import json
import os
import threading

import pytest

from tests.test_windows_whpx_support import config
from tobkiri_host.windows_whpx_process import WindowsWHPXProcess
from tobkiri_protocol.canonical import canonical_json


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
def pipes(tmp_path):
    transport = WindowsWHPXProcess(config(tmp_path))
    child = PipeChild()
    transport._process = child
    transport._started = True
    transport._reader = threading.Thread(target=transport._read_responses, daemon=True)
    transport._reader.start()
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
