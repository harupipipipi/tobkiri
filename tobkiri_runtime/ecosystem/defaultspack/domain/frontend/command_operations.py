from __future__ import annotations

import hashlib
import json
import re
import shlex
import subprocess
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from domain.frontend.command_registry import SlashCommandRegistry, error, ok


class CommandOperationRegistry:
    """Execute resolved v1 operation kinds through registered host/Pack handlers."""

    def __init__(
        self,
        source_registry: SlashCommandRegistry,
        pack_root: Path,
    ) -> None:
        self.source_registry = source_registry
        self.pack_root = pack_root

    def invoke(
        self,
        command: dict[str, Any],
        resolved: dict[str, Any],
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Invoke one already-resolved command without legacy type dispatch."""

        mode = str(payload.get("mode") or "chat")
        if mode not in command.get("modes", []):
            return error(
                "command is not available in this mode",
                "COMMAND_UNAVAILABLE",
                details={"mode": mode},
            )
        raw_args = payload.get("args") if isinstance(payload.get("args"), dict) else {}
        args = self.source_registry.coerce_operation_args(command, raw_args)
        if isinstance(args, dict) and args.get("status") == "error":
            return args

        execution = resolved["execution"]
        kind = str(execution.get("kind") or "")
        if kind == "host_operation":
            return self._host_operation(command, execution, args, payload, context)
        if kind == "state_mutation":
            return self._state_mutation(
                command,
                execution,
                args,
                payload,
            )
        if kind == "pack_operation":
            return self._pack_operation(
                command,
                execution,
                args,
                payload,
                context,
            )
        return error("resolved operation kind is unsupported", "INVALID_COMMAND")

    def binding_contract(
        self,
        command: dict[str, Any],
        resolved: dict[str, Any],
    ) -> dict[str, str]:
        execution = resolved.get("execution") or {}
        kind = str(execution.get("kind") or "")
        operation_ref = str(
            execution.get("operation_ref")
            or execution.get("state_ref")
            or ""
        )
        if not operation_ref:
            raise ValueError("operation binding has no canonical reference")
        verified, concrete_binding = self.source_registry.validate_operation_binding(
            command
        )
        if not verified:
            raise ValueError(
                f"operation binding probe failed: {concrete_binding or operation_ref}"
            )
        if kind == "host_operation":
            action = operation_ref.removeprefix("host:")
            if action not in {
                *{
                    "request_commit_approval",
                    "request_push_approval",
                    "request_terminal_approval",
                    "request_patch_approval",
                    "request_restore_approval",
                },
                *{
                    str((command.get("execution") or {}).get("action") or "")
                },
            }:
                raise ValueError(f"host operation is not registered: {operation_ref}")
            completion = (
                "backend_side_effect"
                if action.startswith("request_") and action.endswith("_approval")
                else "frontend_presentation"
            )
        elif kind == "state_mutation":
            completion = (
                "backend_authoritative_state"
                if (execution.get("offline") or {}).get("backend_authoritative")
                else "resolved_state_or_selection"
            )
        elif kind == "pack_operation":
            completion = "pack_runner_result"
        else:
            raise ValueError(f"unsupported operation kind: {kind}")
        return {
            "operation_ref": operation_ref,
            "kind": kind,
            "completion_semantics": completion,
            "verified_handler": True,
            "concrete_binding": concrete_binding,
        }

    def _host_operation(
        self,
        command: dict[str, Any],
        execution: dict[str, Any],
        args: dict[str, Any],
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        operation_ref = str(execution.get("operation_ref") or "")
        action = operation_ref.removeprefix("host:")
        if not action:
            return error("host operation is not registered", "INVALID_COMMAND")
        if action in {
            "request_commit_approval",
            "request_push_approval",
            "request_terminal_approval",
            "request_patch_approval",
            "request_restore_approval",
        }:
            if not payload.get("_approval_verified"):
                return error(
                    "host operation requires a verified approval continuation",
                    "APPROVAL_REQUIRED",
                )
            return self._execute_high_risk_host_operation(
                command,
                action,
                args,
                context,
            )
        return ok(
            {
                "command": self.source_registry.public_command_contract(command),
                "executed": False,
                "action": action,
                "args": args,
            }
        )

    def _execute_high_risk_host_operation(
        self,
        command: dict[str, Any],
        action: str,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        approved = context.get("_approved_operation_plan")
        if not isinstance(approved, dict):
            return error(
                "approved operation plan is missing",
                "APPROVED_OPERATION_PLAN_MISSING",
            )
        try:
            current = self.prepare_high_risk_plan(action, args, context)
        except ValueError as exc:
            return error(str(exc), "OPERATION_PLAN_INVALID")
        if self._plan_digest(current) != self._plan_digest(approved):
            return error(
                "workspace, Git state, or operation arguments changed after approval",
                "APPROVED_OPERATION_PLAN_CHANGED",
            )
        workspace = Path(str(approved["cwd"])).resolve()
        argv = [str(item) for item in approved["argv"]]
        input_text = (
            str(args["patch"]) if action == "request_patch_approval" else None
        )
        completed = subprocess.run(
            argv,
            cwd=workspace,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        if completed.returncode != 0:
            return error(
                "approved host operation failed",
                "HOST_OPERATION_FAILED",
                details={
                    "returncode": completed.returncode,
                    "stderr": completed.stderr[-4000:],
                },
            )
        return ok(
            {
                "command": self.source_registry.public_command_contract(command),
                "executed": True,
                "action": action,
                "argv": argv,
                "cwd": str(workspace),
                "stdout": completed.stdout[-4000:],
            }
        )

    def prepare_high_risk_plan(
        self,
        action: str,
        args: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve the exact high-risk operation before approval.

        The returned value is stable JSON and is hashed into the one-shot
        approval. It is recomputed immediately before execution, so a changed
        branch, index, HEAD, remote, workspace, or argv invalidates approval.
        """

        workspace = self._workspace_root(context)
        git_head = self._git_output(workspace, "rev-parse", "HEAD")
        git_index_tree = self._git_output(workspace, "write-tree")
        git_branch = self._current_branch(workspace)
        git_status = self._git_output(
            workspace,
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
        )
        plan: dict[str, Any] = {
            "version": 1,
            "action": action,
            "cwd": str(workspace),
            "git_head": git_head,
            "git_index_tree": git_index_tree,
            "git_branch": git_branch,
            "git_status_sha256": hashlib.sha256(
                git_status.encode("utf-8")
            ).hexdigest(),
        }
        if action == "request_commit_approval":
            message = str(args.get("message") or "")
            if not message or "\x00" in message:
                raise ValueError("commit message is invalid")
            argv = ["git", "commit", "-m", message]
        elif action == "request_push_approval":
            remote = str(args.get("remote") or "origin").strip()
            branch = str(args.get("branch") or git_branch).strip()
            if (
                not re.fullmatch(r"[A-Za-z0-9._-]+", remote)
                or remote.startswith("-")
            ):
                raise ValueError("push remote is invalid")
            if branch.startswith("-") or ":" in branch or branch.startswith("+"):
                raise ValueError("push branch/refspec is invalid")
            checked = subprocess.run(
                ["git", "check-ref-format", "--branch", branch],
                cwd=workspace,
                text=True,
                capture_output=True,
                timeout=10,
                check=False,
            )
            if checked.returncode != 0:
                raise ValueError("push branch is invalid")
            remote_url = self._git_output(
                workspace,
                "remote",
                "get-url",
                "--push",
                remote,
            )
            plan["push_remote"] = remote
            plan["push_remote_url_sha256"] = hashlib.sha256(
                remote_url.encode("utf-8")
            ).hexdigest()
            plan["push_branch"] = branch
            argv = ["git", "push", "--", remote, branch]
        elif action == "request_terminal_approval":
            argv = shlex.split(str(args.get("cmd") or ""))
            if not argv or any("\x00" in item for item in argv):
                raise ValueError("terminal argv is empty or invalid")
        elif action == "request_patch_approval":
            patch = str(args.get("patch") or "")
            if not patch:
                raise ValueError("patch is empty")
            plan["stdin_sha256"] = hashlib.sha256(
                patch.encode("utf-8")
            ).hexdigest()
            argv = ["git", "apply", "--whitespace=error", "-"]
        elif action == "request_restore_approval":
            paths = shlex.split(str(args.get("paths") or ""))
            if (
                not paths
                or any(path.startswith("-") or "\x00" in path for path in paths)
            ):
                raise ValueError("restore paths are invalid")
            argv = ["git", "restore", "--worktree", "--", *paths]
        else:
            raise ValueError("high-risk host operation is not registered")
        plan["argv"] = argv
        plan["plan_sha256"] = self._plan_digest(plan)
        return plan

    @staticmethod
    def _plan_digest(plan: dict[str, Any]) -> str:
        normalized = {key: value for key, value in plan.items() if key != "plan_sha256"}
        encoded = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _workspace_root(self, context: dict[str, Any]) -> Path:
        explicit = str(context.get("workspace_path") or "").strip()
        if not explicit:
            raise ValueError("trusted workspace_path is required")
        candidate = Path(explicit).resolve()
        allowed_roots = context.get("authorized_workspace_roots")
        if isinstance(allowed_roots, (list, tuple, set)) and allowed_roots:
            roots = [Path(str(item)).resolve() for item in allowed_roots]
            if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
                raise ValueError("workspace_path is outside authorized workspace roots")
        completed = subprocess.run(
            ["git", "-C", str(candidate), "rev-parse", "--show-toplevel"],
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError("workspace is not inside a Git repository")
        workspace = Path(completed.stdout.strip()).resolve()
        if isinstance(allowed_roots, (list, tuple, set)) and allowed_roots:
            roots = [Path(str(item)).resolve() for item in allowed_roots]
            if not any(workspace == root or workspace.is_relative_to(root) for root in roots):
                raise ValueError("Git workspace is outside authorized workspace roots")
        return workspace

    @staticmethod
    def _git_output(workspace: Path, *args: str) -> str:
        completed = subprocess.run(
            ["git", *args],
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )
        if completed.returncode != 0:
            raise ValueError(f"Git operation failed: {' '.join(args)}")
        return completed.stdout.strip()

    @staticmethod
    def _current_branch(workspace: Path) -> str:
        completed = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=10,
            check=True,
        )
        branch = completed.stdout.strip()
        if not branch:
            raise ValueError("cannot push from a detached HEAD")
        return branch

    def _state_mutation(
        self,
        command: dict[str, Any],
        execution: dict[str, Any],
        args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        legacy_execution = (
            command.get("execution")
            if isinstance(command.get("execution"), dict)
            else {}
        )
        source_type = str(legacy_execution.get("type") or "")
        if source_type == "model_command":
            return self.source_registry.invoke_model_operation(
                command,
                legacy_execution,
                args,
            )
        if source_type == "frontend":
            state_ref = str(execution.get("state_ref") or "")
            action = str(legacy_execution.get("action") or "")
            if not state_ref or not action:
                return error("state mutation handler is not registered", "INVALID_COMMAND")
            return ok(
                {
                    "command": self.source_registry.public_command_contract(command),
                    "executed": False,
                    "action": action,
                    "args": args,
                }
            )
        qualified_name = str(legacy_execution.get("qualified_name") or "")
        builtin_result = self.source_registry.invoke_builtin_operation(
            qualified_name,
            args,
            invocation=payload,
        )
        return self._builtin_result(command, builtin_result, payload)

    def _pack_operation(
        self,
        command: dict[str, Any],
        execution: dict[str, Any],
        args: dict[str, Any],
        payload: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        legacy_execution = (
            command.get("execution")
            if isinstance(command.get("execution"), dict)
            else {}
        )
        source_type = str(legacy_execution.get("type") or "")
        if source_type == "rumi_function":
            builtin_result = self.source_registry.invoke_builtin_operation(
                str(legacy_execution.get("qualified_name") or ""),
                args,
                invocation=payload,
            )
            return self._builtin_result(command, builtin_result, payload)
        if source_type == "chat_action":
            return self.source_registry.invoke_chat_operation(
                command,
                legacy_execution,
                args,
                payload,
                context,
            )
        if source_type == "pack_block":
            return self.source_registry.invoke_pack_operation(
                command,
                legacy_execution,
                args,
                payload,
                context,
            )
        if command.get("source") == "settings.registered_slash_commands":
            operation_ref = str(execution.get("operation_ref") or "")
            return ok(
                {
                    "command": deepcopy(command),
                    "executed": False,
                    "action": operation_ref.removeprefix("host:"),
                    "args": deepcopy(args),
                }
            )
        return error(
            "pack operation handler is not registered",
            "INVALID_COMMAND",
            details={"operation_ref": execution.get("operation_ref")},
        )

    def _builtin_result(
        self,
        command: dict[str, Any],
        builtin_result: dict[str, Any] | None,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        if (
            isinstance(builtin_result, dict)
            and builtin_result.get("status") == "error"
        ):
            return builtin_result
        if builtin_result is None:
            return error("Pack operation is not registered", "INVALID_COMMAND")
        operation_id = str(
            payload.get("invocation_id")
            or payload.get("operation_id")
            or uuid.uuid4()
        )
        response_payload: dict[str, Any] = {
            "command": self.source_registry.public_command_contract(command),
            "executed": True,
            "result": builtin_result,
            "operation_id": operation_id,
            "operation_status": "succeeded",
        }
        client_sequence = payload.get("client_sequence")
        if isinstance(client_sequence, int) and not isinstance(
            client_sequence,
            bool,
        ):
            response_payload["client_sequence"] = client_sequence
        state_snapshot = (
            builtin_result.get("state_snapshot")
            if isinstance(builtin_result, dict)
            else None
        )
        if isinstance(state_snapshot, dict):
            response_payload["state_changes"] = [state_snapshot]
        if str(builtin_result.get("message") or "").strip():
            response_payload["message"] = str(builtin_result["message"])
        return ok(response_payload)
