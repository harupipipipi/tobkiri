"""Focused Wave 7 owner-boundary tests (defined, not run by implementer)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from backend_core.ecosystem.spec.schema.validator import validate_ecosystem
from scripts.quality.legacy_manifest_v3 import load_manifest
from core_runtime.pack_artifact_integrity import verify_declared_artifacts
from ecosystem.rumi_context_runtime_pack.runtime.materializer import (
    ContextMaterializer,
)
from ecosystem.rumi_conversation_store_pack.runtime.store import (
    ConversationConflict,
    ConversationStore,
)
from ecosystem.rumi_knowledge_store_pack.runtime.store import KnowledgeStore
from ecosystem.rumi_memory_store_pack.runtime.store import MemoryStore
from ecosystem.rumi_turn_runtime_pack.runtime.turns import TurnConflict, TurnRuntime


def test_conversation_store_contract_artifacts_are_activatable() -> None:
    """Keep the authoritative chat owner loadable by the desktop process."""
    pack_root = (
        Path(__file__).parents[1] / "ecosystem" / "rumi_conversation_store_pack"
    )
    manifest = load_manifest(pack_root / "rumi.pack.v3.json")
    assert manifest.ok, manifest.diagnostics

    ecosystem_manifest = json.loads(
        (pack_root / "ecosystem.json").read_text(encoding="utf-8")
    )
    assert validate_ecosystem(ecosystem_manifest, raise_on_error=False) == []
    assert ecosystem_manifest["vocabulary"]["types"] == ["service"]
    integrity_ok, diagnostics = verify_declared_artifacts(
        pack_root,
        ecosystem_manifest,
    )
    assert integrity_ok, diagnostics

    runtime_hash = "sha256:" + hashlib.sha256(
        (pack_root / "runtime" / "store.py").read_bytes()
    ).hexdigest()
    assert {
        entrypoint["artifact_hash"]
        for entrypoint in manifest.value["entrypoints"]
    } == {runtime_hash}


def test_turn_runtime_contract_artifacts_are_activatable() -> None:
    """Keep live conversation steering available in packaged profiles."""
    pack_root = Path(__file__).parents[1] / "ecosystem" / "rumi_turn_runtime_pack"
    manifest = load_manifest(pack_root / "rumi.pack.v3.json")
    assert manifest.ok, manifest.diagnostics

    ecosystem_manifest = json.loads(
        (pack_root / "ecosystem.json").read_text(encoding="utf-8")
    )
    integrity_ok, diagnostics = verify_declared_artifacts(
        pack_root,
        ecosystem_manifest,
    )
    assert integrity_ok, diagnostics

    runtime_hash = "sha256:" + hashlib.sha256(
        (pack_root / "runtime" / "turns.py").read_bytes()
    ).hexdigest()
    assert {
        entrypoint["artifact_hash"]
        for entrypoint in manifest.value["entrypoints"]
    } == {runtime_hash}


def test_conversation_and_messages_share_one_atomic_revision(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    created = store.create({"id": "conversation-1"}, expected_revision=0)
    revision = created["conversation"]["conversation_revision"]
    appended = store.append_message(
        "conversation-1",
        {"id": "message-1", "role": "user", "content": "hello"},
        expected_conversation_revision=revision,
    )
    conversation = store.get("conversation-1")
    assert conversation is not None
    assert appended["conversation_revision"] == revision + 1
    assert conversation["current_node_id"] == "message-1"
    assert [item["id"] for item in conversation["messages"]] == ["message-1"]


def test_conversation_parent_links_are_updated_atomically(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    parent = store.create({"id": "parent"}, expected_revision=0)
    store.create(
        {"id": "child", "parent_conversation_id": "parent"},
        expected_revision=parent["store_revision"],
    )

    linked_parent = store.get("parent")
    child = store.get("child")
    assert linked_parent is not None
    assert child is not None
    assert linked_parent["child_conversation_ids"] == ["child"]
    assert child["parent_conversation_id"] == "parent"

    store.delete(
        "parent",
        expected_conversation_revision=linked_parent["conversation_revision"],
    )

    detached_child = store.get("child")
    assert detached_child is not None
    assert detached_child["parent_conversation_id"] is None


def test_conversation_create_rejects_unknown_parent(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)

    with pytest.raises(KeyError, match="parent conversation"):
        store.create(
            {"id": "child", "parent_conversation_id": "missing"},
            expected_revision=0,
        )


def test_owner_drops_client_supplied_icon_svg(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    created = store.create(
        {
            "id": "conversation-1",
            "metadata": {
                "icon_id": "database",
                "icon_svg": '<svg onload="globalThis.pwned=true"></svg>',
            },
        },
        expected_revision=0,
    )

    assert created["conversation"]["metadata"] == {"icon_id": "database"}
    updated = store.update(
        "conversation-1",
        {"metadata": {"icon_svg": "<svg><script>x</script></svg>"}},
        expected_conversation_revision=created["conversation"][
            "conversation_revision"
        ],
    )
    assert "icon_svg" not in updated["conversation"]["metadata"]


def test_message_links_are_updated_and_repaired_atomically(
    tmp_path: Path,
) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    created = store.create({"id": "conversation-1"}, expected_revision=0)
    root = store.append_message(
        "conversation-1",
        {"id": "root"},
        expected_conversation_revision=created["conversation"][
            "conversation_revision"
        ],
    )
    child = store.append_message(
        "conversation-1",
        {"id": "child", "parent_id": "root"},
        expected_conversation_revision=root["conversation_revision"],
    )
    grandchild = store.append_message(
        "conversation-1",
        {"id": "grandchild", "parent_id": "child"},
        expected_conversation_revision=child["conversation_revision"],
    )

    linked = store.get("conversation-1")
    assert linked is not None
    by_id = {item["id"]: item for item in linked["messages"]}
    assert by_id["root"]["children_ids"] == ["child"]
    assert by_id["child"]["children_ids"] == ["grandchild"]

    store.mutate_message(
        "conversation-1",
        "child",
        expected_conversation_revision=grandchild["conversation_revision"],
        delete=True,
    )

    repaired = store.get("conversation-1")
    assert repaired is not None
    by_id = {item["id"]: item for item in repaired["messages"]}
    assert by_id["root"]["children_ids"] == ["grandchild"]
    assert by_id["grandchild"]["parent_id"] == "root"


def test_message_append_rejects_unknown_parent(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    created = store.create({"id": "conversation-1"}, expected_revision=0)

    with pytest.raises(KeyError, match="parent message"):
        store.append_message(
            "conversation-1",
            {"id": "message-1", "parent_id": "missing"},
            expected_conversation_revision=created["conversation"][
                "conversation_revision"
            ],
        )


def test_message_replace_is_atomic_and_revision_guarded(tmp_path: Path) -> None:
    store = ConversationStore("default", user_data_root=tmp_path)
    created = store.create({"id": "conversation-1"}, expected_revision=0)
    revision = created["conversation"]["conversation_revision"]
    result = store.replace_messages(
        "conversation-1",
        [{"id": "a"}, {"id": "b"}],
        expected_conversation_revision=revision,
    )
    assert [item["sequence"] for item in result["messages"]] == [0, 1]
    with pytest.raises(ConversationConflict):
        store.replace_messages(
            "conversation-1",
            [{"id": "c"}],
            expected_conversation_revision=revision,
        )


def test_turn_guidance_is_revision_bound_and_consumed_once() -> None:
    runtime = TurnRuntime()
    turn = runtime.begin(
        {
            "turn_id": "turn-1",
            "request_id": "request-1",
            "conversation_id": "conversation-1",
            "conversation_revision": 1,
        }
    )
    turn = runtime.steer(
        "turn-1", {"prompt": "continue"}, expected_revision=turn["revision"]
    )
    consumed = runtime.consume_guidance(
        "turn-1", expected_revision=turn["revision"]
    )
    assert [item["value"]["prompt"] for item in consumed["items"]] == ["continue"]
    again = runtime.consume_guidance(
        "turn-1", expected_revision=consumed["turn"]["revision"]
    )
    assert again["items"] == []
    with pytest.raises(TurnConflict):
        runtime.steer("turn-1", {}, expected_revision=1)


class _ContextClient:
    def invoke(self, contract_id: str, operation: str, payload: dict) -> object:
        assert operation in {"get", "search"}
        if contract_id == "rumi.resource.conversation.v1":
            return {
                "id": payload["conversation_id"],
                "conversation_revision": 3,
                "messages": [{"id": "m1", "content": "hello"}],
            }
        if contract_id == "rumi.resource.memory.v1":
            return {"revision": 4, "items": [{"id": "mem1", "content": "memory"}]}
        return {"revision": 5, "items": [{"id": "k1", "content": "knowledge"}]}


def test_context_is_derived_and_revision_bound() -> None:
    result = ContextMaterializer(_ContextClient()).materialize(
        {
            "profile_id": "default",
            "conversation_id": "conversation-1",
            "conversation_revision": 3,
            "query": "hello",
            "token_budget": 4096,
        }
    )
    assert result["authoritative"] is False
    assert result["persisted"] is False
    assert result["source_revisions"] == {
        "conversation": 3,
        "memory": 4,
        "knowledge": 5,
    }


def test_memory_migration_is_source_hash_and_marker_bound(tmp_path: Path) -> None:
    store = MemoryStore("default", user_data_root=tmp_path)
    records = [
        {
            "id": "memory-1",
            "content": "remember",
            "created_at": 1,
            "updated_at": 1,
        }
    ]
    source = {"items": [store_module_record(records[0])]}
    digest = hashlib.sha256(
        json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    migrated = store.migrate(records, expected_source_hash=digest)
    assert store.snapshot()["items"][0]["id"] == "memory-1"
    store.rollback(migrated["migration_id"])
    assert store.snapshot()["items"] == []


def store_module_record(value: dict) -> dict:
    """Normalize a deterministic migration fixture without wall-clock defaults."""
    return {
        "id": value["id"],
        "content": value["content"],
        "scope": "user",
        "source": "user",
        "metadata": {},
        "created_at": 1,
        "updated_at": 1,
        "expires_at": None,
        "record_revision": 1,
    }


def test_knowledge_owner_persists_no_embedding(tmp_path: Path) -> None:
    store = KnowledgeStore("default", user_data_root=tmp_path)
    result = store.put(
        {"id": "knowledge-1", "content": "local knowledge", "embedding": [1.0]},
        expected_revision=0,
    )
    assert "embedding" not in result["item"]
    assert store.search("local")["index_kind"] == "derived_local_text"


def test_legacy_owner_modules_contain_no_old_storage_writes() -> None:
    root = Path(__file__).parents[1] / "ecosystem" / "defaultspack"
    targets = [
        root / "domain" / "chat" / "store.py",
        root / "domain" / "chat" / "steer.py",
        root / "domain" / "memory" / "store.py",
        root / "domain" / "memory2" / "sqlite_store.py",
        root / "domain" / "memory2" / "markdown_store.py",
        root / "domain" / "knowledge" / "store.py",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in targets)
    for forbidden in (
        "sqlite3.connect",
        "sqlite_wal_connection",
        "steer_queue.json",
        "conversations.json",
        "shared/knowledge",
    ):
        assert forbidden not in source
