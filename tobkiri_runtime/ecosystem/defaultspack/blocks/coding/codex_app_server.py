"""Codex-style coding backend scaffold.

This module intentionally is not an LLM provider. It models the safety boundary
for a future app-server coding session integration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4


class WorkspaceBoundaryError(ValueError):
    pass


class ServerApprovalRequiredError(PermissionError):
    pass


HIGH_RISK_ACTIONS = {
    "file.write",
    "file.patch",
    "file.delete",
    "terminal.exec",
    "terminal.stream",
    "git.commit",
    "git.push",
}


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def ensure_within_workspace(workspace_root: str | Path, target_path: str | Path | None = None) -> Path:
    root = _resolved(workspace_root)
    if target_path is None or target_path == "":
        target = root
    else:
        target = _resolved(target_path)
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise WorkspaceBoundaryError(f"path is outside workspace root: {target}") from exc
    return target


def server_approval_granted(context: dict[str, Any] | None, action_id: str) -> bool:
    context = context if isinstance(context, dict) else {}
    approvals = context.get("server_approvals")
    if isinstance(approvals, dict) and bool(approvals.get(action_id)):
        return True
    return bool(context.get("_server_side_approved") and context.get("_approved_action_id") == action_id)


def require_server_approval(
    action_id: str,
    *,
    context: dict[str, Any] | None = None,
    client_supplied_approved: bool | None = None,
) -> None:
    del client_supplied_approved
    if action_id in HIGH_RISK_ACTIONS and not server_approval_granted(context, action_id):
        raise ServerApprovalRequiredError(f"server-side approval required for {action_id}")


@dataclass
class CodingSession:
    session_id: str
    workspace_root: str
    profile: dict[str, Any] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)


class CodexAppServerBackend:
    backend_id = "codex-app-server"

    def create_session(self, workspace_root: str, profile: dict[str, Any] | None = None) -> CodingSession:
        root = ensure_within_workspace(workspace_root)
        return CodingSession(
            session_id=f"codex_{uuid4()}",
            workspace_root=str(root),
            profile=dict(profile or {}),
        )

    def send_user_input(
        self,
        session: CodingSession,
        message: str,
        attachments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        event = {
            "type": "user_input",
            "session_id": session.session_id,
            "message": str(message or ""),
            "attachments": list(attachments or []),
        }
        session.events.append(event)
        return event

    def approve_action(self, session: CodingSession, action_id: str, approval: dict[str, Any]) -> dict[str, Any]:
        event = {
            "type": "approval",
            "session_id": session.session_id,
            "action_id": str(action_id),
            "approved": bool((approval or {}).get("approved")),
        }
        session.events.append(event)
        return event

    def validate_action(
        self,
        session: CodingSession,
        action_id: str,
        *,
        target_path: str | Path | None = None,
        context: dict[str, Any] | None = None,
        client_supplied_approved: bool | None = None,
    ) -> None:
        ensure_within_workspace(session.workspace_root, target_path)
        require_server_approval(
            action_id,
            context=context,
            client_supplied_approved=client_supplied_approved,
        )
