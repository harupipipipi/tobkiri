from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from blocks._common import ok, error


def _applescript_string(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _choose_directory_macos(prompt: str) -> tuple[str | None, bool, str | None]:
    script = "POSIX path of (choose folder with prompt {})".format(_applescript_string(prompt))
    try:
        completed = subprocess.run(
            ["osascript", "-e", script],
            text=True,
            capture_output=True,
            timeout=300,
        )
    except FileNotFoundError:
        return None, False, "osascript is not available"
    except subprocess.TimeoutExpired:
        return None, False, "folder selection timed out"
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        if "User canceled" in stderr or "ユーザによってキャンセル" in stderr:
            return None, True, None
        return None, False, stderr or "folder selection failed"
    selected = (completed.stdout or "").strip()
    return selected or None, False, None


def _choose_directory_tk(prompt: str) -> tuple[str | None, bool, str | None]:
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:
        return None, False, str(exc)
    try:
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        selected = filedialog.askdirectory(title=prompt)
        root.destroy()
    except Exception as exc:
        return None, False, str(exc)
    return (selected or None), not bool(selected), None


def _normalize_selected_directory(path: str) -> str:
    resolved = Path(path).expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("selected path is not a directory")
    return str(resolved)


def run(input_data, context=None):
    del context
    prompt = str((input_data or {}).get("prompt") or "保存先フォルダを選択")
    selected = None
    cancelled = False
    last_error = None
    if sys.platform == "darwin":
        selected, cancelled, last_error = _choose_directory_macos(prompt)
    if not selected and not cancelled:
        selected, cancelled, last_error = _choose_directory_tk(prompt)
    if cancelled:
        return ok({"path": None, "cancelled": True})
    if not selected:
        return error(last_error or "folder selection failed", code="DIRECTORY_SELECTION_FAILED")
    try:
        return ok({"path": _normalize_selected_directory(selected), "cancelled": False})
    except ValueError as exc:
        return error(str(exc), code="INVALID_DIRECTORY")
