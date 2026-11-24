import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(DEFAULTSPACK_ROOT))


def test_json_export_returns_self_describing_api_payload(
    tmp_path,
    monkeypatch,
    defaultspack_conversation_owner,
):
    from blocks.chat.export_conversation import run as export_conversation
    from domain.chat.store import ChatStore

    monkeypatch.setenv(
        "RUMI_DEFAULTSPACK_CHAT_STORE_PATH",
        str(tmp_path / "chat" / "conversations.json"),
    )
    ChatStore._instance = None
    try:
        conversation = ChatStore().create_conversation(model="stub/default")

        result = export_conversation(
            {"conversation_id": conversation["id"], "format": "JSON"},
            {},
        )

        assert result["status"] == "ok"
        assert result["data"]["conversation_id"] == conversation["id"]
        assert result["data"]["format"] == "json"
        assert json.loads(result["data"]["content"])["id"] == conversation["id"]
    finally:
        ChatStore._instance = None
