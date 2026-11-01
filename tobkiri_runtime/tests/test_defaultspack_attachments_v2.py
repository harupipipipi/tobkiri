from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("defaultspack_conversation_owner")


def test_attachment_record_created_for_text_file_and_legacy_refs(tmp_path, monkeypatch):
    from domain.chat.attachments.store import manifest_path
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    refs = store.persist_attachments(conv["id"], [{"id": "a1", "name": "note.txt", "type": "text/plain", "content": "hello"}])
    manifest = json.loads(manifest_path(store.conversation_workspace_dir(conv["id"])).read_text(encoding="utf-8"))

    assert refs[0]["workspace_path"].endswith("attachments/note.txt")
    assert manifest["attachments"][0]["representations"]["text"]["text"] == "hello"
    ChatStore._instance = None


def test_attachment_metadata_does_not_store_raw_data_url_unnecessarily(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:image/png;base64," + base64.b64encode(b"abc").decode()
    prepared = prepare_chat_run(
        {"conversation_id": conv["id"], "message": {"content": "see", "attachments": [{"id": "img", "name": "img.png", "type": "image/png", "size": 3, "dataUrl": data_url}]}},
        {},
    )

    assert "dataUrl" not in prepared.metadata["attachments"][0]
    assert prepared.metadata["workspace_attachments"][0]["workspace_path"].endswith("attachments/img.png")
    assert any(block.get("type") == "image_url" for block in prepared.content)
    ChatStore._instance = None


def test_ephemeral_audio_attachment_is_model_only_and_not_persisted(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:audio/webm;base64," + base64.b64encode(b"voice").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "",
                "attachments": [
                    {
                        "id": "ambient-audio",
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 5,
                        "dataUrl": data_url,
                        "ephemeral": True,
                        "do_not_persist": True,
                    }
                ],
            },
        },
        {},
    )

    assert "workspace_attachments" not in prepared.metadata
    assert "dataUrl" not in prepared.metadata["attachments"][0]
    assert any(block.get("type") == "text" and "音声入力" in block.get("text", "") for block in prepared.content)
    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert any(
        isinstance(block, dict)
        and block.get("type") == "input_audio"
        and block.get("input_audio", {}).get("data") == base64.b64encode(b"voice").decode()
        for block in user_messages[-1]["content"]
    )
    assert not (store.conversation_workspace_dir(conv["id"]) / "attachments" / "pinch.webm").exists()
    ChatStore._instance = None


def test_audio_attachment_over_size_limit_is_not_sent_as_input_audio(tmp_path, monkeypatch):
    from domain.chat import run_request
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    monkeypatch.setattr(run_request, "MAX_ATTACHMENT_AUDIO_BYTES", 4)
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:audio/webm;base64," + base64.b64encode(b"voice").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "",
                "attachments": [
                    {
                        "id": "ambient-audio",
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 5,
                        "dataUrl": data_url,
                        "ephemeral": True,
                        "do_not_persist": True,
                    }
                ],
            },
        },
        {},
    )

    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert not any(
        isinstance(block, dict) and block.get("type") == "input_audio"
        for block in user_messages[-1]["content"]
    )
    assert any(block.get("type") == "text" and "音声入力" in block.get("text", "") for block in prepared.content)
    assert not (store.conversation_workspace_dir(conv["id"]) / "attachments" / "pinch.webm").exists()
    ChatStore._instance = None


def test_audio_attachment_decoded_size_limit_is_enforced(tmp_path, monkeypatch):
    from domain.chat import run_request
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    monkeypatch.setattr(run_request, "MAX_ATTACHMENT_AUDIO_BYTES", 4)
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:audio/webm;base64," + base64.b64encode(b"voice").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "",
                "attachments": [
                    {
                        "id": "ambient-audio",
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 4,
                        "dataUrl": data_url,
                        "ephemeral": True,
                        "do_not_persist": True,
                    }
                ],
            },
        },
        {},
    )

    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert not any(
        isinstance(block, dict) and block.get("type") == "input_audio"
        for block in user_messages[-1]["content"]
    )
    assert any(block.get("type") == "text" and "音声入力" in block.get("text", "") for block in prepared.content)
    ChatStore._instance = None


def test_transcribed_audio_attachment_is_sent_as_text_for_non_multimodal_models(tmp_path, monkeypatch):
    from domain.chat.modality_detector import detect_modalities
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:audio/webm;base64," + base64.b64encode(b"voice").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "",
                "attachments": [
                    {
                        "id": "ambient-audio",
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 5,
                        "dataUrl": data_url,
                        "ephemeral": True,
                        "do_not_persist": True,
                        "transcript": "今日の予定を要約して",
                        "transcript_source": "web_speech_api",
                    }
                ],
            },
        },
        {},
    )

    assert "workspace_attachments" not in prepared.metadata
    assert "dataUrl" not in prepared.metadata["attachments"][0]
    assert prepared.metadata["attachments"][0]["transcribed"] is True
    assert prepared.metadata["attachments"][0]["transcript_source"] == "web_speech_api"
    assert any(block.get("type") == "text" and "今日の予定を要約して" in block.get("text", "") for block in prepared.content)
    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert not any(
        isinstance(block, dict) and block.get("type") == "input_audio"
        for block in user_messages[-1]["content"]
    )
    assert detect_modalities(prepared.content, prepared.metadata)["has_audio"] is False
    assert not (store.conversation_workspace_dir(conv["id"]) / "attachments" / "pinch.webm").exists()
    ChatStore._instance = None


def test_transcribed_audio_attachment_can_include_audio_with_explicit_flag(tmp_path, monkeypatch):
    from domain.chat.run_request import prepare_chat_run
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "user_data" / "shared" / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conv = store.create_conversation(model="stub/default")
    data_url = "data:audio/webm;base64," + base64.b64encode(b"voice").decode()

    prepared = prepare_chat_run(
        {
            "conversation_id": conv["id"],
            "message": {
                "content": "",
                "attachments": [
                    {
                        "id": "ambient-audio",
                        "name": "pinch.webm",
                        "type": "audio/webm",
                        "size": 5,
                        "dataUrl": data_url,
                        "ephemeral": True,
                        "do_not_persist": True,
                        "transcript": "今日の予定を要約して",
                        "transcript_source": "web_speech_api",
                        "include_audio_with_transcript": True,
                    }
                ],
            },
        },
        {},
    )

    assert "dataUrl" not in prepared.metadata["attachments"][0]
    assert prepared.metadata["attachments"][0]["transcribed"] is True
    assert prepared.metadata["attachments"][0]["audio_included_with_transcript"] is True
    assert any(block.get("type") == "text" and "今日の予定を要約して" in block.get("text", "") for block in prepared.content)
    user_messages = [message for message in prepared.standard_messages if message.get("role") == "user"]
    assert any(
        isinstance(block, dict)
        and block.get("type") == "input_audio"
        and block.get("input_audio", {}).get("data") == base64.b64encode(b"voice").decode()
        for block in user_messages[-1]["content"]
    )
    assert not (store.conversation_workspace_dir(conv["id"]) / "attachments" / "pinch.webm").exists()
    ChatStore._instance = None
