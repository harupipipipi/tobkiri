from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_chat_facade_does_not_bypass_missing_owner_with_legacy_storage(
    monkeypatch, tmp_path
):
    """A stale JSON path cannot satisfy a request without a selected owner."""
    from domain.chat import store as facade
    from domain.chat.store import ChatStore

    legacy_path = tmp_path / "legacy" / "conversations.json"
    legacy_path.parent.mkdir()
    legacy_path.write_text(
        json.dumps({"conversations": {"legacy": {"id": "legacy"}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(legacy_path))
    monkeypatch.setattr(
        facade,
        "get_container",
        lambda: SimpleNamespace(get_or_none=lambda _name: None),
    )

    with pytest.raises(RuntimeError, match="global conversation owner is unavailable"):
        ChatStore().list_conversations()
