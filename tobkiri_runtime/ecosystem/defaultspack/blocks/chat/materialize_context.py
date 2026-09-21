from __future__ import annotations

import re
from os import PathLike
from typing import Any

from blocks._common import error, ok
from core_runtime.di_container import get_container
from core_runtime.global_contract_dispatch import invoke_global_contract
from core_runtime.resolved_profile_scope import active_resolved_profile
from domain.artifact.workspace import ArtifactWorkspace
from domain.chat.store import ChatStore


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_FORMAT_ALIASES = {
    "text": ("text", ".txt", "text/plain"),
    "txt": ("text", ".txt", "text/plain"),
    "text/plain": ("text", ".txt", "text/plain"),
    "markdown": ("markdown", ".md", "text/markdown"),
    "md": ("markdown", ".md", "text/markdown"),
    "text/markdown": ("markdown", ".md", "text/markdown"),
    "text/x-markdown": ("markdown", ".md", "text/markdown"),
}


def run(input_data: dict[str, Any] | None, context: dict[str, Any] | None):
    payload = input_data if isinstance(input_data, dict) else {}
    conversation_id = str(payload.get("conversation_id") or "").strip()
    if not conversation_id:
        return error("conversation_id is required", "INVALID_INPUT")

    requested_format = str(payload.get("format") or "text").strip().lower().lstrip(".")
    format_spec = _FORMAT_ALIASES.get(requested_format)
    if format_spec is None:
        return error("format must be one of: text, txt, markdown, md", "INVALID_INPUT")
    export_format, extension, mime_type = format_spec

    store = ChatStore()
    conversation = store.get_conversation(conversation_id)
    if conversation is None:
        return error("Conversation not found", "NOT_FOUND")
    registry = get_container().get_or_none("v4_dispatch_session")
    plan = active_resolved_profile()
    if registry is None or plan is None:
        return error("Context runtime unavailable", "CONTEXT_UNAVAILABLE")
    materialized = invoke_global_contract(
        registry,
        "rumi.service.context.v1",
        "materialize",
        {
            "profile_id": plan.profile_id,
            "conversation_id": conversation_id,
            "conversation_revision": conversation["conversation_revision"],
            "query": str(payload.get("query") or ""),
            "system_items": [],
            "recall_limit": int(payload.get("recall_limit") or 8),
            "token_budget": int(payload.get("token_budget") or 131072),
        },
    )
    content = _context_text(materialized, export_format)

    workspace_context = dict(context or {})
    for key in ("artifact_root", "conversation_workspace_dir", "workspace_root"):
        value = workspace_context.get(key)
        if isinstance(value, PathLike):
            workspace_context[key] = str(value)
    if not any(
        isinstance(workspace_context.get(key), str) and workspace_context.get(key, "").strip()
        for key in ("artifact_root", "conversation_workspace_dir", "workspace_root")
    ):
        workspace_context["conversation_workspace_dir"] = str(store.conversation_workspace_dir(conversation_id))

    workspace = ArtifactWorkspace(workspace_context)
    relative_path = "context/" + _safe_filename(conversation_id) + extension
    target = workspace.resolve(relative_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(str(content), encoding="utf-8")

    path = workspace.relative(target)
    filename = target.name
    size = target.stat().st_size
    artifact = {
        "path": path,
        "filename": filename,
        "name": filename,
        "size": size,
        "format": export_format,
        "mime_type": mime_type,
    }
    return ok(
        {
            "path": path,
            "filename": filename,
            "name": filename,
            "size": size,
            "format": export_format,
            "mime_type": mime_type,
            "content_type": mime_type,
            "conversation_id": conversation_id,
            "artifacts": [artifact],
            "message": f"Materialized conversation context to {path}.",
        }
    )


def _safe_filename(value: str) -> str:
    safe = _SAFE_FILENAME_RE.sub("-", value.strip()).strip(".-")
    return safe[:100] or "conversation"


def _context_text(value: Any, export_format: str) -> str:
    if export_format == "text":
        lines = []
        for section in value.get("sections") or []:
            if not isinstance(section, dict):
                continue
            lines.append(f"[{section.get('kind') or 'context'}]")
            for item in section.get("items") or []:
                lines.append(str(item.get("content") if isinstance(item, dict) else item))
        return "\n".join(lines)
    lines = ["# Conversation context", ""]
    for section in value.get("sections") or []:
        if not isinstance(section, dict):
            continue
        kind = str(section.get("kind") or "context").strip().title()
        items = section.get("items") or []
        if kind.lower() == "conversation":
            for item in items:
                role = str(item.get("role") or "Context") if isinstance(item, dict) else "Context"
                lines.extend([f"### {role.title()}", _item_text(item), ""])
            continue
        if not items:
            continue
        lines.extend([f"## {kind}", ""])
        for item in items:
            lines.extend([_item_text(item), ""])
    return "\n".join(lines).rstrip() + "\n"


def _item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return str(item)
    content = item.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                parts.append(str(block.get("text") or block.get("content") or block))
            else:
                parts.append(str(block))
        return "\n".join(part for part in parts if part)
    if content not in (None, ""):
        return str(content)
    return str(item.get("text") or item.get("value") or item)


def materialized_audio_transcript_blocks(attachments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks: list[dict[str, Any]] = []
    for attachment in attachments:
        if not isinstance(attachment, dict):
            continue
        mime = str(attachment.get("type") or attachment.get("mime_type") or "").lower()
        if not mime.startswith("audio/"):
            continue
        transcript = materialized_audio_transcript(attachment)
        if not transcript:
            continue
        name = str(attachment.get("name") or "ambient-recording").strip()[:200] or "ambient-recording"
        blocks.append({"type": "text", "text": f"\n\n音声入力の文字起こし: {name}\n{transcript}"})
    return blocks


def materialized_audio_transcript(attachment: dict[str, Any]) -> str:
    for key in ("transcript", "transcription", "text_transcript"):
        value = attachment.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    metadata = attachment.get("metadata") if isinstance(attachment.get("metadata"), dict) else {}
    for key in ("transcript", "transcription", "text_transcript"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""
