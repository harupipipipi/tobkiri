from __future__ import annotations

import sys
from pathlib import Path

import pytest


DEFAULTSPACK_ROOT = (
    Path(__file__).resolve().parents[1] / "ecosystem" / "defaultspack"
)
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from blocks.authority import approval_window  # noqa: E402


class _FakeBrokerClient:
    def __init__(self, result=None, available=True, error=None):
        self._result = result or {"ok": True}
        self._available = available
        self._error = error
        self.calls: list[str] = []

    def available(self) -> bool:
        return self._available

    def open_authority_approval_window(self, request_id: str):
        self.calls.append(request_id)
        if self._error is not None:
            raise self._error
        return self._result


def _patch_client(monkeypatch: pytest.MonkeyPatch, client: _FakeBrokerClient):
    import domain.host_bridge.viewer_broker_client as broker_module

    monkeypatch.setattr(
        broker_module.ViewerBrokerClient,
        "from_environment",
        classmethod(lambda cls: client),
    )


def test_approval_window_rejects_missing_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient()
    _patch_client(monkeypatch, client)

    result = approval_window.run({})

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_REQUEST_ID"
    assert result["_http_status"] == 400
    assert client.calls == []


def test_approval_window_rejects_unsafe_request_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient()
    _patch_client(monkeypatch, client)

    result = approval_window.run({"request_id": "bad id with spaces!"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_REQUEST_ID"
    assert result["_http_status"] == 400
    assert client.calls == []


def test_approval_window_fails_closed_when_broker_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient(available=False)
    _patch_client(monkeypatch, client)

    result = approval_window.run({"request_id": "interactive-effect-abc"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "HOST_BROKER_UNAVAILABLE"
    assert result["_http_status"] == 503
    assert client.calls == []


def test_approval_window_opens_window_through_broker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient(result={"ok": True, "opened": True})
    _patch_client(monkeypatch, client)

    result = approval_window.run({"request_id": "interactive-effect-abc123"})

    assert result["status"] == "ok"
    assert result["data"]["opened"] is True
    assert result["data"]["request_id"] == "interactive-effect-abc123"
    assert client.calls == ["interactive-effect-abc123"]


def test_approval_window_surfaces_broker_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient(
        result={
            "ok": False,
            "error": {
                "code": "AUTHORITY_APPROVAL_REQUEST_INVALID",
                "message": "invalid approval request id",
            },
        }
    )
    _patch_client(monkeypatch, client)

    result = approval_window.run({"request_id": "interactive-effect-abc123"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "AUTHORITY_APPROVAL_REQUEST_INVALID"
    assert result["_http_status"] == 502
    assert client.calls == ["interactive-effect-abc123"]


def test_approval_window_maps_broker_exceptions_to_502(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _FakeBrokerClient(error=ConnectionError("broker unreachable"))
    _patch_client(monkeypatch, client)

    result = approval_window.run({"request_id": "interactive-effect-abc123"})

    assert result["status"] == "error"
    assert result["error"]["code"] == "APPROVAL_WINDOW_FAILED"
    assert result["_http_status"] == 502
