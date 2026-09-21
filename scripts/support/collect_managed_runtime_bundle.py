#!/usr/bin/env python3
"""Create a bounded, sanitized diagnostic ZIP for managed runtime work."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Iterable

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tobkiri_runtime.core_runtime.pack_boundary import resolve_pack_root

MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_LOG_FILES = 40
MAX_FRAME_BYTES = 5 * 1024 * 1024

BLOCKER_TEMPLATE = """# Blocker

Use this file when the managed runtime work is blocked. Keep it short, factual,
and free of secrets, tokens, raw typed text, cookies, browser profiles, and
unredacted frame captures.

## Failing command

## Expected

## Actual

## Environment dependency

## First failing commit

## Last passing commit

## Suspected files

## Minimal reproduction

## Work completed despite blocker
"""

SECRET_PATTERNS = (
    re.compile(r"(?i)(authorization\s*:\s*)([^\r\n]+)"),
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|password|secret)\b\s*[:=]\s*([^\s,;]+)"),
    re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
)

EXCLUDED_PARTS = {
    ".git",
    ".env",
    ".venv",
    "browser-profile",
    "browser_profiles",
    "build",
    "cookie",
    "cookies",
    "credential",
    "credentials",
    "dist",
    "keychain",
    "node_modules",
    "secret",
    "secrets",
    "target",
    "venv",
}


def redact(text: str, home: Path) -> str:
    result = text.replace(str(home), "<HOME>")
    for pattern in SECRET_PATTERNS:
        if pattern.groups >= 2:
            result = pattern.sub(lambda match: f"{match.group(1)}<REDACTED>", result)
        else:
            result = pattern.sub("<REDACTED>", result)
    return result


def run(command: list[str], cwd: Path, timeout: int = 30) -> str:
    try:
        completed = subprocess.run(command, cwd=cwd, text=True, capture_output=True, timeout=timeout, shell=False)
    except FileNotFoundError:
        return f"command unavailable: {command[0]}\n"
    except subprocess.TimeoutExpired:
        return f"command timed out: {' '.join(command)}\n"
    return (
        f"$ {' '.join(command)}\n"
        f"exit_code={completed.returncode}\n"
        f"--- stdout ---\n{completed.stdout}\n"
        f"--- stderr ---\n{completed.stderr}\n"
    )


def repo_root(start: Path) -> Path:
    completed = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=start,
        text=True,
        capture_output=True,
        timeout=10,
        shell=False,
    )
    if completed.returncode != 0:
        raise SystemExit("Run this script from inside a Git repository.")
    return Path(completed.stdout.strip()).resolve()


def allowed_file(path: Path) -> bool:
    lower_parts = {part.lower() for part in path.parts}
    if lower_parts & EXCLUDED_PARTS:
        return False
    if path.is_symlink():
        return False
    if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".sqlite", ".db"}:
        return False
    try:
        return path.is_file() and path.stat().st_size <= MAX_TEXT_BYTES
    except OSError:
        return False


def candidate_logs(root: Path) -> Iterable[Path]:
    patterns = (
        "**/*managed*runtime*.log",
        "**/*sandbox*.log",
        "**/*desktop*.log",
        "**/pytest*.log",
        "**/playwright*.log",
        "**/npm-*.log",
        "**/cargo-*.log",
    )
    seen: set[Path] = set()
    for pattern in patterns:
        for path in root.glob(pattern):
            resolved = path.resolve()
            if resolved in seen or not allowed_file(resolved):
                continue
            seen.add(resolved)
            yield resolved
            if len(seen) >= MAX_LOG_FILES:
                return


def write_text(path: Path, text: str, home: Path) -> None:
    path.write_text(redact(text, home)[:MAX_TEXT_BYTES], encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="rumi-managed-runtime-debug.zip")
    parser.add_argument(
        "--blocker",
        type=Path,
        help="Optional filled BLOCKER.md. It is redacted before being added to the bundle.",
    )
    parser.add_argument(
        "--write-blocker-template",
        type=Path,
        help="Write the BLOCKER.md template to this path and exit.",
    )
    parser.add_argument(
        "--include-redacted-frame",
        type=Path,
        help="Optional pre-redacted image. The filename must contain '.redacted.'.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    home = Path.home().resolve()

    if args.write_blocker_template:
        target = args.write_blocker_template.expanduser().resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(BLOCKER_TEMPLATE, encoding="utf-8")
        print(target)
        return 0

    root = repo_root(Path.cwd())
    output = Path(args.output).expanduser().resolve()
    managed_template_root = resolve_pack_root("rumi_sandbox_runtime_pack") / "templates"

    with tempfile.TemporaryDirectory(prefix="rumi-runtime-bundle-") as tmp_name:
        tmp = Path(tmp_name)
        (tmp / "BLOCKER.template.md").write_text(BLOCKER_TEMPLATE, encoding="utf-8")
        if args.blocker:
            blocker = args.blocker.expanduser().resolve()
            if not allowed_file(blocker):
                raise SystemExit("Blocker file is unavailable, excluded, or too large.")
            write_text(tmp / "BLOCKER.md", blocker.read_text(encoding="utf-8", errors="replace"), home)
        else:
            (tmp / "BLOCKER.md").write_text(BLOCKER_TEMPLATE, encoding="utf-8")

        commands = {
            "git-status.txt": ["git", "status", "--short", "--branch"],
            "git-head.txt": ["git", "show", "-s", "--format=fuller", "HEAD"],
            "git-diff-stat.txt": ["git", "diff", "--stat"],
            "git-diff.patch": ["git", "diff", "--no-ext-diff"],
            "managed-runtime-files-tracked.txt": [
                "git",
                "ls-files",
                "tobkiri_runtime/docs/managed-sandbox-runtime-implementation-plan.md",
                str(managed_template_root),
                "tobkiri_runtime/scripts/quality/check_template_contracts.py",
                "scripts/support/collect_managed_runtime_bundle.py",
            ],
            "managed-runtime-files-untracked.txt": [
                "git",
                "ls-files",
                "--others",
                "--exclude-standard",
                "tobkiri_runtime/docs/managed-sandbox-runtime-implementation-plan.md",
                str(managed_template_root),
                "tobkiri_runtime/scripts/quality/check_template_contracts.py",
                "scripts/support/collect_managed_runtime_bundle.py",
            ],
            "python-version.txt": [sys.executable, "--version"],
            "node-version.txt": ["node", "--version"],
            "npm-version.txt": ["npm", "--version"],
            "cargo-version.txt": ["cargo", "--version"],
        }
        for filename, command in commands.items():
            write_text(tmp / filename, run(command, root), home)

        branch_output = run(["git", "branch", "--show-current"], root)
        environment = {
            "platform": platform.platform(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
            "repo": root.name,
            "branch": branch_output.splitlines()[-1:] or [],
            "limits": {
                "max_text_bytes": MAX_TEXT_BYTES,
                "max_log_files": MAX_LOG_FILES,
                "max_frame_bytes": MAX_FRAME_BYTES,
            },
        }
        (tmp / "environment.json").write_text(json.dumps(environment, indent=2) + "\n", encoding="utf-8")

        logs_dir = tmp / "logs"
        logs_dir.mkdir()
        for index, source in enumerate(candidate_logs(root), start=1):
            relative = source.relative_to(root) if source.is_relative_to(root) else Path(source.name)
            safe_name = f"{index:02d}-" + "__".join(relative.parts)
            write_text(logs_dir / safe_name, source.read_text(encoding="utf-8", errors="replace"), home)

        if args.include_redacted_frame:
            frame = args.include_redacted_frame.expanduser().resolve()
            if ".redacted." not in frame.name.lower():
                raise SystemExit("Frame filename must contain '.redacted.' to prevent accidental leakage.")
            if frame.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise SystemExit("Unsupported frame format.")
            data = frame.read_bytes()
            if len(data) > MAX_FRAME_BYTES:
                raise SystemExit("Redacted frame is too large.")
            (tmp / frame.name).write_bytes(data)
            (tmp / "redacted-frame.sha256").write_text(hashlib.sha256(data).hexdigest() + "\n", encoding="utf-8")

        manifest = {
            "bundle": "rumi-managed-runtime-debug",
            "schema_version": 1,
            "redaction": {
                "home_path_replaced": True,
                "secret_patterns_applied": True,
                "excluded_parts": sorted(EXCLUDED_PARTS),
                "raw_frames_included": False,
            },
            "blocker_template": "BLOCKER.template.md",
        }
        (tmp / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        output.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(tmp.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(tmp))

    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
