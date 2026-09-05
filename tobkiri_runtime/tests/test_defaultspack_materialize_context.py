from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest


ROOT = Path(__file__).resolve().parent.parent
DEFAULTSPACK_ROOT = ROOT / "ecosystem" / "defaultspack"

sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(DEFAULTSPACK_ROOT))

pytestmark = pytest.mark.usefixtures("wave7_owner_bindings")


@pytest.fixture
def context_contract_bindings(monkeypatch):
    """Install an explicit local context plan for materialization tests."""

    from core_runtime.di_container import get_container
    from ecosystem.rumi_context_runtime_pack.runtime.materializer import (
        create_context_operation,
    )
    from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationStore
    from ecosystem.rumi_memory_store_pack.runtime.store import MemoryStore

    class _ContextClient:
        def invoke(self, contract_id, operation, payload):
            if contract_id == "rumi.resource.conversation.v1":
                raw_path = Path(os.environ["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"])
                owner = ConversationStore("defaults", user_data_root=raw_path.parent)
                owner.root = raw_path.parent
                owner.path = raw_path
                owner.backup_root = raw_path.parent / "migration_backups"
                owner.lock_root = raw_path.parent / "locks"
                return owner.get(payload.get("conversation_id", ""))
            if contract_id == "rumi.resource.memory.v1":
                raw_root = Path(
                    os.environ.get(
                        "RUMI_DEFAULTSPACK_MEMORY2_DIR",
                        Path(os.environ["RUMI_DEFAULTSPACK_CHAT_STORE_PATH"]).parent
                        / "memory",
                    )
                )
                owner = MemoryStore("default", user_data_root=raw_root)
                owner.root = raw_root
                owner.path = raw_root / "memories.json"
                owner.backup_root = raw_root / "migration_backups"
                owner.lock_root = raw_root / "locks"
                return owner.search(
                    str(payload.get("query") or ""),
                    limit=int(payload.get("limit") or 8),
                )
            if contract_id == "rumi.resource.knowledge.v1":
                return {"revision": 0, "items": []}
            raise AssertionError(f"unexpected context contract: {contract_id}")

    class _Plan:
        profile_id = "defaults"
        effective_pack_set = frozenset(
            {
                "rumi_context_runtime_pack",
                "rumi_conversation_store_pack",
                "rumi_memory_store_pack",
                "rumi_knowledge_store_pack",
            }
        )
        effective_permissions = frozenset({"context.materialize"})
        providers = ()

    plan = _Plan()
    class _ContextInterfaceRegistry:
        def __init__(self):
            self._store = {}

        def register(self, key, value, meta=None):
            del meta
            self._store.setdefault(key, []).append(value)

        def get(self, key, strategy="last"):
            values = list(self._store.get(key, ()))
            return values if strategy == "all" else (values[-1] if values else None)

    registry = _ContextInterfaceRegistry()
    provider_specs = {
        "rumi.service.context.v1": (
            "rumi_context_runtime_pack",
            "context-runtime.materialize",
            create_context_operation(_ContextClient()),
            {"context.materialize"},
        ),
            "rumi.resource.conversation.v1": (
            "rumi_conversation_store_pack",
            "conversation-store.resource",
            lambda operation, payload: _ContextClient().invoke(
                "rumi.resource.conversation.v1", operation, payload
            ),
            set(),
        ),
        "rumi.resource.memory.v1": (
            "rumi_memory_store_pack",
            "memory-store.resource",
            lambda operation, payload: _ContextClient().invoke(
                "rumi.resource.memory.v1", operation, payload
            ),
            set(),
        ),
        "rumi.resource.knowledge.v1": (
            "rumi_knowledge_store_pack",
            "knowledge-store.resource",
            lambda operation, payload: _ContextClient().invoke(
                "rumi.resource.knowledge.v1", operation, payload
            ),
            set(),
        ),
    }
    for contract_id, (source_pack_id, instance_id, operation, capabilities) in provider_specs.items():
        registry.register(
            f"global_contract.provider.{contract_id}",
            {
                "contract_id": contract_id,
                "source_pack_id": source_pack_id,
                "provider_instance_id": instance_id,
                "content_hash": f"test:{instance_id}",
                "required_capabilities": sorted(capabilities),
                "operation": operation,
            },
        )
        plan.providers += (
            SimpleNamespace(
                contract_id=contract_id,
                source_pack_id=source_pack_id,
                provider_instance_id=instance_id,
                content_hash=f"test:{instance_id}",
            ),
        )

    class _ContextDispatchSession:
        profile_id = plan.profile_id
        plan_digest = "sha256:" + "8" * 64

        def invoke(self, contract_id, operation_name, payload):
            return provider_specs[contract_id][2](operation_name, payload)

        def provider_metadata(self, contract_id):
            source_pack_id, instance_id, _operation, _capabilities = provider_specs[
                contract_id
            ]
            return (
                {
                    "contract_id": contract_id,
                    "source_pack_id": source_pack_id,
                    "provider_instance_id": instance_id,
                    "content_hash": f"test:{instance_id}",
                },
            )

    container = get_container()
    marker = object()
    previous = {
        "interface_registry": container._instances.get("interface_registry", marker),
        "v4_dispatch_session": container._instances.get(
            "v4_dispatch_session", marker
        ),
    }
    container.set_instance("interface_registry", registry)
    container.set_instance("v4_dispatch_session", _ContextDispatchSession())
    monkeypatch.setattr(
        "blocks.chat.materialize_context.active_resolved_profile", lambda: plan
    )
    try:
        yield
    finally:
        if previous["interface_registry"] is marker:
            container._instances.pop("interface_registry", None)
        else:
            container._instances["interface_registry"] = previous[
                "interface_registry"
            ]
        if previous["v4_dispatch_session"] is marker:
            container._instances.pop("v4_dispatch_session", None)
        else:
            container._instances["v4_dispatch_session"] = previous[
                "v4_dispatch_session"
            ]


def _create_conversation(tmp_path, monkeypatch):
    from domain.chat.store import ChatStore

    monkeypatch.setenv("RUMI_DEFAULTSPACK_CHAT_STORE_PATH", str(tmp_path / "chat" / "conversations.json"))
    ChatStore._instance = None
    store = ChatStore()
    conversation = store.create_conversation(model="stub/default")
    store.add_message(
        conversation["id"],
        {"role": "user", "content": [{"type": "text", "text": "Hello context"}]},
    )
    store.add_message(
        conversation["id"],
        {"role": "assistant", "content": [{"type": "text", "text": "Assistant reply"}]},
    )
    return conversation


@pytest.mark.usefixtures("context_contract_bindings")
def test_context_txt_template_command_materializes_artifact(tmp_path, monkeypatch):
    from blocks.chat import materialize_context
    from domain.chat.store import ChatStore
    from domain.frontend.command_registry import SlashCommandRegistry

    conversation = _create_conversation(tmp_path, monkeypatch)

    pack_root = tmp_path / "defaultspack"
    template_path = pack_root / "templates" / "context_txt" / "default" / "template.json"
    template_path.parent.mkdir(parents=True, exist_ok=True)
    template_path.write_text(
        json.dumps(
            {
                "id": "rumi.test.context_txt",
                "kind": "frontend",
                "version": "1.0.0",
                "status": "active",
                "pieces": [
                    {
                        "id": "context_txt_action",
                        "kind": "function",
                        "role": "action",
                        "action_id": "context_txt",
                        "slash_command": {
                            "id": "context_txt",
                            "name": "context_txt",
                            "label": "Context TXT",
                            "modes": ["chat", "coding", "agent"],
                            "execution": {
                                "type": "pack_block",
                                "qualified_name": "defaultspack:chat.materialize_context",
                            },
                        },
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    artifact_root = tmp_path / "artifacts"
    fake_module = SimpleNamespace(
        __file__=str(pack_root / "blocks" / "chat" / "materialize_context.py"),
        run=materialize_context.run,
    )
    real_import_module = importlib.import_module

    def import_for_registry(module_name):
        if module_name == "blocks.chat.materialize_context":
            return fake_module
        return real_import_module(module_name)

    try:
        with patch(
            "domain.frontend.command_registry.importlib.import_module",
            side_effect=import_for_registry,
        ):
            result = SlashCommandRegistry(pack_root).execute(
                {
                    "command": "context_txt",
                    "mode": "chat",
                    "conversation_id": conversation["id"],
                    "args": {},
                },
                {"artifact_root": str(artifact_root)},
            )
    finally:
        ChatStore._instance = None

    assert result["status"] == "ok"
    assert result["data"]["executed"] is True
    assert result["data"]["message"].startswith("Materialized conversation context")
    data = result["data"]["result"]
    assert data["conversation_id"] == conversation["id"]
    assert data["path"].endswith(".txt")
    assert data["filename"] == Path(data["path"]).name
    assert data["name"] == data["filename"]
    assert data["format"] == "text"
    assert data["mime_type"] == "text/plain"
    assert data["content_type"] == "text/plain"
    assert data["artifacts"] == [
        {
            "path": data["path"],
            "filename": data["filename"],
            "name": data["filename"],
            "size": data["size"],
            "format": "text",
            "mime_type": "text/plain",
        }
    ]
    output_path = (artifact_root / data["path"]).resolve()
    output_path.relative_to(artifact_root.resolve())
    assert output_path.is_file()
    assert data["size"] == output_path.stat().st_size
    content = output_path.read_text(encoding="utf-8")
    assert "Hello context" in content
    assert "Assistant reply" in content
    assert "### User" not in content
    assert not content.lstrip().startswith("#")


@pytest.mark.usefixtures("context_contract_bindings")
def test_materialize_context_honors_text_aliases(tmp_path, monkeypatch):
    from blocks.chat import materialize_context
    from domain.chat.store import ChatStore

    conversation = _create_conversation(tmp_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"

    try:
        for format_alias in ("text", "txt"):
            result = materialize_context.run(
                {"conversation_id": conversation["id"], "format": format_alias},
                {"artifact_root": str(artifact_root)},
            )

            assert result["status"] == "ok"
            data = result["data"]
            assert data["path"].endswith(".txt")
            assert data["filename"] == Path(data["path"]).name
            assert data["format"] == "text"
            assert data["mime_type"] == "text/plain"
            output_path = (artifact_root / data["path"]).resolve()
            output_path.relative_to(artifact_root.resolve())
            content = output_path.read_text(encoding="utf-8")
            assert "Hello context" in content
            assert "### User" not in content
            assert not content.lstrip().startswith("#")
    finally:
        ChatStore._instance = None


def test_materialized_audio_transcript_blocks_are_shared_for_ambient_recordings():
    from blocks.chat.materialize_context import materialized_audio_transcript_blocks

    blocks = materialized_audio_transcript_blocks(
        [
            {
                "name": "ok-mark-recording.webm",
                "type": "audio/webm",
                "metadata": {"transcript": "hello こんにちは"},
                "dataUrl": "data:audio/webm;base64,AAAA",
            },
            {
                "name": "camera-frame.png",
                "type": "image/png",
                "transcript": "ignored",
            },
        ]
    )

    assert blocks == [
        {
            "type": "text",
            "text": "\n\n音声入力の文字起こし: ok-mark-recording.webm\nhello こんにちは",
        }
    ]


def test_chat_run_skips_audio_transcript_block_when_text_already_contains_transcript():
    from domain.chat.run_request import _attachment_audio_transcript_blocks

    blocks = _attachment_audio_transcript_blocks(
        [
            {
                "name": "debug-ok-mark.webm",
                "type": "audio/webm",
                "metadata": {"transcript": "ブラウザQAのテストです。"},
                "dataUrl": "data:audio/webm;base64,AAAA",
            }
        ],
        existing_text="文字起こし:\nブラウザQAのテストです。",
    )

    assert blocks == []


@pytest.mark.usefixtures("context_contract_bindings")
def test_materialize_context_honors_markdown_alias_and_metadata(tmp_path, monkeypatch):
    from blocks.chat import materialize_context
    from domain.chat.store import ChatStore

    conversation = _create_conversation(tmp_path, monkeypatch)
    artifact_root = tmp_path / "artifacts"

    try:
        result = materialize_context.run(
            {"conversation_id": conversation["id"], "format": "md"},
            {"artifact_root": str(artifact_root)},
        )
    finally:
        ChatStore._instance = None

    assert result["status"] == "ok"
    data = result["data"]
    assert data["conversation_id"] == conversation["id"]
    assert data["path"].endswith(".md")
    assert data["filename"] == Path(data["path"]).name
    assert data["name"] == data["filename"]
    assert data["format"] == "markdown"
    assert data["mime_type"] == "text/markdown"
    assert data["content_type"] == "text/markdown"
    assert data["artifacts"] == [
        {
            "path": data["path"],
            "filename": data["filename"],
            "name": data["filename"],
            "size": data["size"],
            "format": "markdown",
            "mime_type": "text/markdown",
        }
    ]
    output_path = (artifact_root / data["path"]).resolve()
    output_path.relative_to(artifact_root.resolve())
    assert output_path.is_file()
    assert data["size"] == output_path.stat().st_size
    content = output_path.read_text(encoding="utf-8")
    assert content.startswith("# ")
    assert "### User" in content
    assert "Hello context" in content
