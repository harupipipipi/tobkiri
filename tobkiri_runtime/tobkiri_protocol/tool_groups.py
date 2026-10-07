"""Shared presentation grouping over tool data; no service discovery or grants."""

from typing import Any, Mapping

SERVICE_IDS = frozenset(['web', 'github', 'files', 'coding', 'terminal', 'browser', 'computer', 'calendar', 'gmail', 'slack', 'google_drive', 'notion', 'memory', 'artifacts', 'mcp', 'system', 'other'])

def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def infer_tool_service(tool: Mapping[str, Any]) -> str:
    """Classify public tool metadata consistently for selection and display."""
    tool_id = str(tool.get("tool_id") or tool.get("name") or "").lower()
    name = str(tool.get("name") or tool.get("display_name") or "").lower()
    metadata = _mapping(tool.get("metadata"))
    category = str(tool.get("category") or metadata.get("category") or "").lower()
    ui = _mapping(tool.get("ui"))
    explicit = str(metadata.get("service_id") or ui.get("service_id") or "").strip().lower()
    if explicit:
        return explicit if explicit in SERVICE_IDS else "other"
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

