"""Deprecated ChatStore facade over global conversation/message owners."""

from __future__ import annotations

import base64
import copy
import json
import re
import time
import uuid
import warnings
from pathlib import Path
from typing import Any, Mapping

from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import (
    captured_profile_id,
    invoke_global_contract,
)
from core_runtime.paths import USER_DATA_DIR
from domain.chat.attachments.store import upsert_attachment_records
from domain.chat.icon_matcher import match_icon

CONVERSATION = "rumi.resource.conversation.v1"
CONVERSATION_MANAGE = "rumi.action.conversation.manage.v1"
MESSAGE = "rumi.resource.message.v1"
MESSAGE_MANAGE = "rumi.action.message.manage.v1"
MAX_APPEND_RETRIES = 32


class ChatStore:
    """Finite compatibility facade with no canonical conversation storage."""

    def __init__(self) -> None:
        warnings.warn(
            "domain.chat.store.ChatStore is a Wave 7 compatibility facade",
            DeprecationWarning,
            stacklevel=2,
        )

    @property
    def conversations(self) -> dict[str, dict[str, Any]]:
        """Return a nonmutable projection keyed by conversation ID."""
        return {item["id"]: item for item in self._snapshot()["conversations"]}

    def create_conversation(
        self,
        model: str | None = None,
        system_prompt_id: str | None = None,
        agent_id: str | None = None,
        tags: list[str] | None = None,
        parent_conversation_id: str | None = None,
        conversation_kind: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        group_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one conversation through its selected owner."""
        conversation_id = str(uuid.uuid4())
        title = str((metadata or {}).get("title") or "New Conversation")
        record = {
            "id": conversation_id,
            "title": title,
            "model_reference": str(model or ""),
            "system_prompt_id": system_prompt_id,
            "agent_id": agent_id,
            "tags": list(tags or []),
            "parent_conversation_id": parent_conversation_id,
            "conversation_kind": conversation_kind
            or ("subagent" if parent_conversation_id else "chat"),
            "metadata": _set_metadata_icon(
                metadata,
                title=title,
                conversation_id=conversation_id,
            ),
            "group_id": group_id,
        }
        result = _invoke(
            CONVERSATION_MANAGE,
            "create",
            {"conversation": record, "expected_revision": self._store_revision()},
        )
        return _legacy_conversation(result["conversation"])

    def get_conversation(self, conversation_id: str) -> dict[str, Any] | None:
        """Get one complete conversation from its owner."""
        value = _invoke(
            CONVERSATION, "get", {"conversation_id": str(conversation_id or "")}
        )
        return _legacy_conversation(value) if isinstance(value, Mapping) else None

    def get_conversation_window(
        self,
        conversation_id: str,
        message_limit: int | None = None,
        message_offset: int | None = None,
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Return one chronological message window from the owner snapshot."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None, None
        messages = list(conversation.get("messages") or [])
        total = len(messages)
        if message_limit is None:
            start, end, resolved = 0, total, total
        else:
            resolved = max(0, int(message_limit))
            start = (
                max(0, total - resolved)
                if message_offset is None
                else max(0, min(total, int(message_offset)))
            )
            end = min(total, start + resolved)
        conversation["messages"] = messages[start:end]
        return conversation, {
            "offset": start,
            "limit": resolved,
            "returned": end - start,
            "total": total,
            "has_more_before": start > 0,
            "has_more_after": end < total,
            "order": "chronological",
        }

    def list_conversations(
        self,
        limit: int = 50,
        offset: int = 0,
        tag: str | None = None,
        tags: list[str] | None = None,
        is_starred: bool | None = None,
        is_pinned: bool | None = None,
        is_archived: bool | None = None,
        company_id: str | None = None,
        workspace_id: str | None = None,
        conversation_kind: str | None = None,
        group_id: str | None = None,
        query: str | None = None,
        include_messages: bool = False,
    ) -> tuple[list[dict[str, Any]], int]:
        """Filter a nonauthoritative list projection from one owner snapshot."""
        required_tags = {str(item) for item in tags or []}
        if tag is not None:
            required_tags.add(str(tag))
        query = str(query or "").casefold()
        result = []
        for raw in self._snapshot()["conversations"]:
            item = _legacy_conversation(raw)
            metadata = item.get("metadata") or {}
            if metadata.get("hidden") is True:
                continue
            if required_tags.difference(item.get("tags") or []):
                continue
            if is_starred is not None and item.get("is_starred") != is_starred:
                continue
            if is_pinned is not None and item.get("is_pinned") != is_pinned:
                continue
            if is_archived is not None and item.get("is_archived") != is_archived:
                continue
            if conversation_kind is not None and item.get("conversation_kind") != conversation_kind:
                continue
            if group_id is not None and str(item.get("group_id") or "") != str(group_id):
                continue
            if company_id is not None and str(metadata.get("company_id") or "") != str(company_id):
                continue
            if workspace_id is not None and str(metadata.get("workspace_id") or "") != str(workspace_id):
                continue
            searchable = " ".join(
                [
                    item.get("title") or "",
                    *(
                        str(value)
                        for value in metadata.values()
                        if isinstance(value, (str, int, float, bool))
                    ),
                    *(
                        _message_text(message)
                        for message in (item.get("messages") or [] if include_messages else [])
                    ),
                ]
            ).casefold()
            if query and query not in searchable:
                continue
            result.append(item if include_messages else _summary(item))
        result.sort(key=lambda item: int(item.get("updated_at") or 0), reverse=True)
        return result[offset : offset + limit], len(result)

    def update_conversation(
        self, conversation_id: str, updates: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Update owner-supported metadata at an exact conversation revision."""
        current = self.get_conversation(conversation_id)
        if current is None:
            return None
        patch = dict(updates)
        if "model" in patch:
            patch["model_reference"] = patch.pop("model")
        supported = {
            "title", "model_reference", "system_prompt_id", "agent_id", "tags",
            "is_starred", "is_pinned", "pinned_at", "pin_scope", "is_archived",
            "current_node_id", "parent_conversation_id", "child_conversation_ids",
            "conversation_kind", "group_id", "metadata",
        }
        extras = {key: value for key, value in patch.items() if key not in supported}
        if extras:
            metadata = dict(
                patch.get("metadata")
                if isinstance(patch.get("metadata"), Mapping)
                else current.get("metadata") or {}
            )
            metadata.update(extras)
            patch["metadata"] = metadata
        patch["metadata"] = _set_metadata_icon(
            patch.get("metadata", current.get("metadata")),
            title=str(patch.get("title", current.get("title")) or ""),
            conversation_id=conversation_id,
        )
        result = _invoke(
            CONVERSATION_MANAGE,
            "update",
            {
                "conversation_id": conversation_id,
                "patch": patch,
                "expected_conversation_revision": current["conversation_revision"],
            },
        )
        return _legacy_conversation(result["conversation"])

    def delete_conversation(self, conversation_id: str) -> bool:
        """Delete one conversation and its messages through the owner."""
        current = self.get_conversation(conversation_id)
        if current is None:
            return False
        _invoke(
            CONVERSATION_MANAGE,
            "delete",
            {
                "conversation_id": conversation_id,
                "expected_conversation_revision": current["conversation_revision"],
            },
        )
        return True

    def add_message(
        self, conversation_id: str, message_dict: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        """Append one message in the owner transaction."""
        last_conflict: Exception | None = None
        for _ in range(MAX_APPEND_RETRIES):
            conversation = self.get_conversation(conversation_id)
            if conversation is None or _read_only(conversation):
                return None
            message = _prepare_message(conversation_id, conversation, message_dict)
            try:
                result = _invoke(
                    MESSAGE_MANAGE,
                    "append",
                    {
                        "conversation_id": conversation_id,
                        "message": message,
                        "expected_conversation_revision": conversation[
                            "conversation_revision"
                        ],
                    },
                )
            except Exception as exc:
                if type(exc).__name__ != "ConversationConflict":
                    raise
                last_conflict = exc
                continue
            return _legacy_message(result["message"], conversation_id)
        if last_conflict is not None:
            raise last_conflict
        raise RuntimeError("message append did not complete")

    def get_message(
        self, conversation_id: str, message_id: str
    ) -> dict[str, Any] | None:
        """Get one message from the owner."""
        value = _invoke(
            MESSAGE,
            "get",
            {"conversation_id": conversation_id, "message_id": message_id},
        )
        return _legacy_message(value, conversation_id) if isinstance(value, Mapping) else None

    def update_message(
        self,
        conversation_id: str,
        message_id: str,
        updates: Mapping[str, Any],
    ) -> dict[str, Any] | None:
        """Update one message through the owner."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or _read_only(conversation):
            return None
        current = self.get_message(conversation_id, message_id)
        if current is None:
            return None
        patch = dict(updates)
        supported = {
            "content", "parts", "metadata", "status", "parent_id",
            "children_ids", "sequence_number", "raw_text", "finish_reason",
            "usage", "widget", "events", "tool_logs",
        }
        extras = {key: value for key, value in patch.items() if key not in supported}
        if extras:
            metadata = dict(
                patch.get("metadata")
                if isinstance(patch.get("metadata"), Mapping)
                else current.get("metadata") or {}
            )
            metadata.update(extras)
            patch["metadata"] = metadata
        result = _invoke(
            MESSAGE_MANAGE,
            "update",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "patch": patch,
                "expected_conversation_revision": conversation["conversation_revision"],
            },
        )
        return _legacy_message(result["message"], conversation_id)

    def delete_message(self, conversation_id: str, message_id: str) -> bool:
        """Delete one message through the owner."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or _read_only(conversation):
            return False
        if self.get_message(conversation_id, message_id) is None:
            return False
        _invoke(
            MESSAGE_MANAGE,
            "delete",
            {
                "conversation_id": conversation_id,
                "message_id": message_id,
                "expected_conversation_revision": conversation["conversation_revision"],
            },
        )
        return True

    def get_messages_range(
        self, conversation_id: str, start_message_id: str, end_message_id: str
    ) -> tuple[list[dict[str, Any]], int] | None:
        """Return an inclusive chronological message range."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        messages = list(conversation.get("messages") or [])
        ids = [item.get("id") for item in messages]
        if start_message_id not in ids or end_message_id not in ids:
            return None
        start, end = ids.index(start_message_id), ids.index(end_message_id)
        start, end = min(start, end), max(start, end)
        return copy.deepcopy(messages[start : end + 1]), start

    def delete_messages_bulk(self, conversation_id: str, message_ids: list[str]) -> int:
        """Delete multiple messages in one ordered owner transaction."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or _read_only(conversation):
            return 0
        delete_ids = set(message_ids)
        messages = []
        for item in conversation.get("messages") or []:
            if item.get("id") in delete_ids:
                continue
            item = dict(item)
            item["children_ids"] = [
                child for child in item.get("children_ids") or [] if child not in delete_ids
            ]
            if item.get("parent_id") in delete_ids:
                item["parent_id"] = None
            messages.append(item)
        deleted = len(conversation.get("messages") or []) - len(messages)
        self._replace_messages(conversation, messages)
        return deleted

    def insert_message_at(
        self,
        conversation_id: str,
        message_dict: Mapping[str, Any],
        position_index: int,
        parent_id: str | None = None,
        children_ids: list[str] | None = None,
    ) -> dict[str, Any] | None:
        """Insert and relink one message in a single owner transaction."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or _read_only(conversation):
            return None
        message = _prepare_message(conversation_id, conversation, message_dict)
        message["parent_id"] = parent_id
        message["children_ids"] = list(children_ids or [])
        messages = [dict(item) for item in conversation.get("messages") or []]
        for item in messages:
            if item.get("id") == parent_id:
                item["children_ids"] = list(item.get("children_ids") or []) + [message["id"]]
            if item.get("id") in message["children_ids"]:
                item["parent_id"] = message["id"]
        index = max(0, min(int(position_index), len(messages)))
        messages.insert(index, message)
        self._replace_messages(conversation, messages)
        return _legacy_message(message, conversation_id)

    def search(self, query: str, conversation_id: str | None = None) -> list[dict[str, Any]]:
        """Search message text in the single owner snapshot."""
        needle = str(query or "").casefold()
        values = (
            [self.get_conversation(conversation_id)]
            if conversation_id
            else [_legacy_conversation(item) for item in self._snapshot()["conversations"]]
        )
        return [
            copy.deepcopy(message)
            for conversation in values
            if conversation
            for message in conversation.get("messages") or []
            if needle in _message_text(message).casefold()
        ]

    def search_conversations(self, query: str, **options: Any) -> tuple[list[dict[str, Any]], int]:
        """Return finite exact-text legacy search results."""
        conversations, _ = self.list_conversations(
            limit=1_000_000,
            include_messages=True,
            is_starred=options.get("is_starred"),
            is_archived=options.get("is_archived"),
        )
        needle = str(query or "").casefold()
        requested_conversation_id = str(options.get("conversation_id") or "")
        requested_role = str(options.get("role") or "all").strip().casefold()
        date_filter = str(options.get("date_filter") or "all").strip().casefold()
        date_window_days = {"today": 1, "7d": 7, "30d": 30}.get(date_filter)
        cutoff = (
            int(time.time() * 1000) - date_window_days * 86_400_000
            if date_window_days is not None
            else None
        )
        results = []
        for item in conversations:
            if (
                requested_conversation_id
                and str(item.get("id") or "") != requested_conversation_id
            ):
                continue
            if cutoff is not None and int(item.get("updated_at") or 0) < cutoff:
                continue
            matches = []
            for message in item.get("messages") or []:
                if (
                    requested_role != "all"
                    and str(message.get("role") or "").strip().casefold()
                    != requested_role
                ):
                    continue
                if needle not in _message_text(message).casefold():
                    continue
                match = copy.deepcopy(message)
                match["exact"] = True
                matches.append(match)
            if needle not in str(item.get("title") or "").casefold() and not matches:
                continue
            results.append({
                "conversation_id": item["id"], "title": item.get("title"),
                "created_at": item.get("created_at"), "updated_at": item.get("updated_at"),
                "is_starred": bool(item.get("is_starred")),
                "is_archived": bool(item.get("is_archived")), "score": 1.0,
                "exact_score": 1.0, "semantic_score": 0.0,
                "match_count": len(matches), "matches": matches[:3],
            })
        offset = int(options.get("offset") or 0)
        limit = int(options.get("limit") or 20)
        return results[offset : offset + limit], len(results)

    def branch(self, conversation_id: str, message_id: str | None) -> dict[str, Any] | None:
        """Create a branch and copy its selected chain through owner operations."""
        source = self.get_conversation(conversation_id)
        if source is None:
            return None
        chain = self.get_message_chain(conversation_id, message_id) if message_id else []
        branch = self.create_conversation(
            model=source.get("model"), system_prompt_id=source.get("system_prompt_id"),
            agent_id=source.get("agent_id"), tags=list(source.get("tags") or []),
            parent_conversation_id=conversation_id,
        )
        self.update_conversation(branch["id"], {"title": f"{source.get('title')} (branch)"})
        id_map = {item["id"]: str(uuid.uuid4()) for item in chain}
        for item in chain:
            copied = dict(item)
            copied["id"] = id_map[item["id"]]
            copied["parent_id"] = id_map.get(item.get("parent_id"))
            copied["children_ids"] = [id_map[child] for child in item.get("children_ids") or [] if child in id_map]
            self.add_message(branch["id"], copied)
        return self.get_conversation(branch["id"])

    def export_conversation(self, conversation_id: str, fmt: str = "markdown") -> str | None:
        """Export the owner snapshot using existing pure exporters."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None:
            return None
        from domain.chat.exporter import export_json, export_markdown, export_text

        if str(fmt).lower() == "json":
            return export_json(conversation)
        if str(fmt).lower() in {"text", "txt"}:
            return export_text(conversation)
        return export_markdown(conversation)

    def get_message_chain(
        self, conversation_id: str, up_to_message_id: str | None
    ) -> list[dict[str, Any]]:
        """Resolve a parent-linked chain from one owner snapshot."""
        conversation = self.get_conversation(conversation_id)
        if conversation is None or up_to_message_id is None:
            return []
        messages = {item["id"]: item for item in conversation.get("messages") or []}
        chain = []
        current = up_to_message_id
        while current in messages:
            item = messages[current]
            chain.append(copy.deepcopy(item))
            current = item.get("parent_id")
        chain.reverse()
        return chain

    def conversation_dir(self, conversation_id: str) -> Path:
        """Return the compatibility artifact directory, not a data owner path."""
        return self._artifact_root() / str(conversation_id)

    def conversation_workspace_dir(self, conversation_id: str) -> Path:
        """Return the compatibility workspace directory."""
        return self.conversation_dir(conversation_id) / "workspace"

    def persist_attachments(self, conversation_id: str, attachments: Any) -> list[dict[str, Any]]:
        """Persist attachment payloads outside conversation owner state."""
        if not isinstance(attachments, list):
            return []
        root = self.conversation_workspace_dir(conversation_id) / "attachments"
        root.mkdir(parents=True, exist_ok=True)
        refs = []
        for index, attachment in enumerate(attachments):
            if not isinstance(attachment, Mapping) or any(
                attachment.get(key) for key in ("ephemeral", "do_not_persist", "no_persist")
            ):
                continue
            name = _safe_filename(str(attachment.get("name") or f"attachment-{index + 1}"))
            path = root / name
            data_url = attachment.get("dataUrl") or attachment.get("data_url")
            if isinstance(data_url, str) and data_url.startswith("data:") and "," in data_url:
                path.write_bytes(base64.b64decode(data_url.split(",", 1)[1]))
            elif isinstance(attachment.get("content"), str):
                path.write_text(str(attachment["content"]), encoding="utf-8")
            else:
                path.write_text(json.dumps(dict(attachment), default=str), encoding="utf-8")
            refs.append({
                "id": attachment.get("id"), "name": attachment.get("name") or name,
                "size": attachment.get("size"), "type": attachment.get("type"),
                "workspace_path": path.relative_to(self.conversation_dir(conversation_id)).as_posix(),
            })
        if refs:
            upsert_attachment_records(
                self.conversation_workspace_dir(conversation_id),
                [
                    item
                    for item in attachments
                    if isinstance(item, Mapping)
                    and not any(
                        item.get(key)
                        for key in ("ephemeral", "do_not_persist", "no_persist")
                    )
                ],
                refs,
            )
        return refs

    def _replace_messages(
        self, conversation: Mapping[str, Any], messages: list[Mapping[str, Any]]
    ) -> None:
        _invoke(
            MESSAGE_MANAGE,
            "replace",
            {
                "conversation_id": conversation["id"], "messages": messages,
                "expected_conversation_revision": conversation["conversation_revision"],
            },
        )

    def _snapshot(self) -> dict[str, Any]:
        return dict(_invoke(CONVERSATION, "list", {}))

    def _store_revision(self) -> int:
        return int(self._snapshot().get("revision") or 0)

    @staticmethod
    def _artifact_root() -> Path:
        session = get_container().get_or_none("v4_dispatch_session")
        if session is None:
            raise RuntimeError("global conversation owner is unavailable")
        profile_id = captured_profile_id(session)
        return Path(USER_DATA_DIR) / "compatibility" / "conversation_artifacts" / profile_id


def _invoke(contract_id: str, operation: str, payload: Mapping[str, Any]) -> Any:
    registry = get_container().get_or_none("v4_dispatch_session")
    if registry is None:
        raise RuntimeError("global conversation owner is unavailable")
    return invoke_global_contract(
        registry,
        contract_id,
        operation,
        {"profile_id": captured_profile_id(registry), **dict(payload)},
    )


def _legacy_conversation(value: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["model"] = result.get("model_reference") or ""
    result["messages"] = [
        _legacy_message(item, str(result.get("id") or ""))
        for item in result.get("messages") or []
    ]
    return result


def _legacy_message(value: Mapping[str, Any], conversation_id: str) -> dict[str, Any]:
    result = copy.deepcopy(dict(value))
    result["conversation_id"] = conversation_id
    if "sequence" in result:
        result["sequence_number"] = int(result.get("sequence") or 0) + 1
    else:
        result["sequence_number"] = int(result.get("sequence_number") or 1)
    return result


def _prepare_message(
    conversation_id: str,
    conversation: Mapping[str, Any],
    value: Mapping[str, Any],
) -> dict[str, Any]:
    message = copy.deepcopy(dict(value))
    message["id"] = str(message.get("id") or uuid.uuid4())
    message["conversation_id"] = conversation_id
    message.setdefault("parent_id", conversation.get("current_node_id"))
    message.setdefault("children_ids", [])
    message.setdefault("created_at", int(time.time() * 1000))
    message.setdefault("raw_text", _message_text(message))
    message.setdefault("sequence_number", len(conversation.get("messages") or []) + 1)
    return message


def _summary(conversation: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(conversation))
    messages = list(result.pop("messages", []))
    result["messages"] = []
    result["message_count"] = len(messages)
    result["last_message_preview"] = _message_text(messages[-1]) if messages else ""
    return result


def _message_text(message: Mapping[str, Any]) -> str:
    raw = message.get("raw_text")
    if raw:
        return str(raw)
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            str(item.get("text") or item.get("content") or "")
            if isinstance(item, Mapping) else str(item)
            for item in content
        )
    return str(content or "")


def _read_only(conversation: Mapping[str, Any]) -> bool:
    metadata = conversation.get("metadata")
    return isinstance(metadata, Mapping) and (
        metadata.get("read_only") is True
        or metadata.get("shared_read_only") is True
    )


def _set_metadata_icon(
    metadata: Any,
    *,
    title: str,
    conversation_id: str,
) -> dict[str, Any]:
    """Replace caller icon fields with one host-generated inert icon ID."""
    value = dict(metadata) if isinstance(metadata, Mapping) else {}
    value.pop("icon_svg", None)
    value.pop("icon_id", None)
    value["icon_id"] = match_icon(title, conversation_id)["icon_id"]
    return value


def _safe_filename(name: str) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip(".-")
    return value[:180] or "attachment"
