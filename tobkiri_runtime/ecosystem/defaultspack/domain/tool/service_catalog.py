from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from domain.chat.tool_selection_schema import COMPUTER_TOOL_IDS
from domain.tool.schema_adapter import mapping_or_empty, tool_name_from_definition


SERVICE_ORDER = [
    "web",
    "github",
    "files",
    "coding",
    "terminal",
    "browser",
    "computer",
    "calendar",
    "gmail",
    "slack",
    "google_drive",
    "notion",
    "memory",
    "artifacts",
    "mcp",
    "system",
    "other",
]

SERVICE_LABELS: dict[str, str] = {
    "web": "Web検索",
    "github": "GitHub",
    "files": "Files",
    "coding": "Coding",
    "terminal": "Terminal",
    "browser": "Browser",
    "computer": "PC操作",
    "calendar": "Calendar",
    "gmail": "Gmail",
    "slack": "Slack",
    "google_drive": "Google Drive",
    "notion": "Notion",
    "memory": "Memory",
    "artifacts": "Artifacts",
    "mcp": "MCP",
    "system": "System",
    "other": "Other",
}

SERVICE_SUMMARIES: dict[str, str] = {
    "web": "Web、検索、オンライン情報を扱います",
    "github": "リポジトリ、Issue、Pull Requestを扱います",
    "files": "ローカルファイルやドキュメントを扱います",
    "coding": "コード編集、ビルド、開発作業を扱います",
    "terminal": "コマンド実行やジョブ操作を扱います",
    "browser": "ブラウザ、ページ、ダウンロードを扱います",
    "computer": "画面上のアプリやPC操作を扱います",
    "calendar": "予定やカレンダーを扱います",
    "gmail": "メール検索や下書きを扱います",
    "slack": "Slackメッセージを扱います",
    "google_drive": "Google Drive、Docs、Sheets、Slidesを扱います",
    "notion": "Notionページやデータベースを扱います",
    "memory": "記憶、知識、会話コンテキストを扱います",
    "artifacts": "成果物ファイルとプレビューを扱います",
    "mcp": "MCP接続と外部サーバーToolを扱います",
    "system": "Rumi内部のシステム機能を扱います",
    "other": "その他の機能を扱います",
}

ACTION_ORDER = ["read", "search", "create", "update", "send", "execute", "computer", "delete"]
ACTION_RANK = {name: index for index, name in enumerate(ACTION_ORDER)}


@dataclass
class ToolService:
    service_id: str
    label: str
    summary: str
    tools: list[dict[str, Any]] = field(default_factory=list)

    def compact(self) -> dict[str, Any]:
        permissions = _summarize_actions(self.tools)
        connection_status = _combined_connection_status(self.tools)
        return {
            "service_id": self.service_id,
            "label": self.label,
            "summary": self.summary,
            "connection_status": connection_status,
            "tool_count": len(self.tools),
            "action_classes": permissions,
        }


class ToolServiceCatalog:
    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self._tools = [tool for tool in (tools or []) if isinstance(tool, dict)]
        self._records = [self.compact_record(tool) for tool in self._tools]
        self._by_tool_id = {record["tool_id"]: record for record in self._records}

    def compact_records(self) -> list[dict[str, Any]]:
        return list(self._records)

    def service_for_tool(self, tool: dict[str, Any] | str) -> dict[str, Any]:
        tool_id = tool if isinstance(tool, str) else _tool_id(tool)
        return self._by_tool_id.get(str(tool_id), self.compact_record(tool if isinstance(tool, dict) else {"tool_id": tool_id}))

    def service_ids(self) -> list[str]:
        return sorted({record["service_id"] for record in self._records}, key=_service_sort_key)

    def services(self) -> list[dict[str, Any]]:
        grouped: dict[str, ToolService] = {}
        for tool, record in zip(self._tools, self._records):
            service_id = record["service_id"]
            if service_id not in grouped:
                grouped[service_id] = ToolService(
                    service_id=service_id,
                    label=record["service_label"],
                    summary=SERVICE_SUMMARIES.get(service_id, SERVICE_SUMMARIES["other"]),
                )
            grouped[service_id].tools.append({**tool, "_compact_record": record})
        return [grouped[key].compact() for key in sorted(grouped, key=_service_sort_key)]

    def tools_for_target(self, target_kind: str, target_id: str) -> list[dict[str, Any]]:
        target_kind = str(target_kind or "").strip().lower()
        target_id = str(target_id or "").strip()
        if not target_id:
            return []
        if target_kind == "service":
            return [
                tool
                for tool in self._tools
                if self.compact_record(tool)["service_id"] == target_id
            ]
        return [tool for tool in self._tools if _tool_id(tool) == target_id]

    @staticmethod
    def compact_record(tool: dict[str, Any]) -> dict[str, Any]:
        tool_id = _tool_id(tool)
        service_id = infer_service_id(tool)
        action_class = infer_action_class(tool)
        metadata = mapping_or_empty(tool.get("metadata"))
        return {
            "tool_id": tool_id,
            "service_id": service_id,
            "service_label": SERVICE_LABELS.get(service_id, SERVICE_LABELS["other"]),
            "name": str(tool.get("display_name") or tool.get("name") or tool_id).strip() or tool_id,
            "summary": str(tool.get("summary") or tool.get("description") or "").strip(),
            "action_class": action_class,
            "risk": str(tool.get("risk") or metadata.get("risk") or "low").strip().lower() or "low",
            "requires_explicit_intent": requires_explicit_intent(tool),
            "connection_status": infer_connection_status(tool),
            "minimum_permission": "confirm" if minimum_requires_confirm(tool) else "auto",
            "tags": _string_list(tool.get("tags")) + _string_list(metadata.get("tags")),
        }


def infer_service_id(tool: dict[str, Any]) -> str:
    tool_id = _tool_id(tool).lower()
    name = str(tool.get("name") or tool.get("display_name") or "").lower()
    metadata = mapping_or_empty(tool.get("metadata"))
    category = str(tool.get("category") or metadata.get("category") or "").lower()
    ui = mapping_or_empty(tool.get("ui"))
    explicit = str(metadata.get("service_id") or ui.get("service_id") or "").strip().lower()
    if explicit:
        return explicit if explicit in SERVICE_LABELS else "other"
    mcp_name = str(metadata.get("server_id") or metadata.get("mcp_server_id") or "").strip()
    if tool_id.startswith("mcp__") or mcp_name:
        return "mcp"
    haystack = " ".join([tool_id, name, category])
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("github", ("github", "pull_request", "pr_", "issue")),
        ("gmail", ("gmail", "email", "mail")),
        ("slack", ("slack",)),
        ("google_drive", ("google_drive", "drive", "slides", "sheet", "doc_")),
        ("calendar", ("calendar",)),
        ("notion", ("notion",)),
        ("computer", ("computer_use", "browser_computer", "screen", "mouse", "keyboard")),
        ("browser", ("browser", "html_preview", "webapp_preview")),
        ("terminal", ("terminal", "sandbox_exec", "python_exec", "node_exec", "command", "shell")),
        ("coding", ("coding", "workspace", "git_", "webapp_build", "webapp_lint", "project_scaffold", "package_install")),
        ("files", ("file", "pdf", "doc", "ocr", "audio_transcribe", "image_convert", "image_resize")),
        ("artifacts", ("artifact", "export", "zip", "preview")),
        ("memory", ("memory", "knowledge", "source_rank", "source_extract")),
        ("web", ("web_search", "reddit", "research", "source_extract", "wide_research")),
        ("system", ("workflow", "job_", "tts_generate", "image_generate", "tool_search")),
    )
    for service_id, tokens in rules:
        if any(token in haystack for token in tokens):
            return service_id
    return "other"


def infer_action_class(tool: dict[str, Any]) -> str:
    tool_id = _tool_id(tool).lower()
    metadata = mapping_or_empty(tool.get("metadata"))
    raw = str(tool.get("action_class") or metadata.get("action_class") or tool.get("action_type") or metadata.get("action_type") or "").strip().lower()
    if raw in ACTION_RANK:
        return raw
    haystack = " ".join([
        tool_id,
        str(tool.get("name") or "").lower(),
        str(tool.get("summary") or "").lower(),
        " ".join(_string_list(tool.get("tags"))).lower(),
    ])
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("delete", ("delete", "remove", "cancel", "削除")),
        ("computer", ("computer", "browser_computer", "mouse", "keyboard", "click")),
        ("execute", ("exec", "terminal", "shell", "python", "node", "workflow_run", "job_create")),
        ("send", ("send", "push", "publish", "external_send", "slack_send", "line_push", "discord_send", "gmail_draft")),
        ("update", ("update", "patch", "write", "edit", "persist", "retry", "resume")),
        ("create", ("create", "generate", "scaffold", "define")),
        ("search", ("search", "rank", "query", "find")),
        ("read", ("read", "list", "status", "export", "extract", "preview")),
    )
    for action, tokens in rules:
        if any(token in haystack for token in tokens):
            return action
    if bool(tool.get("write_action") or metadata.get("write_action")):
        return "update"
    return "read"


def infer_connection_status(tool: dict[str, Any]) -> str:
    if tool.get("enabled") is False:
        return "unavailable"
    metadata = mapping_or_empty(tool.get("metadata"))
    availability = tool.get("availability") if isinstance(tool.get("availability"), dict) else metadata.get("availability")
    if isinstance(availability, dict):
        status = str(availability.get("status") or "").strip().lower()
        if status in {"connected", "available", "ok", "ready"}:
            return "connected"
        if status in {"missing_api_key", "setup_required", "not_configured"}:
            return "setup_required"
        if status in {"error", "failed"}:
            return "error"
        if status in {"unavailable", "disabled"}:
            return "unavailable"
        if status:
            return "unavailable"
    if metadata.get("legacy_compat_unexecutable"):
        return "unavailable"
    # Registered local/bundled Tools need no external connection. Explicit but
    # unrecognized availability values above fail closed as unavailable.
    return "connected"


def requires_explicit_intent(tool: dict[str, Any]) -> bool:
    tool_id = _tool_id(tool)
    metadata = mapping_or_empty(tool.get("metadata"))
    if metadata.get("requires_explicit_intent") is True or tool.get("requires_explicit_intent") is True:
        return True
    return tool_id in COMPUTER_TOOL_IDS or infer_action_class(tool) in {"computer"}


def minimum_requires_confirm(tool: dict[str, Any]) -> bool:
    metadata = mapping_or_empty(tool.get("metadata"))
    if bool(tool.get("requires_approval") or metadata.get("requires_approval")):
        return True
    if str(tool.get("risk") or metadata.get("risk") or "").strip().lower() in {"high"}:
        return True
    return infer_action_class(tool) in {"create", "update", "send", "execute", "computer", "delete"}


def more_restrictive_permission(left: str, right: str) -> str:
    rank = {"auto": 0, "confirm": 1, "block": 2}
    left = left if left in rank else "auto"
    right = right if right in rank else "auto"
    return left if rank[left] >= rank[right] else right


def _tool_id(tool: dict[str, Any]) -> str:
    return str(tool.get("tool_id") or tool_name_from_definition(tool) or tool.get("name") or "").strip()


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [part.strip() for part in re.split(r"[,\\s]+", value) if part.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item or "").strip()]
    return []


def _service_sort_key(service_id: str) -> tuple[int, str]:
    try:
        return (SERVICE_ORDER.index(service_id), service_id)
    except ValueError:
        return (len(SERVICE_ORDER), service_id)


def _summarize_actions(tools: list[dict[str, Any]]) -> list[str]:
    actions = {infer_action_class(tool) for tool in tools}
    return sorted(actions, key=lambda action: ACTION_RANK.get(action, 99))


def _combined_connection_status(tools: list[dict[str, Any]]) -> str:
    statuses = [infer_connection_status(tool) for tool in tools]
    if any(status == "connected" for status in statuses):
        return "connected"
    if any(status == "setup_required" for status in statuses):
        return "setup_required"
    if any(status == "error" for status in statuses):
        return "error"
    return "unavailable"
