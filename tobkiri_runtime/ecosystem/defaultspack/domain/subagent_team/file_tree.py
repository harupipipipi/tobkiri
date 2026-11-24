from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from domain.company.models import DEFAULT_CHANNEL_ID
from domain.company.runtime_store import CompanyRuntimeStore
from domain.company.store import CompanyStore
from domain.coding.file_ops import FileOps
from domain.coding.git_ops import GitOps
from domain.coding.workspace_policy import WorkspaceTrustRequired, require_registered_trusted_workspace, require_trusted_workspace
from domain.coding.workspace_resolver import WorkspaceResolver

from .models import team_metadata
from .service import SubagentTeamService, is_denial


class ChannelHistoryAccessError(PermissionError):
    code = "CHANNEL_HISTORY_FORBIDDEN"


def build_file_tree(input_data: dict[str, Any] | None = None, context: dict[str, Any] | None = None) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    resolution = _resolve_trusted_workspace(data, context or {}, operation="read subagent file tree")
    directory = str(data.get("directory") or ".")
    recursive = bool(data.get("recursive", True))
    limit = data.get("limit", 400)
    if not isinstance(limit, int) or limit < 1:
        limit = 400
    files = FileOps(resolution.root_path).list_files(directory, recursive=recursive)
    clipped = len(files) > limit
    files = files[:limit]
    git_status = None
    git_error = None
    if data.get("include_git", True):
        try:
            git_status = GitOps(resolution.root_path).status()
        except Exception as exc:
            git_error = str(exc)
    workspace_hash = hashlib.sha256(str(resolution.root_path or "").encode("utf-8")).hexdigest()[:16]
    payload = {
        "workspace_id": resolution.workspace_id or f"ws_{workspace_hash}",
        "workspace_hash": workspace_hash,
        "workspace_root": f"workspace:{workspace_hash}",
        "root": ".",
        "directory": directory,
        "recursive": recursive,
        "files": files,
        "total_returned": len(files),
        "clipped": clipped,
        "status": {
            "git": git_status,
            "git_error": git_error,
            "counts": _counts(files, git_status),
        },
        "policy": {
            "read_only": True,
            "workspace_source": resolution.source,
            "trusted": resolution.trusted,
        },
    }
    company_id = str(data.get("company_id") or "").strip()
    if company_id:
        payload["team_workspace"] = _virtual_team_workspace_tree(company_id)
    return payload


def open_file_tree_node(
    input_data: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    *,
    settings_owner: Any = None,
) -> dict[str, Any]:
    data = input_data if isinstance(input_data, dict) else {}
    node_kind, node_id = _node_kind_and_id(data)
    if node_kind in {"history", "channel"}:
        return _open_history_node(
            data,
            context or {},
            node_kind=node_kind,
            node_id=node_id,
            settings_owner=settings_owner,
        )
    return _open_file_node(data, context or {}, node_id=node_id)


def _open_file_node(data: dict[str, Any], context: dict[str, Any], *, node_id: str) -> dict[str, Any]:
    resolution = _resolve_trusted_workspace(data, context, operation="open subagent file tree node")
    ops = FileOps(resolution.root_path)
    path = str(data.get("path") or data.get("file_path") or node_id or ".").strip() or "."
    root_hash = hashlib.sha256(str(resolution.root_path or "").encode("utf-8")).hexdigest()[:16]
    workspace_ref = f"workspace:{root_hash}"
    try:
        entries = ops.list_files(path, recursive=False)
        return {
            "kind": "directory",
            "workspace_id": resolution.workspace_id or f"ws_{root_hash}",
            "workspace_root": workspace_ref,
            "path": _safe_rel(path),
            "entries": entries,
            "total_returned": len(entries),
            "policy": {"read_only": True, "workspace_source": resolution.source, "trusted": resolution.trusted},
        }
    except NotADirectoryError:
        pass
    start_line = data.get("start_line")
    end_line = data.get("end_line")
    line_window = ops.read_file_lines(path, start_line=start_line, end_line=end_line)
    content = str(line_window.get("content") or "")
    content = content.replace(str(Path(resolution.root_path).resolve()), workspace_ref)
    max_chars = data.get("max_chars", 20000)
    try:
        max_chars = max(1, int(max_chars))
    except (TypeError, ValueError):
        max_chars = 20000
    clipped = len(content) > max_chars
    preview = content[:max_chars]
    if clipped and max_chars > 3:
        preview = preview[: max_chars - 3].rstrip() + "..."
    return {
        "kind": "file",
        "workspace_id": resolution.workspace_id or f"ws_{root_hash}",
        "workspace_root": workspace_ref,
        "path": _safe_rel(path),
        "name": Path(path).name or ".",
        "content": preview,
        "preview": preview,
        "start_line": line_window.get("start_line"),
        "end_line": line_window.get("end_line"),
        "total_lines": line_window.get("total_lines"),
        "truncated": bool(line_window.get("truncated") or clipped),
        "policy": {"read_only": True, "workspace_source": resolution.source, "trusted": resolution.trusted},
    }


def _open_history_node(
    data: dict[str, Any],
    context: dict[str, Any],
    *,
    node_kind: str,
    node_id: str,
    settings_owner: Any = None,
) -> dict[str, Any]:
    company_id = str(data.get("company_id") or "").strip()
    if not company_id:
        raise ValueError("company_id is required for history/channel nodes")
    if CompanyStore().get_company(company_id) is None:
        raise ValueError("company not found: " + company_id)
    channel_id = str(data.get("channel_id") or node_id or DEFAULT_CHANNEL_ID).strip() or DEFAULT_CHANNEL_ID
    if channel_id.startswith("channel:"):
        channel_id = channel_id.split(":", 1)[1]
    if channel_id.startswith("history:"):
        channel_id = channel_id.split(":", 1)[1] or DEFAULT_CHANNEL_ID
    access = SubagentTeamService(settings_owner=settings_owner).authorize_channel_read(
        company_id,
        channel_id,
        context=context,
    )
    if is_denial(access):
        raise ChannelHistoryAccessError(str(access.get("message") or "channel history access denied"))
    limit = data.get("limit", 50)
    try:
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        limit = 50
    runtime = CompanyRuntimeStore()
    messages, message_total = runtime.list_messages(company_id, channel_id=channel_id, limit=limit)
    tasks, task_total = runtime.list_tasks(company_id, limit=max(limit, 100))
    channel_tasks = [task for task in tasks if str(task.get("channel_id") or "") == channel_id][:limit]
    runs = runtime.list_run_links(company_id, limit=limit)
    return {
        "kind": node_kind,
        "company_id": company_id,
        "channel_id": channel_id,
        "messages": [_public_history_message(message) for message in messages],
        "message_total": message_total,
        "history": {
            "tasks": channel_tasks,
            "task_total": task_total,
            "runs": runs,
        },
        "policy": {"read_only": True},
    }


def _resolve_trusted_workspace(data: dict[str, Any], context: dict[str, Any], *, operation: str):
    resolution = WorkspaceResolver().resolve(data, context, allow_cwd_fallback=True)
    if _allows_unregistered_workspace_root(context):
        if resolution.uses_workspace_id:
            require_trusted_workspace(resolution, operation=operation)
        return resolution
    if not resolution.uses_workspace_id:
        raise WorkspaceTrustRequired("registered trusted workspace_id required for " + operation)
    return require_registered_trusted_workspace(resolution, operation=operation)


def _allows_unregistered_workspace_root(context: dict[str, Any]) -> bool:
    return bool(
        context.get("trusted_internal")
        or context.get("_trusted_internal")
        or context.get("allow_unregistered_workspace_root")
    )


def _counts(files: list[dict[str, Any]], git_status: dict[str, Any] | None) -> dict[str, int]:
    counts = {
        "files": sum(1 for item in files if not item.get("is_dir")),
        "directories": sum(1 for item in files if item.get("is_dir")),
        "staged": 0,
        "modified": 0,
        "untracked": 0,
    }
    if isinstance(git_status, dict):
        counts["staged"] = len(git_status.get("staged") or [])
        counts["modified"] = len(git_status.get("modified") or [])
        counts["untracked"] = len(git_status.get("untracked") or [])
    return counts


def _node_kind_and_id(data: dict[str, Any]) -> tuple[str, str]:
    node_id = str(data.get("node_id") or data.get("id") or data.get("path") or data.get("channel_id") or "").strip()
    node_kind = str(data.get("node_type") or data.get("kind") or "").strip().lower()
    if not node_kind:
        if node_id.startswith("history:"):
            node_kind = "history"
        elif node_id.startswith("channel:"):
            node_kind = "channel"
        else:
            node_kind = "file"
    return node_kind, node_id


def _safe_rel(path: str) -> str:
    clean = str(path or ".").replace("\\", "/").strip()
    if not clean or clean == ".":
        return "."
    return clean.lstrip("/")


def _public_history_message(message: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(message.get("message_id") or message.get("id") or ""),
        "channel_id": str(message.get("channel_id") or ""),
        "thread_id": message.get("thread_id"),
        "sender_id": str(message.get("sender_id") or ""),
        "content": str(message.get("content") or ""),
        "mentions": list(message.get("mentions") or []),
        "task_ids": list(message.get("task_ids") or []),
        "created_at": message.get("created_at"),
    }


def _virtual_team_workspace_tree(company_id: str) -> dict[str, Any]:
    store = CompanyStore()
    if store.get_company(company_id) is None:
        return {"company_id": company_id, "nodes": [], "missing": True}
    channels = store.list_channels(company_id) or []
    agents = store.list_agents(company_id) or []
    runtime = CompanyRuntimeStore()
    runs = runtime.list_run_links(company_id, limit=500)
    root = _folder("team-workspace", "team-workspace", "Team Workspace")
    channels_root = _folder("channels", "team-workspace/channels", "channels")
    for channel in channels:
        channel_id = str(channel.get("id") or channel.get("channel_id") or "")
        if not channel_id:
            continue
        channel_path = "team-workspace/channels/" + channel_id
        channel_node = _folder("channel:" + channel_id, channel_path, channel_id)
        channel_node["children"] = [
            _file("conversation:" + channel_id, channel_path + "/conversation.md", "conversation.md"),
            _file("messages:" + channel_id, channel_path + "/messages.jsonl", "messages.jsonl"),
            _folder("tasks:" + channel_id, channel_path + "/tasks", "tasks"),
            _folder("approvals:" + channel_id, channel_path + "/approvals", "approvals"),
            _folder("artifacts:" + channel_id, channel_path + "/artifacts", "artifacts"),
        ]
        channels_root.setdefault("children", []).append(channel_node)
    agents_root = _folder("agents", "team-workspace/agents", "agents")
    for agent in agents:
        team = team_metadata(agent)
        short_id = str(team.get("short_id") or agent.get("short_id") or agent.get("agent_id") or "")
        if not short_id:
            continue
        agent_path = "team-workspace/agents/" + short_id
        agent_runs = [run for run in runs if str(run.get("agent_id") or "") == str(agent.get("agent_id") or agent.get("id") or "")]
        agents_root.setdefault("children", []).append(
            {
                **_folder("agent:" + short_id, agent_path, short_id),
                "agent_id": str(agent.get("agent_id") or agent.get("id") or ""),
                "children": [
                    {
                        **_folder("agent-runs:" + short_id, agent_path + "/runs", "runs"),
                        "run_count": len(agent_runs),
                    },
                    _folder("agent-memory:" + short_id, agent_path + "/memory_notes", "memory_notes"),
                ],
            }
        )
    dms_root = _folder("dms", "team-workspace/dms", "dms")
    for channel in channels:
        metadata = channel.get("metadata") if isinstance(channel.get("metadata"), dict) else {}
        if str(channel.get("visibility") or "") != "dm" and not metadata.get("dm"):
            continue
        members = [str(member) for member in channel.get("members", [])]
        label = "-".join(members) or str(channel.get("id") or "dm")
        dms_root.setdefault("children", []).append(
            _folder("dm:" + str(channel.get("id") or label), "team-workspace/dms/" + label, label)
        )
    root["children"] = [channels_root, agents_root, dms_root]
    return {
        "company_id": company_id,
        "root": root,
        "paths": [
            "channels/{channel}/conversation.md",
            "channels/{channel}/messages.jsonl",
            "channels/{channel}/tasks",
            "channels/{channel}/approvals",
            "channels/{channel}/artifacts",
            "agents/{short_id}/runs",
            "dms/{short_id}",
        ],
    }


def _folder(node_id: str, path: str, name: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "folder", "path": path, "name": name, "children": []}


def _file(node_id: str, path: str, name: str) -> dict[str, Any]:
    return {"id": node_id, "kind": "file", "path": path, "name": name}
