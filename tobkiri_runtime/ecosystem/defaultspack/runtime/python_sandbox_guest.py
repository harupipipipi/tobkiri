"""One-shot PackVM Python CLI and unittest entrypoint.

The trusted PackVM runner selects this file only after it verifies the signed
Function, contract, version, and operation tuple. The runner validates the same
finite request independently and invokes this file inside its existing VZ guest
namespace. This process receives a workspace snapshot and user input only. Its
stdout and stderr are opaque program output; this module emits no result frame.
"""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import runpy
import sys
import tempfile
from typing import Any
import unittest

MAX_FILES = 256
MAX_ENTRIES = 1024
MAX_DEPTH = 64
MAX_FILE_BYTES = 64 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024
MAX_REQUEST_BYTES = 900 * 1024
MAX_ARGUMENTS = 32
MAX_ARGUMENT_BYTES = 1024
MAX_ARGUMENT_TOTAL_BYTES = 16 * 1024
MAX_STDIN_BYTES = 16 * 1024
MAX_TIMEOUT_SECONDS = 60
_MODULE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")


def _relative_path(value: Any, *, suffix: str | None = None, allow_dot: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 512
        or "\x00" in value
        or "\\" in value
        or value.startswith(("/", "~"))
        or ":" in value
    ):
        raise ValueError("relative path is invalid")
    if allow_dot and value == ".":
        return "."
    parts = value.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError("relative path is not canonical")
    path = PurePosixPath(*parts).as_posix()
    if suffix is not None and not path.endswith(suffix):
        raise ValueError("script path must end in .py")
    return path


def _validate(value: object) -> dict[str, Any]:
    """Independently validate the complete guest request before staging."""
    if not isinstance(value, Mapping):
        raise ValueError("request must be an object")
    mode = value.get("mode")
    common = {"mode", "cwd", "timeout_seconds", "workspace_snapshot"}
    if mode == "script":
        if set(value) != common | {"script_path", "arguments", "stdin"}:
            raise ValueError("script request fields are invalid")
        script_path = _relative_path(value["script_path"], suffix=".py")
        arguments = value["arguments"]
        if not isinstance(arguments, list) or len(arguments) > MAX_ARGUMENTS:
            raise ValueError("script arguments are invalid")
        argument_bytes = 0
        normalized_arguments: list[str] = []
        for item in arguments:
            if not isinstance(item, str) or not item or "\x00" in item:
                raise ValueError("script argument is invalid")
            size = len(item.encode("utf-8"))
            if size > MAX_ARGUMENT_BYTES:
                raise ValueError("script argument exceeds its limit")
            argument_bytes += size
            if argument_bytes > MAX_ARGUMENT_TOTAL_BYTES:
                raise ValueError("script arguments exceed their total limit")
            normalized_arguments.append(item)
        stdin = value["stdin"]
        if not isinstance(stdin, str) or len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
            raise ValueError("script stdin exceeds its limit")
        operation: dict[str, Any] = {
            "mode": "script",
            "script_path": script_path,
            "arguments": normalized_arguments,
            "stdin": stdin,
        }
    elif mode == "unittest":
        if set(value) != common | {"test_targets"}:
            raise ValueError("unittest request fields are invalid")
        targets = value["test_targets"]
        if (
            not isinstance(targets, list)
            or not 1 <= len(targets) <= 16
            or len(set(targets)) != len(targets)
            or any(
                not isinstance(item, str)
                or len(item) > 256
                or _MODULE.fullmatch(item) is None
                for item in targets
            )
        ):
            raise ValueError("unittest targets are invalid")
        operation = {"mode": "unittest", "test_targets": list(targets)}
    else:
        raise ValueError("mode is unsupported")

    cwd = _relative_path(value["cwd"], allow_dot=True)
    timeout = value["timeout_seconds"]
    if type(timeout) is not int or not 1 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout is invalid")

    files = value["workspace_snapshot"]
    if not isinstance(files, list) or len(files) > MAX_FILES:
        raise ValueError("workspace snapshot is invalid")
    normalized_files: list[dict[str, str]] = []
    seen: set[str] = set()
    total_bytes = 0
    all_entries: set[str] = set()
    for entry in files:
        if not isinstance(entry, Mapping) or set(entry) != {"path", "content_base64"}:
            raise ValueError("workspace entry is invalid")
        path = _relative_path(entry["path"])
        if path in seen:
            raise ValueError("workspace file is duplicated")
        seen.add(path)
        parts = PurePosixPath(path).parts
        if len(parts) > MAX_DEPTH:
            raise ValueError("workspace snapshot is too deep")
        for index in range(1, len(parts) + 1):
            all_entries.add("/".join(parts[:index]))
        if len(all_entries) > MAX_ENTRIES:
            raise ValueError("workspace snapshot has too many entries")
        content_text = entry["content_base64"]
        max_encoded = ((MAX_FILE_BYTES + 2) // 3) * 4
        if not isinstance(content_text, str) or len(content_text) > max_encoded:
            raise ValueError("workspace file exceeds its limit")
        try:
            content = base64.b64decode(content_text, validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ValueError("workspace base64 is invalid") from exc
        if (
            len(content) > MAX_FILE_BYTES
            or base64.b64encode(content).decode("ascii") != content_text
        ):
            raise ValueError("workspace file encoding is not canonical")
        total_bytes += len(content)
        if total_bytes > MAX_SNAPSHOT_BYTES:
            raise ValueError("workspace snapshot exceeds 512 KiB")
        normalized_files.append({"path": path, "content_base64": content_text})
    if normalized_files != sorted(normalized_files, key=lambda item: item["path"]):
        raise ValueError("workspace files are not sorted")
    if any(
        any(parent.as_posix() in seen for parent in PurePosixPath(path).parents if parent.as_posix() != ".")
        for path in seen
    ):
        raise ValueError("workspace paths collide")
    if mode == "script" and operation["script_path"] not in seen:
        raise ValueError("script is absent from the workspace snapshot")
    if cwd != "." and not any(path.startswith(cwd + "/") for path in seen):
        raise ValueError("working directory is absent from the workspace snapshot")

    request = {
        **operation,
        "cwd": cwd,
        "timeout_seconds": timeout,
        "workspace_snapshot": normalized_files,
    }
    if len(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()) > MAX_REQUEST_BYTES:
        raise ValueError("request exceeds its encoded limit")
    return request


def _materialize(snapshot: list[dict[str, str]], root: Path) -> None:
    """Create only the validated bounded snapshot in a private tmpfs tree."""
    for entry in snapshot:
        destination = root.joinpath(*PurePosixPath(entry["path"]).parts)
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        content = base64.b64decode(entry["content_base64"], validate=True)
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            view = memoryview(content)
            while view:
                count = os.write(descriptor, view)
                if count <= 0:
                    raise OSError("snapshot write made no progress")
                view = view[count:]
        finally:
            os.close(descriptor)


def _main() -> int:
    """Validate one finite JSON request, then run ordinary Python CLI code."""
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    if len(raw) > MAX_REQUEST_BYTES:
        sys.stderr.write("Python sandbox request exceeds its limit.\n")
        return 1
    try:
        request = _validate(json.loads(raw))
    except (ValueError, TypeError, RecursionError, json.JSONDecodeError):
        sys.stderr.write("Python sandbox request is invalid.\n")
        return 1

    with tempfile.TemporaryDirectory(prefix="tobkiri-python-", dir="/tmp") as name:
        root = Path(name) / "workspace"
        root.mkdir(mode=0o700)
        _materialize(request["workspace_snapshot"], root)
        cwd = root if request["cwd"] == "." else root.joinpath(*PurePosixPath(request["cwd"]).parts)
        os.chdir(cwd)
        # All trusted bootstrap and unittest imports happen before the supplied
        # workspace is added to sys.path.
        if str(cwd) not in sys.path:
            sys.path.insert(0, str(cwd))
        if request["mode"] == "script":
            script = root.joinpath(*PurePosixPath(request["script_path"]).parts)
            sys.argv = [str(script), *request["arguments"]]
            sys.path.insert(0, str(script.parent))
            sys.stdin = io.TextIOWrapper(io.BytesIO(request["stdin"].encode("utf-8")), encoding="utf-8")
            try:
                runpy.run_path(str(script), run_name="__main__")
            except SystemExit as exit_request:
                code = exit_request.code
                if code is None:
                    return 0
                if type(code) is int:
                    return code
                if code:
                    sys.stderr.write(f"{code}\n")
                return 1
            return 0

        program = unittest.TestProgram(
            module=None,
            argv=["python3", *request["test_targets"]],
            exit=False,
        )
        return 0 if program.result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(_main())
