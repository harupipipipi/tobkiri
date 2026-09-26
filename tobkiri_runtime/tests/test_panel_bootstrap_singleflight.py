"""Panel recovery must not launch concurrent full runtime captures."""

from __future__ import annotations

import threading
from types import SimpleNamespace

from core_runtime.pack_api_server import PackAPIHandler


def test_panel_bootstrap_recovery_is_single_flight() -> None:
    entered = threading.Event()
    release = threading.Event()
    refreshes: list[object] = []

    def refresh(session: object) -> None:
        refreshes.append(session)
        entered.set()
        assert release.wait(5)

    class Handler(PackAPIHandler):
        _panel_auth_manager = SimpleNamespace(validate_bootstrap_secret=lambda _secret: True)
        _runtime_refresh = staticmethod(refresh)
        _panel_refresh_lock = threading.Lock()

        def _is_loopback_client(self, _address: object) -> bool:
            return True

        def _parse_object_body(self) -> dict[str, object]:
            return {}

        @classmethod
        def _current_panel_auth_binding(cls) -> None:
            return None

        def _send_response(self, response: object, status: int = 200) -> None:
            self.response_status = status

    first = object.__new__(Handler)
    first.headers = {"X-Rumi-Desktop-Bootstrap": "test-secret"}
    first.client_address = ("127.0.0.1", 1)
    second = object.__new__(Handler)
    second.headers = first.headers
    second.client_address = first.client_address

    worker = threading.Thread(target=first._handle_panel_bootstrap)
    worker.start()
    try:
        assert entered.wait(5)
        second._handle_panel_bootstrap()
        assert second.response_status == 503
        assert refreshes == [None]
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    assert first.response_status == 401
