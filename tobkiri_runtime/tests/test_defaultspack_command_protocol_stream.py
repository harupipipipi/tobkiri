from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

from domain.frontend.invocation_events import InvocationEventStore  # noqa: E402


class _Registry:
    events: InvocationEventStore

    def __init__(self) -> None:
        self.events = type(self).events

    @staticmethod
    def owner_key(payload, context) -> str:
        return "local:default"

    @staticmethod
    def reconcile_approval(payload, context) -> None:
        return None


def test_sse_replays_after_last_event_id_and_emits_terminal_event(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = InvocationEventStore(tmp_path / "events.sqlite3")
    store.claim(
        "inv-stream",
        {"step": 1},
        owner_key="local:default",
        request_fingerprint="stream",
    )
    store.append("inv-stream", "completed", {"step": 2})
    _Registry.events = store
    module = importlib.import_module("blocks.ui.command_protocol_stream")
    monkeypatch.setattr(module, "CommandProtocolRegistry", _Registry)

    result = module.run(
        {
            "invocation_id": "inv-stream",
            "_headers": {"Last-Event-ID": "1"},
            "wait_seconds": 0,
        },
        {},
    )
    chunks = list(result["events"])

    assert result["_sse"] is True
    assert len(chunks) == 1
    frame = chunks[0].decode("utf-8")
    assert frame.startswith("id: 2\nevent: completed\ndata: ")
    data = json.loads(frame.split("data: ", 1)[1])
    assert data["invocation_id"] == "inv-stream"
    assert data["sequence"] == 2


def test_sse_rejects_invalid_cursor(monkeypatch) -> None:
    module = importlib.import_module("blocks.ui.command_protocol_stream")
    monkeypatch.setattr(module, "CommandProtocolRegistry", _Registry)

    result = module.run(
        {
            "invocation_id": "inv-stream",
            "after_sequence": -1,
        },
        {},
    )

    assert result["status"] == "error"
    assert result["error"]["code"] == "INVALID_INPUT"
