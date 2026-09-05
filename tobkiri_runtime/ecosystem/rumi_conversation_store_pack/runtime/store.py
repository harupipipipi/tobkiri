"""Atomic profile-bound conversation and ordered-message owner."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.paths import USER_DATA_DIR
from core_runtime.profile_workspace import validate_profile_id
from core_runtime.runtime_locks import NamedLock

STORE_VERSION = "rumi.conversation-store.v1"
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")


class ConversationConflict(RuntimeError):
    """Raised for stale owner-store or conversation revisions."""


class ConversationStore:
    """Own conversations and their ordered messages in one atomic store."""

    def __init__(
        self,
        profile_id: str,
        *,
        user_data_root: Path | None = None,
    ) -> None:
        self.profile_id = validate_profile_id(profile_id)
        self.root = (
            Path(user_data_root or USER_DATA_DIR)
            / "packs"
            / "rumi_conversation_store_pack"
            / "profiles"
            / self.profile_id
        )
        self.path = self.root / "conversations.json"
        self.backup_root = self.root / "migration_backups"
        self.lock_root = self.root / "locks"

    def snapshot(self) -> dict[str, Any]:
        """Return all conversation records in deterministic order."""
        state = self._read()
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": state["revision"],
            "conversations": [
                _copy(state["conversations"][key])
                for key in sorted(state["conversations"])
            ],
            "migration": _copy(state.get("migration")),
        }

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        """Return one complete conversation with ordered messages."""
        value = self._read()["conversations"].get(_identifier(conversation_id))
        return _copy(value) if isinstance(value, Mapping) else None

    def create(
        self,
        record: Mapping[str, Any],
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        """Create one conversation at an exact store revision."""
        normalized = _conversation(record, allow_messages=False)
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            _assert_store_revision(state, expected_revision)
            conversation_id = normalized["id"]
            if conversation_id in state["conversations"]:
                raise ConversationConflict("conversation already exists")
            now = _now_ms()
            normalized["child_conversation_ids"] = []
            parent_id = normalized.get("parent_conversation_id")
            if parent_id is not None:
                parent_id = _identifier(parent_id)
                parent = state["conversations"].get(parent_id)
                if not isinstance(parent, Mapping):
                    raise KeyError("parent conversation is unknown")
                parent = dict(parent)
                child_ids = list(parent.get("child_conversation_ids") or [])
                if conversation_id not in child_ids:
                    child_ids.append(conversation_id)
                parent["child_conversation_ids"] = child_ids
                parent["updated_at"] = now
                parent["conversation_revision"] = (
                    int(parent.get("conversation_revision") or 0) + 1
                )
                state["conversations"][parent_id] = parent
            normalized.update(
                {
                    "created_at": int(normalized.get("created_at") or now),
                    "updated_at": now,
                    "conversation_revision": 1,
                    "messages": [],
                }
            )
            state["conversations"][conversation_id] = normalized
            state["revision"] += 1
            self._write(state)
        return {
            "action": "created",
            "conversation": _copy(normalized),
            "store_revision": state["revision"],
        }

    def update(
        self,
        conversation_id: str,
        patch: Mapping[str, Any],
        *,
        expected_conversation_revision: int,
    ) -> dict[str, Any]:
        """Update mutable conversation metadata at an exact record revision."""
        conversation_id = _identifier(conversation_id)
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            current = state["conversations"].get(conversation_id)
            if not isinstance(current, Mapping):
                raise KeyError("conversation is unknown")
            current = dict(current)
            _assert_conversation_revision(current, expected_conversation_revision)
            prior_parent_id = current.get("parent_conversation_id")
            requested_parent_id = (
                patch.get("parent_conversation_id")
                if "parent_conversation_id" in patch
                else prior_parent_id
            )
            if requested_parent_id is not None:
                requested_parent_id = _identifier(requested_parent_id)
                if requested_parent_id == conversation_id:
                    raise ValueError("conversation cannot be its own parent")
                if requested_parent_id not in state["conversations"]:
                    raise KeyError("parent conversation is unknown")
            for key in (
                "title",
                "model_reference",
                "system_prompt_id",
                "agent_id",
                "tags",
                "is_starred",
                "is_pinned",
                "pinned_at",
                "pin_scope",
                "is_archived",
                "current_node_id",
                "parent_conversation_id",
                "conversation_kind",
                "group_id",
                "metadata",
            ):
                if key in patch:
                    current[key] = (
                        _conversation_metadata(patch[key])
                        if key == "metadata"
                        else _safe(patch[key])
                    )
            now = _now_ms()
            if requested_parent_id != prior_parent_id:
                _relink_conversation_parent(
                    state["conversations"],
                    conversation_id,
                    prior_parent_id,
                    requested_parent_id,
                    now,
                )
            current["updated_at"] = now
            current["conversation_revision"] += 1
            state["conversations"][conversation_id] = current
            state["revision"] += 1
            self._write(state)
        return {
            "action": "updated",
            "conversation": _copy(current),
            "store_revision": state["revision"],
        }

    def delete(
        self,
        conversation_id: str,
        *,
        expected_conversation_revision: int,
    ) -> dict[str, Any]:
        """Delete a conversation and all owned messages atomically."""
        conversation_id = _identifier(conversation_id)
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            current = state["conversations"].get(conversation_id)
            if not isinstance(current, Mapping):
                raise KeyError("conversation is unknown")
            _assert_conversation_revision(current, expected_conversation_revision)
            message_count = len(current.get("messages") or [])
            now = _now_ms()
            parent_id = current.get("parent_conversation_id")
            if parent_id in state["conversations"]:
                parent = dict(state["conversations"][parent_id])
                parent["child_conversation_ids"] = [
                    child_id
                    for child_id in parent.get("child_conversation_ids") or []
                    if child_id != conversation_id
                ]
                _touch_conversation(parent, now)
                state["conversations"][parent_id] = parent
            for child_id, child_value in list(state["conversations"].items()):
                if child_id == conversation_id or not isinstance(
                    child_value, Mapping
                ):
                    continue
                if child_value.get("parent_conversation_id") != conversation_id:
                    continue
                child = dict(child_value)
                child["parent_conversation_id"] = None
                _touch_conversation(child, now)
                state["conversations"][child_id] = child
            del state["conversations"][conversation_id]
            state["revision"] += 1
            self._write(state)
        return {
            "action": "deleted",
            "conversation_id": conversation_id,
            "deleted_messages": message_count,
            "store_revision": state["revision"],
        }

    def append_message(
        self,
        conversation_id: str,
        message: Mapping[str, Any],
        *,
        expected_conversation_revision: int,
    ) -> dict[str, Any]:
        """Append one normalized message at an exact conversation revision."""
        conversation_id = _identifier(conversation_id)
        normalized = _message(message)
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            current = state["conversations"].get(conversation_id)
            if not isinstance(current, Mapping):
                raise KeyError("conversation is unknown")
            current = dict(current)
            _assert_conversation_revision(current, expected_conversation_revision)
            messages = list(current.get("messages") or [])
            if any(item.get("id") == normalized["id"] for item in messages):
                raise ConversationConflict("message already exists")
            normalized["children_ids"] = []
            parent_id = normalized.get("parent_id")
            if parent_id is not None:
                parent_id = _identifier(parent_id)
                parent = next(
                    (
                        item
                        for item in messages
                        if item.get("id") == parent_id
                    ),
                    None,
                )
                if parent is None:
                    raise KeyError("parent message is unknown")
                parent = dict(parent)
                child_ids = list(parent.get("children_ids") or [])
                if normalized["id"] not in child_ids:
                    child_ids.append(normalized["id"])
                parent["children_ids"] = child_ids
                messages = [
                    parent if item.get("id") == parent_id else item
                    for item in messages
                ]
            normalized["sequence"] = len(messages)
            messages.append(normalized)
            current["messages"] = messages
            current["current_node_id"] = normalized["id"]
            current["updated_at"] = _now_ms()
            current["conversation_revision"] += 1
            state["conversations"][conversation_id] = current
            state["revision"] += 1
            self._write(state)
        return {
            "action": "message_appended",
            "message": _copy(normalized),
            "conversation_revision": current["conversation_revision"],
            "store_revision": state["revision"],
        }

    def mutate_message(
        self,
        conversation_id: str,
        message_id: str,
        *,
        expected_conversation_revision: int,
        patch: Mapping[str, Any] | None = None,
        delete: bool = False,
    ) -> dict[str, Any]:
        """Update or delete one message while preserving sequence order."""
        conversation_id = _identifier(conversation_id)
        message_id = _identifier(message_id)
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            current = state["conversations"].get(conversation_id)
            if not isinstance(current, Mapping):
                raise KeyError("conversation is unknown")
            current = dict(current)
            _assert_conversation_revision(current, expected_conversation_revision)
            messages = [dict(item) for item in current.get("messages") or []]
            index = next(
                (i for i, item in enumerate(messages) if item.get("id") == message_id),
                None,
            )
            if index is None:
                raise KeyError("message is unknown")
            if delete:
                deleted = messages[index]
                parent_id = deleted.get("parent_id")
                if parent_id is not None and not any(
                    item.get("id") == parent_id for item in messages
                ):
                    parent_id = None
                child_ids = {
                    str(item.get("id"))
                    for item in messages
                    if item.get("parent_id") == message_id
                }
                child_ids.update(
                    str(child_id)
                    for child_id in deleted.get("children_ids") or []
                    if any(
                        item.get("id") == child_id for item in messages
                    )
                )
                del messages[index]
                for item in messages:
                    if item.get("id") == parent_id:
                        item["children_ids"] = [
                            child_id
                            for child_id in item.get("children_ids") or []
                            if child_id != message_id
                        ]
                        for child_id in sorted(child_ids):
                            if child_id not in item["children_ids"]:
                                item["children_ids"].append(child_id)
                    if item.get("id") in child_ids:
                        item["parent_id"] = parent_id
                    else:
                        item["children_ids"] = [
                            child_id
                            for child_id in item.get("children_ids") or []
                            if child_id != message_id
                        ]
                action = "message_deleted"
                result_message = None
            else:
                prior_parent_id = messages[index].get("parent_id")
                requested_parent_id = (
                    patch.get("parent_id")
                    if patch is not None and "parent_id" in patch
                    else prior_parent_id
                )
                if requested_parent_id is not None:
                    requested_parent_id = _identifier(requested_parent_id)
                    if requested_parent_id == message_id:
                        raise ValueError("message cannot be its own parent")
                    if not any(
                        item.get("id") == requested_parent_id
                        for item in messages
                    ):
                        raise KeyError("parent message is unknown")
                if requested_parent_id != prior_parent_id:
                    for item in messages:
                        if item.get("id") == prior_parent_id:
                            item["children_ids"] = [
                                child_id
                                for child_id in item.get("children_ids") or []
                                if child_id != message_id
                            ]
                        if item.get("id") == requested_parent_id:
                            child_ids = list(item.get("children_ids") or [])
                            if message_id not in child_ids:
                                child_ids.append(message_id)
                            item["children_ids"] = child_ids
                    messages[index]["parent_id"] = requested_parent_id
                for key in (
                    "content",
                    "parts",
                    "metadata",
                    "status",
                    "sequence_number",
                    "raw_text",
                    "finish_reason",
                    "usage",
                    "widget",
                    "events",
                    "tool_logs",
                ):
                    if patch is not None and key in patch:
                        messages[index][key] = _safe(patch[key])
                messages[index]["updated_at"] = _now_ms()
                action = "message_updated"
                result_message = _copy(messages[index])
            for sequence, item in enumerate(messages):
                item["sequence"] = sequence
            current["messages"] = messages
            if delete and current.get("current_node_id") == message_id:
                current["current_node_id"] = parent_id
            current["updated_at"] = _now_ms()
            current["conversation_revision"] += 1
            state["conversations"][conversation_id] = current
            state["revision"] += 1
            self._write(state)
        return {
            "action": action,
            "message": result_message,
            "message_id": message_id,
            "conversation_revision": current["conversation_revision"],
            "store_revision": state["revision"],
        }

    def replace_messages(
        self,
        conversation_id: str,
        messages: list[Mapping[str, Any]],
        *,
        expected_conversation_revision: int,
    ) -> dict[str, Any]:
        """Replace the complete ordered message set in one owner transaction."""
        conversation_id = _identifier(conversation_id)
        normalized = [_message(item) for item in messages]
        message_ids = [item["id"] for item in normalized]
        if len(message_ids) != len(set(message_ids)):
            raise ValueError("duplicate message ID in replacement")
        _normalize_message_links(normalized)
        for sequence, item in enumerate(normalized):
            item["sequence"] = sequence
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            current = state["conversations"].get(conversation_id)
            if not isinstance(current, Mapping):
                raise KeyError("conversation is unknown")
            current = dict(current)
            _assert_conversation_revision(current, expected_conversation_revision)
            current["messages"] = normalized
            current["current_node_id"] = (
                normalized[-1]["id"] if normalized else None
            )
            current["updated_at"] = _now_ms()
            current["conversation_revision"] += 1
            state["conversations"][conversation_id] = current
            state["revision"] += 1
            self._write(state)
        return {
            "action": "messages_replaced",
            "messages": _copy(normalized),
            "conversation_revision": current["conversation_revision"],
            "store_revision": state["revision"],
        }

    def migrate(
        self,
        conversations: list[Mapping[str, Any]],
        *,
        expected_source_hash: str,
    ) -> dict[str, Any]:
        """Import a complete deterministic legacy snapshot exactly once."""
        raw = [dict(item) for item in conversations]
        raw.sort(key=lambda item: str(item.get("id") or ""))
        source = {"conversations": raw}
        source_hash = _hash(source)
        if source_hash != str(expected_source_hash or ""):
            raise ConversationConflict("conversation migration source changed")
        normalized = [_conversation(item, allow_messages=True) for item in raw]
        ids = [item["id"] for item in normalized]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate conversation ID in migration")
        with NamedLock(self.lock_root, "conversations"):
            if self.path.is_file():
                raise RuntimeError("conversation target is already initialized")
            migration_id = f"migration-{uuid.uuid4().hex}"
            backup = self.backup_root / migration_id
            backup.mkdir(parents=True, exist_ok=False)
            os.chmod(backup, 0o700)
            _atomic_json(backup / "legacy-conversations.json", source)
            state = self._empty()
            state["conversations"] = {item["id"]: item for item in normalized}
            state["revision"] = 1
            state["migration"] = {
                "migration_id": migration_id,
                "source_hash": source_hash,
                "backup": str(backup),
            }
            self._write(state)
        return {
            "migration_id": migration_id,
            "source_hash": source_hash,
            "conversations": len(normalized),
            "messages": sum(len(item["messages"]) for item in normalized),
        }

    def rollback_migration(self, migration_id: str) -> dict[str, Any]:
        """Remove migrated owner state for an exact migration marker."""
        with NamedLock(self.lock_root, "conversations"):
            state = self._read()
            marker = state.get("migration")
            if not isinstance(marker, Mapping) or marker.get(
                "migration_id"
            ) != migration_id:
                raise ValueError("conversation migration marker mismatch")
            _atomic_json(self.root / f"rollback-{migration_id}.json", state)
            self.path.unlink(missing_ok=True)
        return {"migration_id": migration_id, "rolled_back": True}

    def _read(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        value = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(value, dict)
            or value.get("version") != STORE_VERSION
            or value.get("profile_id") != self.profile_id
            or not isinstance(value.get("conversations"), dict)
        ):
            raise ValueError("conversation store is invalid")
        conversations: dict[str, Any] = {}
        for conversation_id, raw in value["conversations"].items():
            if not isinstance(raw, Mapping):
                conversations[conversation_id] = raw
                continue
            conversation = dict(raw)
            conversation["metadata"] = _conversation_metadata(
                conversation.get("metadata")
            )
            conversations[conversation_id] = conversation
        return {**value, "conversations": conversations}

    def _write(self, state: Mapping[str, Any]) -> None:
        _atomic_json(self.path, state)

    def _empty(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "profile_id": self.profile_id,
            "revision": 0,
            "conversations": {},
            "migration": None,
        }


def create_conversation_resource(
    client: Any,
) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create conversation resource operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = ConversationStore(_profile(payload))
        if name == "list":
            return store.snapshot()
        if name == "get":
            return store.get(str(payload.get("conversation_id") or ""))
        raise ValueError(f"unknown conversation resource operation: {name}")

    return operation


def create_conversation_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create conversation mutation operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = ConversationStore(_profile(payload))
        if name == "create":
            return store.create(
                _mapping(payload.get("conversation")),
                expected_revision=int(payload.get("expected_revision") or 0),
            )
        if name == "update":
            return store.update(
                str(payload.get("conversation_id") or ""),
                _mapping(payload.get("patch")),
                expected_conversation_revision=int(
                    payload.get("expected_conversation_revision") or 0
                ),
            )
        if name == "delete":
            return store.delete(
                str(payload.get("conversation_id") or ""),
                expected_conversation_revision=int(
                    payload.get("expected_conversation_revision") or 0
                ),
            )
        raise ValueError(f"unknown conversation action: {name}")

    return operation


def create_message_resource(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create ordered message resource operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        if name not in {"list", "get"}:
            raise ValueError(f"unknown message resource operation: {name}")
        conversation = ConversationStore(_profile(payload)).get(
            str(payload.get("conversation_id") or "")
        )
        if conversation is None:
            return None
        messages = list(conversation.get("messages") or [])
        if name == "list":
            return {
                "conversation_id": conversation["id"],
                "conversation_revision": conversation["conversation_revision"],
                "messages": messages,
            }
        message_id = str(payload.get("message_id") or "")
        return next((item for item in messages if item.get("id") == message_id), None)

    return operation


def create_message_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create ordered message mutation operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = ConversationStore(_profile(payload))
        expected = int(payload.get("expected_conversation_revision") or 0)
        conversation_id = str(payload.get("conversation_id") or "")
        if name == "append":
            return store.append_message(
                conversation_id,
                _mapping(payload.get("message")),
                expected_conversation_revision=expected,
            )
        if name in {"update", "delete"}:
            return store.mutate_message(
                conversation_id,
                str(payload.get("message_id") or ""),
                expected_conversation_revision=expected,
                patch=_mapping(payload.get("patch")) if name == "update" else None,
                delete=name == "delete",
            )
        if name == "replace":
            messages = payload.get("messages")
            if not isinstance(messages, list):
                raise ValueError("ordered message list is required")
            return store.replace_messages(
                conversation_id,
                [item for item in messages if isinstance(item, Mapping)],
                expected_conversation_revision=expected,
            )
        raise ValueError(f"unknown message action: {name}")

    return operation


def create_migration_action(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create explicit conversation migration and rollback operations."""
    del client

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        store = ConversationStore(_profile(payload))
        if name == "migrate":
            conversations = payload.get("conversations")
            if not isinstance(conversations, list):
                raise ValueError("conversation migration source is invalid")
            return store.migrate(
                [item for item in conversations if isinstance(item, Mapping)],
                expected_source_hash=str(payload.get("expected_source_hash") or ""),
            )
        if name == "rollback":
            return store.rollback_migration(str(payload.get("migration_id") or ""))
        raise ValueError(f"unknown conversation migration action: {name}")

    return operation


def _conversation(value: Mapping[str, Any], *, allow_messages: bool) -> dict[str, Any]:
    conversation_id = _identifier(value.get("id") or uuid.uuid4())
    messages = value.get("messages") if allow_messages else []
    messages = messages if isinstance(messages, list) else []
    normalized_messages = [
        _message(item) for item in messages if isinstance(item, Mapping)
    ]
    message_ids = [item["id"] for item in normalized_messages]
    if len(message_ids) != len(set(message_ids)):
        raise ValueError("duplicate message ID in conversation")
    _normalize_message_links(normalized_messages)
    for sequence, item in enumerate(normalized_messages):
        item["sequence"] = sequence
    created_at = int(value.get("created_at") or _now_ms())
    return {
        "id": conversation_id,
        "title": str(value.get("title") or "New Conversation")[:500],
        "created_at": created_at,
        "updated_at": int(value.get("updated_at") or created_at),
        "conversation_revision": max(
            1, int(value.get("conversation_revision") or 1)
        ),
        "model_reference": str(
            value.get("model_reference") or value.get("model") or ""
        ),
        "system_prompt_id": value.get("system_prompt_id"),
        "agent_id": value.get("agent_id"),
        "tags": _safe(value.get("tags") or []),
        "is_starred": bool(value.get("is_starred", False)),
        "is_pinned": bool(value.get("is_pinned", False)),
        "pinned_at": value.get("pinned_at"),
        "pin_scope": str(value.get("pin_scope") or "global"),
        "is_archived": bool(value.get("is_archived", False)),
        "current_node_id": value.get("current_node_id"),
        "parent_conversation_id": value.get("parent_conversation_id"),
        "child_conversation_ids": _safe(value.get("child_conversation_ids") or []),
        "conversation_kind": str(value.get("conversation_kind") or "chat"),
        "group_id": value.get("group_id"),
        "metadata": _conversation_metadata(value.get("metadata") or {}),
        "messages": normalized_messages,
    }


def _message(value: Mapping[str, Any]) -> dict[str, Any]:
    created_at = int(value.get("created_at") or value.get("timestamp") or _now_ms())
    return {
        "id": _identifier(value.get("id") or uuid.uuid4()),
        "role": str(value.get("role") or "user"),
        "content": _safe(value.get("content") or ""),
        "parts": _safe(value.get("parts") or []),
        "status": str(value.get("status") or "complete"),
        "created_at": created_at,
        "updated_at": int(value.get("updated_at") or created_at),
        "sequence": max(0, int(value.get("sequence") or 0)),
        "metadata": _safe(value.get("metadata") or {}),
        "parent_id": value.get("parent_id"),
        "children_ids": _safe(value.get("children_ids") or []),
        "sequence_number": max(1, int(value.get("sequence_number") or 1)),
        "raw_text": str(value.get("raw_text") or ""),
        "finish_reason": value.get("finish_reason"),
        "usage": _safe(value.get("usage")),
        "widget": _safe(value.get("widget")),
        "events": _safe(value.get("events")),
        "tool_logs": _safe(value.get("tool_logs")),
    }


def _normalize_message_links(messages: list[dict[str, Any]]) -> None:
    """Derive children from validated parent IDs for one complete message set."""
    by_id = {item["id"]: item for item in messages}
    for item in messages:
        item["children_ids"] = []
    for item in messages:
        parent_id = item.get("parent_id")
        if parent_id is None:
            continue
        parent_id = _identifier(parent_id)
        if parent_id == item["id"]:
            raise ValueError("message cannot be its own parent")
        parent = by_id.get(parent_id)
        if parent is None:
            raise ValueError("message parent is unknown")
        item["parent_id"] = parent_id
        parent["children_ids"].append(item["id"])


def _conversation_metadata(value: Any) -> dict[str, Any]:
    """Drop executable legacy icon markup before owner persistence."""
    if not isinstance(value, Mapping):
        return {}
    metadata = dict(_safe(value))
    metadata.pop("icon_svg", None)
    return metadata


def _touch_conversation(conversation: dict[str, Any], now: int) -> None:
    conversation["updated_at"] = now
    conversation["conversation_revision"] = (
        int(conversation.get("conversation_revision") or 0) + 1
    )


def _relink_conversation_parent(
    conversations: dict[str, Any],
    conversation_id: str,
    prior_parent_id: Any,
    requested_parent_id: Any,
    now: int,
) -> None:
    if prior_parent_id in conversations:
        prior_parent = dict(conversations[prior_parent_id])
        prior_parent["child_conversation_ids"] = [
            child_id
            for child_id in prior_parent.get("child_conversation_ids") or []
            if child_id != conversation_id
        ]
        _touch_conversation(prior_parent, now)
        conversations[prior_parent_id] = prior_parent
    if requested_parent_id in conversations:
        requested_parent = dict(conversations[requested_parent_id])
        child_ids = list(
            requested_parent.get("child_conversation_ids") or []
        )
        if conversation_id not in child_ids:
            child_ids.append(conversation_id)
        requested_parent["child_conversation_ids"] = child_ids
        _touch_conversation(requested_parent, now)
        conversations[requested_parent_id] = requested_parent


def _identifier(value: Any) -> str:
    identifier = str(value or "").strip()
    if not _ID.fullmatch(identifier):
        raise ValueError("conversation or message identifier is invalid")
    return identifier


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("object payload is required")
    return value


def _profile(payload: Mapping[str, Any]) -> str:
    return str(payload.get("profile_id") or "default")


def _assert_store_revision(state: Mapping[str, Any], expected: int) -> None:
    if int(state.get("revision") or 0) != expected:
        raise ConversationConflict("conversation store revision is stale")


def _assert_conversation_revision(value: Mapping[str, Any], expected: int) -> None:
    if int(value.get("conversation_revision") or 0) != expected:
        raise ConversationConflict("conversation revision is stale")


def _safe(value: Any) -> Any:
    normalized = _utf8_safe(value)
    serialized = json.dumps(normalized, ensure_ascii=False, default=str)
    return _utf8_safe(json.loads(serialized))


def _copy(value: Any) -> Any:
    return _safe(value) if value is not None else None


def _hash(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now_ms() -> int:
    return int(time.time() * 1000)


def _utf8_safe(value: Any) -> Any:
    """Replace lone surrogates recursively while retaining valid Unicode."""
    if isinstance(value, str):
        return value.encode("utf-8", errors="replace").decode("utf-8")
    if isinstance(value, Mapping):
        return {
            _utf8_safe(key) if isinstance(key, str) else key: _utf8_safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_utf8_safe(item) for item in value]
    return value


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(
                _utf8_safe(value),
                handle,
                ensure_ascii=False,
                sort_keys=True,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
