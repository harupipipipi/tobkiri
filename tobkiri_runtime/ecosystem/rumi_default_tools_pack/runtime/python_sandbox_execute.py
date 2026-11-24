"""Saved-workspace Host adapter for the finite Python PackVM operation."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path, PurePosixPath
import stat
from typing import Any, Mapping

from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from ecosystem.defaultspack.domain.coding.workspace_jail import WorkspaceJail
from tobkiri_host.saved_workspace_context import require_saved_workspace_binding

PACK = "rumi_default_tools_pack"
TOOL_ID = "python_sandbox_execute"
LOCAL_CONTRACT = "tobkiri.service.tool.local.operation.v1"
LOCAL_OPERATION = f"{PACK}.python-sandbox-execute-operation"
LOCAL_FUNCTION = f"{PACK}.python-sandbox-execute-tool"
GUEST_CONTRACT = "tobkiri.service.python.sandbox.v1"
GUEST_OPERATION = "defaultspack.python-sandbox-guest-execute"
GUEST_FUNCTION = "defaultspack.python-sandbox-guest"
GUEST_VERSION = "1.0.0"
ALLOWED_CONTRACTS = frozenset({GUEST_CONTRACT})

MAX_FILES = 256
MAX_ENTRIES = 1024
MAX_DEPTH = 64
MAX_FILE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_CHILD_REQUEST_BYTES = 900 * 1024
MAX_ARGUMENTS = 32
MAX_ARGUMENT_BYTES = 1024
MAX_ARGUMENT_TOTAL_BYTES = 16 * 1024
MAX_STDIN_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 60
MAX_OUTPUT_BYTES = 64 * 1024

_SKIP_DIRS = frozenset(
    {
        ".git",
        ".rumi_snapshots",
        ".mypy_cache",
        ".pytest_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
def validate_arguments(value: Any) -> dict[str, Any]:
    """Use the same closed validator as native consent and saved approval."""
    from tobkiri_host.saved_tool_consent import validate_python_sandbox_arguments

    return validate_python_sandbox_arguments(value)


def _open_canonical_root(root_text: str, expected_dev: int, expected_ino: int) -> int:
    """Open each root component without following a symlink and verify pins."""
    root = Path(root_text)
    if not root.is_absolute() or str(root) != root_text or ".." in root.parts:
        raise PermissionError("saved workspace root is not canonical")
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    nofollow_flag = getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open("/", os.O_RDONLY | directory_flag)
    try:
        for part in root.parts[1:]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | directory_flag | nofollow_flag,
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_dev != expected_dev
            or metadata.st_ino != expected_ino
        ):
            raise PermissionError("saved workspace root identity changed")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def snapshot_workspace(
    root_text: str, expected_dev: int, expected_ino: int
) -> list[dict[str, str]]:
    """Read a small no-follow snapshot; reject races and aggregate overflow."""
    root_fd = _open_canonical_root(root_text, expected_dev, expected_ino)
    result: list[dict[str, str]] = []
    audit = {"bytes": 0, "files": 0, "entries": 0}
    jail = WorkspaceJail(root_text)

    def walk(directory_fd: int, parent_parts: tuple[str, ...]) -> None:
        before = os.fstat(directory_fd)
        names = sorted(os.listdir(directory_fd))
        audit["entries"] += len(names)
        if audit["entries"] > MAX_ENTRIES:
            raise ValueError("saved workspace snapshot has too many entries")
        for name in names:
            if not name or name in {".", ".."} or "/" in name or "\\" in name:
                raise PermissionError("saved workspace entry name is invalid")
            parts = parent_parts + (name,)
            if len(parts) > MAX_DEPTH:
                raise ValueError("saved workspace snapshot is too deep")
            relative = PurePosixPath(*parts).as_posix()
            try:
                jail.ensure_allowed(relative, operation="python_sandbox_stage")
            except PermissionError:
                continue
            if any(part in _SKIP_DIRS for part in parts):
                continue
            metadata = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if stat.S_ISDIR(metadata.st_mode):
                child_fd = os.open(
                    name,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_CLOEXEC", 0),
                    dir_fd=directory_fd,
                )
                try:
                    opened = os.fstat(child_fd)
                    if (opened.st_dev, opened.st_ino) != (
                        metadata.st_dev,
                        metadata.st_ino,
                    ):
                        raise PermissionError(
                            "saved workspace directory changed during staging"
                        )
                    walk(child_fd, parts)
                    after = os.fstat(child_fd)
                    if (after.st_mtime_ns, after.st_ctime_ns) != (
                        opened.st_mtime_ns,
                        opened.st_ctime_ns,
                    ):
                        raise PermissionError("saved workspace changed during staging")
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                # Symlinks, hard links, devices, sockets and FIFOs never enter
                # the guest snapshot. A requested path omitted here fails in
                # the guest rather than resolving against the Host.
                continue
            if metadata.st_size > MAX_FILE_BYTES:
                raise ValueError("saved workspace file exceeds the snapshot limit")
            audit["files"] += 1
            if audit["files"] > MAX_FILES:
                raise ValueError("saved workspace snapshot has too many files")
            audit["bytes"] += metadata.st_size
            if audit["bytes"] > MAX_SNAPSHOT_BYTES:
                raise ValueError("saved workspace snapshot exceeds 512 KiB")
            file_fd = os.open(
                name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NONBLOCK", 0),
                dir_fd=directory_fd,
            )
            try:
                opened = os.fstat(file_fd)
                if (
                    not stat.S_ISREG(opened.st_mode)
                    or opened.st_nlink != 1
                    or (opened.st_dev, opened.st_ino, opened.st_size)
                    != (metadata.st_dev, metadata.st_ino, metadata.st_size)
                ):
                    raise PermissionError("saved workspace file changed during staging")
                chunks: list[bytes] = []
                remaining = MAX_FILE_BYTES + 1
                while remaining:
                    chunk = os.read(file_fd, min(remaining, 16 * 1024))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    remaining -= len(chunk)
                content = b"".join(chunks)
                after = os.fstat(file_fd)
                if (
                    len(content) != metadata.st_size
                    or len(content) > MAX_FILE_BYTES
                    or (after.st_mtime_ns, after.st_ctime_ns)
                    != (opened.st_mtime_ns, opened.st_ctime_ns)
                ):
                    raise PermissionError("saved workspace file changed during staging")
            finally:
                os.close(file_fd)
            result.append(
                {
                    "path": relative,
                    "content_base64": base64.b64encode(content).decode("ascii"),
                }
            )
        after = os.fstat(directory_fd)
        if (after.st_mtime_ns, after.st_ctime_ns) != (
            before.st_mtime_ns,
            before.st_ctime_ns,
        ):
            raise PermissionError("saved workspace directory changed during staging")

    try:
        walk(root_fd, ())
    finally:
        os.close(root_fd)
    result.sort(key=lambda item: item["path"])
    encoded = json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_CHILD_REQUEST_BYTES:
        raise ValueError(
            "saved workspace encoded snapshot exceeds the PackVM request limit"
        )
    return result


def validate_guest_result(value: Any) -> dict[str, Any]:
    """Accept only the finite result frame from the dedicated guest child."""
    if not isinstance(value, Mapping) or set(value) != {
        "status",
        "exit_code",
        "stdout",
        "stderr",
        "duration_ms",
    }:
        raise ValueError("Python sandbox guest result is invalid")
    if (
        value["status"] not in {"completed", "failed"}
        or type(value["exit_code"]) is not int
        or not -255 <= value["exit_code"] <= 255
        or not isinstance(value["stdout"], str)
        or not isinstance(value["stderr"], str)
        or not isinstance(value["duration_ms"], int)
        or isinstance(value["duration_ms"], bool)
        or value["duration_ms"] < 0
        or value["duration_ms"] > MAX_TIMEOUT_SECONDS * 1000
        or (value["status"] == "completed") != (value["exit_code"] == 0)
    ):
        raise ValueError("Python sandbox guest result fields are invalid")
    if (
        len(value["stdout"].encode("utf-8")) > MAX_OUTPUT_BYTES
        or len(value["stderr"].encode("utf-8")) > MAX_OUTPUT_BYTES
    ):
        raise ValueError("Python sandbox guest result exceeds the output limit")
    return dict(value)


def _tool_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Use the exact consent digest validator before resolving saved authority."""
    from tobkiri_host.saved_tool_consent import validate_python_sandbox_payload

    return validate_python_sandbox_payload(payload)


def _bind(context: Any) -> HostFunction:
    if context.user_data_root is None:
        raise PermissionError("Python sandbox requires the selected workspace owner")
    profile_id = context.profile_id

    def invoke(payload: Mapping[str, Any], invocation: Any) -> Mapping[str, Any]:
        tool_payload = _tool_payload(payload)
        arguments = tool_payload["arguments"]
        invocation.assert_current()
        binding = require_saved_workspace_binding(invocation)
        binding.assert_current()
        if binding.resolution.profile_id != profile_id:
            raise PermissionError("Python sandbox saved workspace profile changed")
        snapshot = snapshot_workspace(
            binding.canonical_root,
            binding.resolution.root_st_dev,
            binding.resolution.root_st_ino,
        )
        binding.assert_current()
        invocation.assert_current()
        client = invocation.contract_client(
            allowed_contract_ids=ALLOWED_CONTRACTS,
            consumer_pack_id=PACK,
            include_credentials=False,
        )
        guest_request = {
            **arguments,
            "workspace_snapshot": snapshot,
        }
        binding.assert_current()
        invocation.assert_current()
        result = client.invoke(GUEST_CONTRACT, GUEST_OPERATION, guest_request)
        binding.assert_current()
        invocation.assert_current()
        guest_result = validate_guest_result(result)
        return {
            "result": json.dumps(guest_result, ensure_ascii=False, sort_keys=True),
            "is_error": guest_result["exit_code"] != 0,
            "widget": None,
        }

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    LOCAL_FUNCTION,
    LOCAL_CONTRACT,
    LOCAL_OPERATION,
    _bind,
)
