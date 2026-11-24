from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from domain.frontend.command_registry import SlashCommandRegistry, error, ok


_HOST_COMMAND_ALLOWLIST = {
    "git_read": frozenset({"git"}),
    "git_write": frozenset({"git"}),
    "terminal": frozenset(
        {
            "cargo",
            "git",
            "just",
            "mypy",
            "node",
            "npm",
            "npx",
            "python",
            "python3",
            "pytest",
            "ruff",
            "rustc",
            "swift",
            "true",
            "uv",
            "xcodebuild",
        }
    ),
}
def _curated_host_system_search_path() -> str:
    if os.name != "nt":
        return os.defpath
    candidates = str(os.environ.get("PATH") or "").split(os.pathsep)
    system_root = str(os.environ.get("SystemRoot") or "").strip()
    if system_root:
        candidates.insert(0, str(Path(system_root) / "System32"))
    safe = tuple(
        dict.fromkeys(
            item
            for item in candidates
            if item and item != "." and os.path.isabs(item)
        )
    )
    return os.pathsep.join(safe)


_HOST_SYSTEM_SEARCH_PATH = _curated_host_system_search_path()


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
        executable = approved.get("executable")
        if not isinstance(executable, dict):
            return error(
                "approved executable identity is missing",
                "APPROVED_OPERATION_PLAN_INVALID",
            )
        input_text = (
            str(args["patch"]) if action == "request_patch_approval" else None
        )
        completed = self._run_host_process(
            argv=tuple(argv),
            cwd=workspace,
            stdin=input_text,
            timeout_seconds=300,
            command_class=(
                "terminal"
                if action == "request_terminal_approval"
                else "git_write"
            ),
            allowed_cwds=(workspace,),
            executable_identity=executable,
        )
        if completed.exit_code != 0:
            return error(
                "approved host operation failed",
                "HOST_OPERATION_FAILED",
                details={
                    "exit_code": completed.exit_code,
                    "timed_out": completed.timed_out,
                    "stderr_sha256": self._text_digest(completed.stderr),
                    "stderr_bytes": len(completed.stderr.encode("utf-8")),
                    "stderr_truncated": completed.stderr_truncated,
                },
            )
        return ok(
            {
                "command": self.source_registry.public_command_contract(command),
                "executed": True,
                "action": action,
                # Raw argv/cwd/stdout/stderr stay in short-lived process memory.
                "execution_receipt": {
                    "executable": Path(argv[0]).name,
                    "exit_code": completed.exit_code,
                    "stdout_sha256": self._text_digest(completed.stdout),
                    "stdout_bytes": len(completed.stdout.encode("utf-8")),
                    "stdout_truncated": completed.stdout_truncated,
                    "boundary": completed.attestation.boundary,
                },
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

        git_executable = self._resolve_host_command(
            "git_read",
            ("git",),
            search_path=_HOST_SYSTEM_SEARCH_PATH,
        )
        workspace = self._workspace_root(context, git_executable)
        git_head = self._git_output(
            workspace,
            "rev-parse",
            "HEAD",
            executable_identity=git_executable,
        )
        git_index_tree = self._git_output(
            workspace,
            "write-tree",
            executable_identity=git_executable,
        )
        git_branch = self._current_branch(workspace, git_executable)
        git_status = self._git_output(
            workspace,
            "status",
            "--porcelain=v2",
            "--untracked-files=all",
            executable_identity=git_executable,
        )
        plan: dict[str, Any] = {
            "version": 2,
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
            checked = self._run_host_process(
                argv=("git", "check-ref-format", "--branch", branch),
                cwd=workspace,
                stdin=None,
                timeout_seconds=10,
                command_class="git_read",
                allowed_cwds=(workspace,),
                executable_identity=git_executable,
            )
            if checked.exit_code != 0:
                raise ValueError("push branch is invalid")
            remote_url = self._git_output(
                workspace,
                "remote",
                "get-url",
                "--push",
                remote,
                executable_identity=git_executable,
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
        command_class = (
            "terminal"
            if action == "request_terminal_approval"
            else "git_write"
        )
        executable = (
            self._resolve_host_command(command_class, tuple(argv))
            if action == "request_terminal_approval"
            else git_executable
        )
        plan["git_executable"] = git_executable
        plan["executable"] = executable
        plan["argv"] = [str(executable["path"]), *argv[1:]]
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

    def _workspace_root(
        self,
        context: dict[str, Any],
        git_executable: dict[str, Any],
    ) -> Path:
        explicit = str(context.get("workspace_path") or "").strip()
        if not explicit:
            raise ValueError("trusted workspace_path is required")
        candidate = Path(explicit).resolve()
        roots = self._authorized_workspace_roots(context)
        if not any(candidate == root or candidate.is_relative_to(root) for root in roots):
            raise ValueError("workspace_path is outside authorized workspace roots")
        completed = self._run_host_process(
            argv=("git", "-C", str(candidate), "rev-parse", "--show-toplevel"),
            cwd=candidate,
            stdin=None,
            timeout_seconds=10,
            command_class="git_read",
            allowed_cwds=(candidate,),
            executable_identity=git_executable,
        )
        if completed.exit_code != 0:
            raise ValueError("workspace is not inside a Git repository")
        workspace = Path(completed.stdout.strip()).resolve()
        if not any(workspace == root or workspace.is_relative_to(root) for root in roots):
            raise ValueError("Git workspace is outside authorized workspace roots")
        return workspace

    def _git_output(
        self,
        workspace: Path,
        *args: str,
        executable_identity: dict[str, Any],
    ) -> str:
        completed = self._run_host_process(
            argv=("git", *args),
            cwd=workspace,
            stdin=None,
            timeout_seconds=10,
            command_class="git_read",
            allowed_cwds=(workspace,),
            executable_identity=executable_identity,
        )
        if completed.exit_code != 0:
            raise ValueError("Git state inspection failed")
        return completed.stdout.strip()

    def _current_branch(
        self,
        workspace: Path,
        git_executable: dict[str, Any],
    ) -> str:
        completed = self._run_host_process(
            argv=("git", "branch", "--show-current"),
            cwd=workspace,
            stdin=None,
            timeout_seconds=10,
            command_class="git_read",
            allowed_cwds=(workspace,),
            executable_identity=git_executable,
        )
        if completed.exit_code != 0:
            raise ValueError("Git branch lookup failed")
        branch = completed.stdout.strip()
        if not branch:
            raise ValueError("cannot push from a detached HEAD")
        return branch

    @staticmethod
    def _authorized_workspace_roots(
        context: dict[str, Any],
    ) -> tuple[Path, ...]:
        raw_roots = context.get("authorized_workspace_roots")
        if (
            not isinstance(raw_roots, (list, tuple, set))
            or not raw_roots
        ):
            raise ValueError("trusted authorized_workspace_roots are required")
        unresolved_roots = tuple(Path(str(item)) for item in raw_roots)
        if any(not root.is_absolute() for root in unresolved_roots):
            raise ValueError("authorized workspace root is invalid")
        roots = tuple(root.resolve() for root in unresolved_roots)
        if any(not root.is_dir() for root in roots):
            raise ValueError("authorized workspace root is invalid")
        return roots

    @classmethod
    def _resolve_host_command(
        cls,
        command_class: str,
        argv: tuple[str, ...],
        *,
        search_path: str | None = None,
    ) -> dict[str, Any]:
        allowed_names = _HOST_COMMAND_ALLOWLIST.get(command_class)
        requested_name = Path(argv[0]).name
        if (
            allowed_names is None
            or requested_name not in allowed_names
            or argv[0] != requested_name
        ):
            raise ValueError("Host executable is not allowlisted")
        executable = shutil.which(
            argv[0],
            path=(
                search_path
                if search_path is not None
                else os.environ.get("PATH", _HOST_SYSTEM_SEARCH_PATH)
            ),
        )
        if executable is None:
            raise ValueError("Host executable is unavailable")
        identity = cls._executable_identity(Path(executable))
        identity["requested_name"] = requested_name
        return identity

    @staticmethod
    def _executable_identity(executable: Path) -> dict[str, Any]:
        try:
            resolved = executable.resolve(strict=True)
            metadata = resolved.stat()
        except OSError as exc:
            raise ValueError("Host executable is unavailable") from exc
        if (
            not stat.S_ISREG(metadata.st_mode)
            or (
                os.name != "nt"
                and metadata.st_mode & 0o111 == 0
            )
        ):
            raise ValueError("Host executable is unavailable")
        digest = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError as exc:
            raise ValueError("Host executable is unavailable") from exc
        return {
            "path": str(resolved),
            "sha256": "sha256:" + digest.hexdigest(),
            "size": metadata.st_size,
            "device": metadata.st_dev,
            "inode": metadata.st_ino,
            "mode": stat.S_IMODE(metadata.st_mode),
        }

    @classmethod
    def _verify_executable_identity(
        cls,
        command_class: str,
        argv: tuple[str, ...],
        expected: dict[str, Any],
    ) -> str:
        allowed_names = _HOST_COMMAND_ALLOWLIST.get(command_class)
        path = Path(str(expected.get("path") or ""))
        requested_name = str(expected.get("requested_name") or "")
        if (
            allowed_names is None
            or requested_name not in allowed_names
            or not path.is_absolute()
            or str(argv[0]) != str(path)
        ):
            raise ValueError("Host executable is not allowlisted")
        current = cls._executable_identity(path)
        current["requested_name"] = requested_name
        if current != expected:
            raise ValueError("Host executable changed after approval")
        return str(path)

    @classmethod
    def _run_host_process(
        cls,
        *,
        argv: tuple[str, ...],
        cwd: Path,
        stdin: str | None,
        timeout_seconds: float,
        command_class: str,
        allowed_cwds: tuple[Path, ...],
        executable_identity: dict[str, Any] | None = None,
    ) -> BoundedProcessResult:
        identity = (
            executable_identity
            if executable_identity is not None
            else cls._resolve_host_command(command_class, argv)
        )
        pinned_path = str(identity.get("path") or "")
        if argv[0] == str(identity.get("requested_name") or ""):
            pinned_argv = (pinned_path, *argv[1:])
        else:
            pinned_argv = argv
        executable = cls._verify_executable_identity(
            command_class,
            pinned_argv,
            identity,
        )
        resolved_argv = (executable, *argv[1:])
        child_path = os.pathsep.join(
            dict.fromkeys(
                (
                    str(Path(executable).parent),
                    *_HOST_SYSTEM_SEARCH_PATH.split(os.pathsep),
                )
            )
        )
        environment = {"PATH": child_path}
        if os.name == "nt":
            raw_system_root = str(os.environ.get("SystemRoot") or "").strip()
            if not raw_system_root:
                raise ValueError("Windows SystemRoot is unavailable")
            system_root = Path(raw_system_root).resolve()
            if not system_root.is_dir() or not (system_root / "System32").is_dir():
                raise ValueError("Windows SystemRoot is invalid")
            environment["SystemRoot"] = str(system_root)
        return HostBoundedProcessRunner().run_local(
            argv=resolved_argv,
            cwd=cwd.resolve(),
            stdin=stdin,
            timeout_seconds=timeout_seconds,
            environment=environment,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({resolved_argv[0]}),
                allowed_argv=(resolved_argv,),
                allowed_cwds=tuple(root.resolve() for root in allowed_cwds),
                allowed_environment=frozenset(environment),
                max_stdin_bytes=8 * 1024 * 1024,
                max_stdout_bytes=256 * 1024,
                max_stderr_bytes=64 * 1024,
                max_timeout_seconds=timeout_seconds,
            ),
        )

    @staticmethod
    def _text_digest(value: str) -> str:
        return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()

    def _state_mutation(
        self,
        command: dict[str, Any],
        execution: dict[str, Any],
        args: dict[str, Any],
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        raw_execution = command.get("execution")
        legacy_execution: dict[str, Any] = (
            dict(raw_execution) if isinstance(raw_execution, dict) else {}
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
        raw_execution = command.get("execution")
        legacy_execution: dict[str, Any] = (
            dict(raw_execution) if isinstance(raw_execution, dict) else {}
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
