"""Viewer-authorized clipboard runner owned by the clipboard service pack."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Any, Final, Mapping


_MAX_BYTES: Final[int] = 1_048_576
_TIMEOUT_SECONDS: Final[int] = 5


def run_clipboard_host_action(
    action: str,
    payload: Mapping[str, Any] | None,
    *,
    viewer_host_approved: bool,
) -> dict[str, Any]:
    """Read or write text after the Viewer validated an exact token."""

    if not viewer_host_approved:
        raise PermissionError("Viewer approval is required")
    normalized = str(action or "").strip()
    if normalized == "computer.clipboard.read":
        text = _read_text()
        return {"action": normalized, "text": text, "format": "text/plain"}
    if normalized in {"computer.clipboard.write", "computer.clipboard.clear"}:
        arguments = dict(payload or {})
        text = "" if normalized.endswith(".clear") else str(arguments.get("text") or "")
        if len(text.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("clipboard text exceeds one MiB")
        _write_text(text)
        return {"action": normalized, "written": True, "cleared": text == ""}
    return {
        "action": normalized,
        "is_error": True,
        "error_type": "clipboard_runner_unavailable",
    }


def _read_text() -> str:
    command = _read_command()
    completed = subprocess.run(
        command,
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
    )
    if len(completed.stdout) > _MAX_BYTES:
        raise ValueError("clipboard text exceeds one MiB")
    return completed.stdout.decode("utf-8", errors="strict")


def _write_text(text: str) -> None:
    subprocess.run(
        _write_command(),
        input=text.encode("utf-8"),
        check=True,
        capture_output=True,
        timeout=_TIMEOUT_SECONDS,
    )


def _read_command() -> list[str]:
    if sys.platform == "darwin":
        return ["pbpaste"]
    if os.name == "nt":
        return [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            "Get-Clipboard -Raw",
        ]
    for command, arguments in (
        ("wl-paste", ["wl-paste", "--no-newline"]),
        ("xclip", ["xclip", "-selection", "clipboard", "-out"]),
        ("xsel", ["xsel", "--clipboard", "--output"]),
    ):
        if shutil.which(command):
            return arguments
    raise RuntimeError("system clipboard reader is unavailable")


def _write_command() -> list[str]:
    if sys.platform == "darwin":
        return ["pbcopy"]
    if os.name == "nt":
        return ["clip.exe"]
    for command, arguments in (
        ("wl-copy", ["wl-copy"]),
        ("xclip", ["xclip", "-selection", "clipboard"]),
        ("xsel", ["xsel", "--clipboard", "--input"]),
    ):
        if shutil.which(command):
            return arguments
    raise RuntimeError("system clipboard writer is unavailable")

