"""Actual TLS buffered reads stop before a deliberately blocked peer releases."""

from __future__ import annotations

from pathlib import Path
import ssl
import threading
import time
import urllib.request

import pytest

from core_runtime.credential_transport import _open_pinned_request
from tests.test_credential_broker_pack import _pin_test_network
from tests.test_mcp_sse_cancellation import _call, _server
from tests.test_mcp_sse_cancellation import tls_server as tls_server


@pytest.mark.parametrize("phase", ["headers", "body"])
@pytest.mark.parametrize("ending", ["cancel", "deadline"])
def test_verified_tls_buffered_io_stops_without_peer_release(
    monkeypatch: pytest.MonkeyPatch, tls_server: tuple[ssl.SSLContext, Path],
    phase: str, ending: str,
) -> None:
    tls, certificate = tls_server
    entered, release, released, cancellation = (threading.Event() for _ in range(4))
    received = []

    def stall(handler):
        received.append(handler.path)
        if phase == "body":
            handler.send_response(200)
            handler.send_header("Content-Length", "100")
            handler.end_headers()
            handler.wfile.write(b"{")
            handler.wfile.flush()
        entered.set()
        release.wait(30)
        released.set()

    context = ssl.create_default_context(cafile=str(certificate))
    monkeypatch.setattr(ssl, "create_default_context", lambda **_kwargs: context)
    _pin_test_network(monkeypatch)
    with _server(stall, tls) as origin:
        deadline = time.monotonic() + (5 if ending == "deadline" else 20)

        def send():
            request = urllib.request.Request(origin + "/invoke", data=b"{}", method="POST")
            with _open_pinned_request(
                request, timeout=20, deadline=deadline, cancellation=cancellation,
            ) as response:
                response.read(1024)

        worker, done, errors = _call(send)
        try:
            assert entered.wait(3)
            if ending == "cancel":
                cancellation.set()
            budget = max(0, deadline - time.monotonic()) + 2 if ending == "deadline" else 2
            assert done.wait(budget)
            assert not released.is_set()
            assert len(errors) == 1 and isinstance(errors[0], (OSError, TimeoutError, InterruptedError))
            assert received == ["/invoke"]
        finally:
            release.set()
            worker.join(3)
        assert not worker.is_alive()
    assert not any(thread.name == "tobkiri-http-deadline" for thread in threading.enumerate())
