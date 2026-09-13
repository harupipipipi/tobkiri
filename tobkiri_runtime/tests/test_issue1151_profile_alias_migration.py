"""Issue #1151 acceptance coverage for saved profile aliases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ecosystem.rumi_model_registry_pack.runtime.registry import ModelRegistry


class _ConversationStore:
    """Deterministic owner fixture that persists identity, not an alias only."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def save(self, conversation: dict[str, Any]) -> None:
        self.path.write_text(
            json.dumps(conversation, ensure_ascii=False, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def load(self) -> dict[str, Any]:
        value = json.loads(self.path.read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        return value


def test_alias_migration_restart_and_rollback_preserve_conversation_identity(
    tmp_path: Path,
) -> None:
    registry = ModelRegistry("default", user_data_root=tmp_path / "registry")
    first = registry.save(
        {
            "model_profile_id": "profile-a",
            "display_name": "Provider A profile",
            "model_id": "provider-a/model",
            "requirements": {
                "preferred_provider_instance_id": "provider-a"
            },
        },
        expected_revision=0,
    )
    registry.set_alias("default", "profile-a", expected_revision=1)
    resolved_before = registry.resolve("default")
    assert resolved_before is not None

    store = _ConversationStore(tmp_path / "conversation.json")
    conversation = {
        "conversation_id": "conversation-1151",
        "profile_alias": "default",
        "profile_id": resolved_before["resolved_profile_id"],
        "model_id": resolved_before["profile"]["model_id"],
        "provider_instance_id": "provider-a",
        "messages": [{"role": "user", "content": "keep this"}],
    }
    store.save(conversation)

    second = registry.save(
        {
            "model_profile_id": "profile-b",
            "display_name": "Provider B profile",
            "model_id": "provider-b/model",
            "requirements": {
                "preferred_provider_instance_id": "provider-b"
            },
        },
        expected_revision=first["store_revision"] + 1,
    )
    registry.set_alias(
        "default", "profile-b", expected_revision=second["store_revision"]
    )

    restarted = ModelRegistry("default", user_data_root=tmp_path / "registry")
    migrated = restarted.resolve("default")
    assert migrated is not None
    assert migrated["resolved_profile_id"] == "profile-b"
    assert migrated["profile"]["model_id"] == "provider-b/model"
    assert store.load() == conversation

    restarted.set_alias("default", "profile-a", expected_revision=4)
    rolled_back = ModelRegistry("default", user_data_root=tmp_path / "registry")
    restored = rolled_back.resolve("default")
    assert restored is not None
    assert restored["resolved_profile_id"] == "profile-a"
    assert restored["profile"]["model_id"] == "provider-a/model"
    assert store.load() == conversation
    assert store.load()["conversation_id"] == "conversation-1151"
    assert store.load()["provider_instance_id"] == "provider-a"
