"""ファイル操作ドメインロジック

ワークスペースルート相対パスで動作し、パストラバーサルを防止する。
"""

import difflib
import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from typing import Any

from .workspace_jail import WorkspaceJail

MAX_TEXT_FILE_BYTES = 2 * 1024 * 1024
SNAPSHOT_DIR = ".rumi_snapshots"
SNAPSHOT_MANIFEST = "snapshot.json"
SNAPSHOT_CONTENT_DIR = "contents"
TERMINAL_LOG = "terminal_log.jsonl"
WORKTREE_SCHEMA_VERSION = 2
COMMAND_LOG_LIMIT = 50
SNAPSHOT_ID_PATTERN = re.compile(r"^\d{8}T\d{6}Z-[0-9a-fA-F]{8}$")
PROTECTED_PATHS = {".git", SNAPSHOT_DIR}
CHECKPOINT_SKIPPED_DIRS = {
    ".cache",
    ".mypy_cache",
    ".next",
    ".nox",
    ".nuxt",
    ".pytest_cache",
    ".ruff_cache",
    ".svelte-kit",
    ".tox",
    ".turbo",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}


class FileOps:
    """ファイル操作を提供するクラス。

    全てのパスはワークスペースルートからの相対パスとして解釈され、
    ルート外へのアクセスは拒否される。
    """

    def __init__(self, workspace_root=None):
        if workspace_root is None:
            workspace_root = os.getcwd()
        self._root = os.path.realpath(workspace_root)
        self._jail = WorkspaceJail(self._root)

    @property
    def root(self):
        return self._root

    def _resolve(self, path):
        """パスをワークスペースルート配下に正規化する。

        ルート外を指す場合は ValueError を送出する。
        """
        return str(self._jail.resolve(path, allow_absolute=True))

    def _resolve_user_path(self, path, operation="access"):
        resolved = str(self._jail.resolve_user_path(path))
        rel = self._relative(resolved)
        self._jail.ensure_allowed(rel, operation=operation)
        return resolved

    def _relative(self, resolved):
        return os.path.relpath(resolved, self._root).replace(os.sep, "/")

    def _ensure_unprotected_mutation(self, resolved):
        rel = self._relative(resolved)
        self._jail.ensure_allowed(rel, operation="mutation")
        parts = set(rel.replace("\\", "/").split("/"))
        if rel in PROTECTED_PATHS or parts & PROTECTED_PATHS:
            raise PermissionError("Protected workspace path cannot be modified: " + rel)

    def _is_protected_rel(self, rel):
        rel = str(rel or "").replace("\\", "/").strip("/")
        if not rel or rel == ".":
            return False
        parts = set(rel.split("/"))
        return rel in PROTECTED_PATHS or bool(parts & PROTECTED_PATHS)

    def _is_restricted_rel(self, rel):
        return self._jail.restriction_reason(rel) is not None

    def _normalize_rel(self, rel):
        rel = str(rel or ".").replace("\\", "/").strip("/")
        return rel or "."

    def _has_explicit_selection_inside(self, rel, selected_rels):
        rel = self._normalize_rel(rel)
        for selected in selected_rels:
            selected = self._normalize_rel(selected)
            if selected == ".":
                continue
            if (
                selected == rel
                or selected.startswith(rel.rstrip("/") + "/")
                or rel.startswith(selected.rstrip("/") + "/")
            ):
                return True
        return False

    def _is_skipped_checkpoint_dir(self, rel, selected_rels):
        rel = self._normalize_rel(rel)
        first = rel.split("/", 1)[0]
        return first in CHECKPOINT_SKIPPED_DIRS and not self._has_explicit_selection_inside(rel, selected_rels)

    def _is_inside_root(self, resolved):
        real = os.path.realpath(resolved)
        return real == self._root or real.startswith(self._root + os.sep)

    def _ensure_text_size(self, resolved):
        if os.path.exists(resolved) and os.path.isfile(resolved):
            size = os.path.getsize(resolved)
            if size > MAX_TEXT_FILE_BYTES:
                raise ValueError(f"File is too large for text operation: {size} bytes")

    def _looks_binary(self, resolved):
        if not os.path.isfile(resolved):
            return False
        with open(resolved, "rb") as handle:
            sample = handle.read(4096)
        return b"\0" in sample

    def _sha256_file(self, resolved):
        digest = hashlib.sha256()
        with open(resolved, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _safe_stat(self, resolved):
        try:
            stat = os.stat(resolved)
        except OSError:
            return {}
        return {
            "mode": stat.st_mode,
            "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1000000000)),
            "size": stat.st_size,
        }

    def _split_git_z(self, raw):
        if not raw:
            return []
        return [item for item in raw.split("\0") if item]

    @staticmethod
    def _normalize_git_status_path(path):
        text = str(path or "").strip()
        if not text:
            return ""
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = []
        if len(parts) == 1:
            return parts[0]
        return text.strip('"')

    def _porcelain_v1_paths(self, path_text):
        return tuple(
            normalized
            for normalized in (
                self._normalize_git_status_path(part)
                for part in str(path_text or "").split(" -> ")
            )
            if normalized
        )

    def _visible_porcelain_v1_path(self, path_text):
        paths = self._porcelain_v1_paths(path_text)
        return bool(paths) and all(not self._is_restricted_rel(path) for path in paths)

    def _porcelain_v2_paths(self, line):
        text = str(line or "")
        if text.startswith("#"):
            return ()
        if text.startswith(("? ", "! ")):
            return (self._normalize_git_status_path(text[2:]),)
        if text.startswith("1 "):
            parts = text.split(maxsplit=8)
            return (self._normalize_git_status_path(parts[8]),) if len(parts) > 8 else ()
        if text.startswith("2 "):
            parts = text.split(maxsplit=9)
            if len(parts) <= 9:
                return ()
            return tuple(
                normalized
                for normalized in (
                    self._normalize_git_status_path(part)
                    for part in parts[9].split("\t")
                )
                if normalized
            )
        if text.startswith("u "):
            parts = text.split(maxsplit=10)
            return (self._normalize_git_status_path(parts[10]),) if len(parts) > 10 else ()
        parts = text.split()
        return (self._normalize_git_status_path(parts[-1]),) if parts else ()

    def _visible_porcelain_v2_line(self, line):
        paths = self._porcelain_v2_paths(line)
        return str(line).startswith("#") or (bool(paths) and all(not self._is_restricted_rel(path) for path in paths))

    def _filter_git_porcelain_v1(self, text):
        lines = []
        for line in str(text or "").splitlines():
            if len(line) >= 4 and not self._visible_porcelain_v1_path(line[3:]):
                continue
            lines.append(line)
        return "\n".join(lines) + ("\n" if lines else "")

    def _filter_git_porcelain_v2(self, text):
        lines = []
        for line in str(text or "").splitlines():
            if not self._visible_porcelain_v2_line(line):
                continue
            lines.append(line)
        return "\n".join(lines) + ("\n" if lines else "")

    def _run_git_text(self, args, cwd=None, timeout=10):
        completed = subprocess.run(
            ["git"] + list(args),
            cwd=cwd or self._root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "git command failed")
        return completed.stdout

    def _run_git_bytes(self, args, cwd=None, timeout=10):
        completed = subprocess.run(
            ["git"] + list(args),
            cwd=cwd or self._root,
            capture_output=True,
            timeout=timeout,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                completed.stderr.decode("utf-8", errors="replace").strip()
                or completed.stdout.decode("utf-8", errors="replace").strip()
                or "git command failed"
            )
        return completed.stdout

    def _git_metadata(self):
        metadata: dict[str, Any] = {
            "available": False,
            "root": None,
            "head": None,
            "head_short": None,
            "branch": None,
            "status": {},
            "tracked_paths": [],
            "dirty_paths": [],
        }
        try:
            git_root = os.path.realpath(self._run_git_text(["rev-parse", "--show-toplevel"]).strip())
            if not (
                git_root == self._root
                or git_root.startswith(self._root + os.sep)
                or self._root.startswith(git_root + os.sep)
            ):
                metadata["error"] = "git root is outside workspace root: " + git_root
                return metadata
            metadata["available"] = True
            metadata["root"] = git_root
            try:
                metadata["head"] = self._run_git_text(["rev-parse", "HEAD"]).strip()
                metadata["head_short"] = self._run_git_text(["rev-parse", "--short", "HEAD"]).strip()
            except Exception as exc:
                metadata["head_error"] = str(exc)
            try:
                metadata["branch"] = self._run_git_text(["rev-parse", "--abbrev-ref", "HEAD"]).strip()
            except Exception as exc:
                metadata["branch_error"] = str(exc)
            porcelain = self._filter_git_porcelain_v1(self._run_git_text(["status", "--porcelain=v1"]))
            porcelain_v2 = self._filter_git_porcelain_v2(
                self._run_git_text(["status", "--porcelain=v2", "--branch"])
            )
            staged = self._split_git_z(self._run_git_text(["diff", "--name-only", "--cached", "-z"]))
            modified = self._split_git_z(self._run_git_text(["diff", "--name-only", "-z"]))
            deleted = self._split_git_z(self._run_git_text(["ls-files", "--deleted", "-z"]))
            untracked = self._split_git_z(self._run_git_text(["ls-files", "--others", "--exclude-standard", "-z"]))
            tracked = self._split_git_z(self._run_git_text(["ls-files", "-z"]))
        except Exception as exc:
            metadata["error"] = str(exc)
            return metadata

        def to_workspace_rel(git_path):
            resolved = os.path.realpath(os.path.join(git_root, git_path))
            if resolved != self._root and not resolved.startswith(self._root + os.sep):
                return None
            return self._relative(resolved)

        tracked_paths = {}
        for git_path in tracked:
            rel = to_workspace_rel(git_path)
            if rel and not self._is_restricted_rel(rel):
                tracked_paths[rel] = git_path.replace("\\", "/")

        dirty_map: dict[str, dict[str, Any]] = {}
        for label, paths in (
            ("staged", staged),
            ("modified", modified),
            ("deleted", deleted),
            ("untracked", untracked),
        ):
            for git_path in paths:
                rel = to_workspace_rel(git_path)
                if not rel or self._is_restricted_rel(rel):
                    continue
                entry = dirty_map.setdefault(
                    rel,
                    {
                        "path": rel,
                        "git_path": git_path.replace("\\", "/"),
                        "statuses": [],
                        "tracked": rel in tracked_paths,
                    },
                )
                if label not in entry["statuses"]:
                    entry["statuses"].append(label)

        dirty_paths = []
        for rel in sorted(dirty_map):
            entry = dirty_map[rel]
            entry["statuses"].sort()
            dirty_paths.append(entry)

        def visible_workspace_rels(paths):
            result = []
            for path in paths:
                rel = to_workspace_rel(path)
                if rel and not self._is_restricted_rel(rel):
                    result.append(rel)
            return result

        metadata["status"] = {
            "clean": not bool(porcelain.strip()),
            "porcelain": porcelain,
            "porcelain_v2": porcelain_v2,
            "staged": visible_workspace_rels(staged),
            "modified": visible_workspace_rels(modified),
            "deleted": visible_workspace_rels(deleted),
            "untracked": visible_workspace_rels(untracked),
        }
        metadata["tracked_paths"] = [
            {"path": rel, "git_path": git_path}
            for rel, git_path in sorted(tracked_paths.items())
        ]
        metadata["dirty_paths"] = dirty_paths
        return metadata

    def _git_status_for_path(self, git_metadata):
        status_by_path = {}
        tracked_by_path = {}
        for item in git_metadata.get("tracked_paths", []):
            if isinstance(item, dict) and item.get("path"):
                tracked_by_path[str(item["path"])] = str(item.get("git_path") or item["path"])
        for item in git_metadata.get("dirty_paths", []):
            if isinstance(item, dict) and item.get("path"):
                status_by_path[str(item["path"])] = item
        return tracked_by_path, status_by_path

    def _append_manifest_dir(self, entries_by_path, rel):
        rel = self._normalize_rel(rel)
        if rel == "." or self._is_restricted_rel(rel) or rel in entries_by_path:
            return
        resolved = self._resolve(rel)
        if not os.path.isdir(resolved) or not self._is_inside_root(resolved):
            return
        entry = {
            "path": rel,
            "type": "dir",
        }
        stat = self._safe_stat(resolved)
        if stat:
            entry["mtime_ns"] = stat["mtime_ns"]
            entry["mode"] = stat["mode"]
        entries_by_path[rel] = entry

    def _append_manifest_file(self, entries_by_path, rel, tracked_by_path, status_by_path, git_available=False):
        rel = self._normalize_rel(rel)
        if self._is_restricted_rel(rel):
            return
        try:
            resolved = self._resolve(rel)
        except ValueError:
            return
        if not os.path.isfile(resolved) or not self._is_inside_root(resolved):
            return
        parts = rel.split("/")
        for index in range(1, len(parts)):
            self._append_manifest_dir(entries_by_path, "/".join(parts[:index]))
        stat = self._safe_stat(resolved)
        entry = {
            "path": rel,
            "type": "file",
            "size": stat.get("size", 0),
            "mtime_ns": stat.get("mtime_ns"),
            "mode": stat.get("mode"),
            "sha256": self._sha256_file(resolved),
        }
        if git_available:
            git_path = tracked_by_path.get(rel)
            dirty = status_by_path.get(rel)
            entry["git"] = {
                "tracked": rel in tracked_by_path,
                "dirty": dirty is not None or rel not in tracked_by_path,
                "git_path": git_path or rel,
                "statuses": list(dirty.get("statuses", [])) if dirty else (
                    ["untracked"] if rel not in tracked_by_path else []
                ),
            }
        entries_by_path[rel] = entry

    def _git_manifest_candidates(self, tracked_by_path, status_by_path, selected_rels):
        selected_rels = [self._normalize_rel(rel) for rel in (selected_rels or ["."])]
        candidates = set(status_by_path)
        if "." in selected_rels:
            candidates.update(tracked_by_path)
        else:
            candidates.update(
                rel
                for rel in tracked_by_path
                if self._is_selected(rel, selected_rels)
            )
            candidates.update(rel for rel in selected_rels if rel != ".")
        return sorted(candidate for candidate in candidates if not self._is_restricted_rel(candidate))

    def _worktree_manifest(self, git_metadata=None, selected_rels=None):
        git_metadata = git_metadata or {}
        tracked_by_path, status_by_path = self._git_status_for_path(git_metadata)
        selected_rels = [self._normalize_rel(rel) for rel in (selected_rels or ["."])]
        if git_metadata.get("available"):
            entries_by_path: dict[str, dict[str, Any]] = {}
            for rel in self._git_manifest_candidates(tracked_by_path, status_by_path, selected_rels):
                self._append_manifest_file(
                    entries_by_path,
                    rel,
                    tracked_by_path,
                    status_by_path,
                    git_available=True,
                )
            return [
                entries_by_path[path]
                for path in sorted(entries_by_path, key=lambda item: (item.count("/"), item))
            ]

        entries = []
        for dirpath, dirnames, filenames in os.walk(self._root):
            dirnames[:] = [
                dirname
                for dirname in sorted(dirnames)
                if not self._is_restricted_rel(self._relative(os.path.join(dirpath, dirname)))
                and not self._is_skipped_checkpoint_dir(
                    self._relative(os.path.join(dirpath, dirname)),
                    selected_rels,
                )
                and self._is_inside_root(os.path.join(dirpath, dirname))
            ]
            for dirname in dirnames:
                resolved = os.path.join(dirpath, dirname)
                rel = self._relative(resolved)
                entry = {
                    "path": rel,
                    "type": "dir",
                }
                stat = self._safe_stat(resolved)
                if stat:
                    entry["mtime_ns"] = stat["mtime_ns"]
                    entry["mode"] = stat["mode"]
                entries.append(entry)
            for filename in sorted(filenames):
                resolved = os.path.join(dirpath, filename)
                rel = self._relative(resolved)
                if (
                    self._is_restricted_rel(rel)
                    or self._is_skipped_checkpoint_dir(rel, selected_rels)
                    or not os.path.isfile(resolved)
                    or not self._is_inside_root(resolved)
                ):
                    continue
                stat = self._safe_stat(resolved)
                entry = {
                    "path": rel,
                    "type": "file",
                    "size": stat.get("size", 0),
                    "mtime_ns": stat.get("mtime_ns"),
                    "mode": stat.get("mode"),
                    "sha256": self._sha256_file(resolved),
                }
                entries.append(entry)
        return entries

    def _path_entry(self, item, include_missing=False):
        resolved = self._resolve_user_path(item, operation="checkpoint")
        rel = self._relative(resolved)
        entry = {
            "path": rel,
            "requested_path": str(item),
            "existed": os.path.exists(resolved),
            "is_dir": os.path.isdir(resolved),
            "is_file": os.path.isfile(resolved),
        }
        if os.path.isfile(resolved):
            entry["size"] = os.path.getsize(resolved)
            entry["sha256"] = self._sha256_file(resolved)
        elif include_missing and not os.path.exists(resolved):
            entry["missing"] = True
        return entry

    def _copy_snapshot_content(self, snapshot_root, rel, captured_from):
        if self._is_restricted_rel(rel):
            return False
        resolved = self._resolve(rel)
        if not os.path.isfile(resolved):
            return False
        destination = os.path.join(snapshot_root, SNAPSHOT_CONTENT_DIR, rel)
        parent = os.path.dirname(destination)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        shutil.copy2(resolved, destination)
        captured_from.append({
            "path": rel,
            "size": os.path.getsize(resolved),
            "sha256": self._sha256_file(resolved),
        })
        return True

    def _capture_worktree_contents(self, snapshot_root, worktree_entries, git_metadata):
        captured: list[dict[str, Any]] = []
        missing_dirty = []
        files_by_path = {
            entry["path"]: entry
            for entry in worktree_entries
            if entry.get("type") == "file" and entry.get("path")
        }
        if git_metadata.get("available"):
            candidate_paths = {
                entry["path"]
                for entry in worktree_entries
                if entry.get("type") == "file"
                and (
                    not entry.get("git", {}).get("tracked")
                    or entry.get("git", {}).get("dirty")
                )
            }
            for dirty in git_metadata.get("dirty_paths", []):
                if not isinstance(dirty, dict) or not dirty.get("path"):
                    continue
                rel = str(dirty["path"])
                if rel not in files_by_path:
                    missing_dirty.append(dict(dirty))
                else:
                    candidate_paths.add(rel)
        else:
            candidate_paths = set(files_by_path)

        for rel in sorted(candidate_paths):
            if self._is_restricted_rel(rel):
                continue
            self._copy_snapshot_content(snapshot_root, rel, captured)
        return captured, missing_dirty

    def _read_terminal_log(self, limit=COMMAND_LOG_LIMIT):
        log_path = self._resolve(os.path.join(SNAPSHOT_DIR, TERMINAL_LOG))
        if not os.path.isfile(log_path):
            return []
        try:
            with open(log_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        entries = []
        for line in lines[-int(limit or COMMAND_LOG_LIMIT):]:
            try:
                loaded = json.loads(line)
            except ValueError:
                continue
            if isinstance(loaded, dict):
                entries.append(loaded)
        return entries

    def read_file(self, path):
        """ファイルを読み取り、内容を文字列で返す。"""
        resolved = self._resolve_user_path(path, operation="read")
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"File not found: {path}")
        self._ensure_text_size(resolved)
        if self._looks_binary(resolved):
            raise ValueError("Binary file cannot be read as text: " + str(path))
        with open(resolved, "r", encoding="utf-8") as f:
            return f.read()

    def read_file_lines(self, path, start_line=None, end_line=None):
        """Read a 1-based inclusive line window from a text file."""
        resolved = self._resolve_user_path(path, operation="read")
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"File not found: {path}")
        self._ensure_text_size(resolved)
        if self._looks_binary(resolved):
            raise ValueError("Binary file cannot be read as text: " + str(path))
        with open(resolved, "r", encoding="utf-8") as f:
            lines = f.readlines()
        total_lines = len(lines)
        start = int(start_line or 1)
        end = int(end_line or total_lines)
        start_index = max(0, start - 1)
        end_index = max(start_index, end)
        content = "".join(lines[start_index:end_index])
        actual_end = min(total_lines, end)
        return {
            "content": content,
            "start_line": start,
            "end_line": actual_end,
            "total_lines": total_lines,
            "truncated": start > 1 or actual_end < total_lines,
        }

    def write_file(self, path, content):
        """ファイルに書き込み、書き込んだバイト数を返す。

        親ディレクトリが存在しない場合は自動作成する。
        """
        resolved = self._resolve_user_path(path, operation="write")
        self._ensure_unprotected_mutation(resolved)
        parent = os.path.dirname(resolved)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        encoded = content.encode("utf-8")
        fd, tmp_path = tempfile.mkstemp(prefix=".rumi-write-", dir=parent or self._root)
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(encoded)
            os.replace(tmp_path, resolved)
        finally:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        return len(encoded)

    def write_file_atomic(self, path, content):
        """Write a text file using the same atomic path as write_file."""
        return self.write_file(path, content)

    def checkpoint_before_mutation(self, operation, paths, metadata=None):
        """Create a reversible checkpoint before a workspace mutation."""
        clean_paths = []
        for path in paths if isinstance(paths, list) else [paths]:
            if path is not None:
                clean_paths.append(str(path))
        checkpoint_metadata = {
            "operation": str(operation or "mutation"),
            "kind": "pre_mutation",
        }
        if isinstance(metadata, dict):
            checkpoint_metadata.update(metadata)
        return self.worktree_checkpoint(
            paths=clean_paths or ["."],
            metadata=checkpoint_metadata,
            include_missing=True,
        )

    def preview_write(self, path, content):
        return {
            "path": path,
            "diff": self.diff_text(path, content),
        }

    def create_file(self, path, content=""):
        """ファイルを新規作成する。既に存在する場合はエラー。

        親ディレクトリが存在しない場合は自動作成する。
        """
        resolved = self._resolve_user_path(path, operation="create")
        self._ensure_unprotected_mutation(resolved)
        if os.path.exists(resolved):
            raise FileExistsError(f"File already exists: {path}")
        parent = os.path.dirname(resolved)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(resolved, "w", encoding="utf-8") as f:
            f.write(content)

    def delete_file(self, path):
        """ファイルを削除する。"""
        resolved = self._resolve_user_path(path, operation="delete")
        self._ensure_unprotected_mutation(resolved)
        if not os.path.isfile(resolved):
            raise FileNotFoundError(f"File not found: {path}")
        os.remove(resolved)

    def safe_delete(self, path):
        snapshot = self.snapshot([path])
        self.delete_file(path)
        return {"path": path, "deleted": True, "snapshot": snapshot}

    def move_file(self, source, destination):
        """ファイルまたはディレクトリを移動する。"""
        resolved_source = self._resolve_user_path(source, operation="move")
        resolved_destination = self._resolve_user_path(destination, operation="move")
        self._ensure_unprotected_mutation(resolved_source)
        self._ensure_unprotected_mutation(resolved_destination)
        if not os.path.exists(resolved_source):
            raise FileNotFoundError(f"Path not found: {source}")
        parent = os.path.dirname(resolved_destination)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        shutil.move(resolved_source, resolved_destination)
        return {
            "source": source,
            "destination": destination,
            "moved": True,
        }

    def diff_text(self, path, new_content):
        """既存ファイルと新しい内容の unified diff を返す。"""
        old_content = ""
        resolved = self._resolve_user_path(path, operation="diff")
        if os.path.exists(resolved):
            if not os.path.isfile(resolved):
                raise IsADirectoryError(f"Path is not a file: {path}")
            old_content = self.read_file(path)
        old_lines = old_content.splitlines(keepends=True)
        new_lines = str(new_content).splitlines(keepends=True)
        return "".join(
            difflib.unified_diff(
                old_lines,
                new_lines,
                fromfile=path + " (current)",
                tofile=path + " (proposed)",
            )
        )

    def apply_patch_text(self, path, old, new):
        """単純な old/new 置換パッチを適用する。"""
        content = self.read_file(path)
        if old not in content:
            raise ValueError("Patch old text was not found in file: " + path)
        updated = content.replace(old, new, 1)
        size = self.write_file(path, updated)
        return {
            "path": path,
            "patched": True,
            "size": size,
            "diff": "".join(
                difflib.unified_diff(
                    content.splitlines(keepends=True),
                    updated.splitlines(keepends=True),
                    fromfile=path + " (before)",
                    tofile=path + " (after)",
                )
            ),
        }

    def _snapshot_manifest_path(self, snapshot_root):
        return os.path.join(snapshot_root, SNAPSHOT_MANIFEST)

    def _validate_snapshot_id(self, snapshot_id):
        if not isinstance(snapshot_id, str) or not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise ValueError("Invalid snapshot id")

    def _load_snapshot_manifest(self, snapshot_id):
        self._validate_snapshot_id(snapshot_id)
        snapshot_root = self._resolve(os.path.join(SNAPSHOT_DIR, snapshot_id))
        manifest_path = self._snapshot_manifest_path(snapshot_root)
        if not os.path.isfile(manifest_path):
            return {}
        with open(manifest_path, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        return loaded if isinstance(loaded, dict) else {}

    def worktree_checkpoint(self, paths=None, metadata=None, include_missing=False, include_terminal_log=True):
        """Create a whole-worktree checkpoint with restorable dirty content."""
        snapshot_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + str(uuid.uuid4())[:8]
        snapshot_root = self._resolve(os.path.join(SNAPSHOT_DIR, snapshot_id))
        selected = [paths] if isinstance(paths, str) else (paths if paths else ["."])
        path_entries = [self._path_entry(item, include_missing=include_missing) for item in selected]
        selected_rels = [entry["path"] for entry in path_entries if entry.get("path")]
        git_metadata = self._git_metadata()
        worktree_entries = self._worktree_manifest(git_metadata, selected_rels=selected_rels)
        os.makedirs(snapshot_root, exist_ok=True)
        captured, missing_dirty = self._capture_worktree_contents(
            snapshot_root,
            worktree_entries,
            git_metadata,
        )
        command_log = self._read_terminal_log() if include_terminal_log else []
        manifest = {
            "schema_version": WORKTREE_SCHEMA_VERSION,
            "kind": "worktree",
            "snapshot_id": snapshot_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace_root": self._root,
            "paths": path_entries,
            "metadata": dict(metadata or {}) if isinstance(metadata, dict) else {},
            "worktree": {
                "manifest": worktree_entries,
                "captured_files": captured,
                "missing_dirty_paths": missing_dirty,
                "git": git_metadata,
                "terminal": {
                    "available": bool(command_log),
                    "commands": command_log,
                },
            },
        }
        with open(self._snapshot_manifest_path(snapshot_root), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        requested_missing = [
            entry["path"]
            for entry in path_entries
            if entry.get("existed") is False
        ]
        covered = sorted({item["path"] for item in captured}.union(requested_missing))
        return {
            "snapshot_id": snapshot_id,
            "path": self._relative(snapshot_root),
            "kind": "worktree",
            "files": covered,
            "captured_files": [item["path"] for item in captured],
            "manifest_files": len([item for item in worktree_entries if item.get("type") == "file"]),
            "manifest_entries": len(worktree_entries),
            "git": {
                "available": bool(git_metadata.get("available")),
                "head": git_metadata.get("head"),
                "branch": git_metadata.get("branch"),
                "clean": git_metadata.get("status", {}).get("clean"),
                "dirty_paths": [item.get("path") for item in git_metadata.get("dirty_paths", [])],
            },
            "terminal": {
                "commands": len(command_log),
            },
            "metadata": manifest["metadata"],
        }

    def snapshot(self, paths=None, metadata=None, include_missing=False):
        """対象ファイルを workspace 内の .rumi_snapshots にコピーする。"""
        snapshot_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + str(uuid.uuid4())[:8]
        snapshot_root = self._resolve(os.path.join(SNAPSHOT_DIR, snapshot_id))
        os.makedirs(snapshot_root, exist_ok=True)
        selected = paths if paths else ["."]
        copied = []
        entries = []
        for item in selected:
            resolved = self._resolve_user_path(item, operation="snapshot")
            rel = self._relative(resolved)
            entry = {
                "path": rel,
                "requested_path": str(item),
                "existed": os.path.exists(resolved),
                "is_dir": os.path.isdir(resolved),
                "is_file": os.path.isfile(resolved),
            }
            if os.path.isfile(resolved):
                entry["size"] = os.path.getsize(resolved)
            entries.append(entry)
            if not os.path.exists(resolved):
                if include_missing:
                    copied.append(rel)
                continue
            if rel == SNAPSHOT_DIR or rel.startswith(SNAPSHOT_DIR + "/"):
                continue
            destination = os.path.join(snapshot_root, rel)
            parent = os.path.dirname(destination)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(resolved):
                shutil.copytree(
                    resolved,
                    destination,
                    dirs_exist_ok=True,
                    ignore=self._snapshot_ignore,
                )
            else:
                shutil.copy2(resolved, destination)
            copied.append(rel)
        manifest = {
            "snapshot_id": snapshot_id,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace_root": self._root,
            "paths": entries,
            "metadata": dict(metadata or {}) if isinstance(metadata, dict) else {},
        }
        with open(self._snapshot_manifest_path(snapshot_root), "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        return {
            "snapshot_id": snapshot_id,
            "path": self._relative(snapshot_root),
            "files": copied,
            "metadata": manifest["metadata"],
        }

    def _selection_rels(self, paths):
        selected = [paths] if isinstance(paths, str) else (paths if paths else ["."])
        result = []
        for item in selected:
            resolved = self._resolve_user_path(item, operation="restore")
            result.append(self._normalize_rel(self._relative(resolved)))
        return result

    def _snapshot_ignore(self, current_dir, names):
        ignored = []
        for name in names:
            candidate = os.path.join(current_dir, name)
            if not self._is_inside_root(candidate):
                ignored.append(name)
                continue
            if self._is_restricted_rel(self._relative(candidate)):
                ignored.append(name)
        return set(ignored)

    def _is_selected(self, rel, selected_rels):
        rel = self._normalize_rel(rel)
        for selected in selected_rels:
            selected = self._normalize_rel(selected)
            if selected == "." or rel == selected or rel.startswith(selected.rstrip("/") + "/"):
                return True
        return False

    def _content_source_path(self, snapshot_root, rel):
        source = os.path.realpath(os.path.join(snapshot_root, SNAPSHOT_CONTENT_DIR, rel))
        content_root = os.path.realpath(os.path.join(snapshot_root, SNAPSHOT_CONTENT_DIR))
        if source != content_root and not source.startswith(content_root + os.sep):
            raise ValueError("Snapshot content path traversal detected: " + str(rel))
        return source

    def _git_blob_for_checkpoint(self, manifest, entry):
        git_metadata = manifest.get("worktree", {}).get("git", {})
        if not git_metadata.get("available") or not git_metadata.get("head"):
            return None
        git_root = os.path.realpath(git_metadata.get("root") or self._root)
        if not os.path.isdir(git_root):
            return None
        git_info = entry.get("git", {}) if isinstance(entry.get("git"), dict) else {}
        git_path = str(git_info.get("git_path") or entry.get("path") or "").replace("\\", "/")
        if not git_path:
            return None
        try:
            return self._run_git_bytes(
                ["show", "{}:{}".format(git_metadata["head"], git_path)],
                cwd=git_root,
                timeout=30,
            )
        except Exception:
            return None

    def _remove_current_paths_not_in_checkpoint(self, checkpoint_paths, checkpoint_dirs, selected_rels):
        current_entries = self._worktree_manifest({}, selected_rels=selected_rels)
        removed = []
        for entry in sorted(
            current_entries,
            key=lambda item: (str(item.get("path", "")).count("/"), item.get("path", "")),
            reverse=True,
        ):
            rel = entry.get("path")
            if not rel or not self._is_selected(rel, selected_rels) or rel in checkpoint_paths:
                continue
            resolved = self._resolve(rel)
            self._ensure_unprotected_mutation(resolved)
            if entry.get("type") == "file" and os.path.isfile(resolved):
                os.remove(resolved)
                removed.append(rel)
            elif entry.get("type") == "dir" and rel not in checkpoint_dirs and os.path.isdir(resolved):
                try:
                    os.rmdir(resolved)
                    removed.append(rel)
                except OSError:
                    pass
        return removed

    def _restore_worktree_snapshot(self, snapshot_id, snapshot_root, manifest, paths=None):
        worktree = manifest.get("worktree", {})
        entries = [
            entry
            for entry in worktree.get("manifest", [])
            if isinstance(entry, dict) and entry.get("path")
        ]
        selected_rels = self._selection_rels(paths)
        checkpoint_paths = {entry["path"] for entry in entries}
        checkpoint_dirs = {entry["path"] for entry in entries if entry.get("type") == "dir"}
        removed = self._remove_current_paths_not_in_checkpoint(
            checkpoint_paths,
            checkpoint_dirs,
            selected_rels,
        )
        restored = []
        warnings = []

        for entry in sorted(entries, key=lambda item: (item.get("type") != "dir", item.get("path", ""))):
            rel = entry["path"]
            if not self._is_selected(rel, selected_rels):
                continue
            destination = self._resolve_user_path(rel, operation="restore")
            self._ensure_unprotected_mutation(destination)
            if entry.get("type") == "dir":
                if os.path.isfile(destination):
                    os.remove(destination)
                    removed.append(rel)
                os.makedirs(destination, exist_ok=True)
                continue
            if entry.get("type") != "file":
                continue
            parent = os.path.dirname(destination)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(destination):
                shutil.rmtree(destination)
                removed.append(rel)
            source = self._content_source_path(snapshot_root, rel)
            if os.path.isfile(source):
                shutil.copy2(source, destination)
                restored.append(rel)
                continue
            blob = self._git_blob_for_checkpoint(manifest, entry)
            if blob is not None:
                with open(destination, "wb") as handle:
                    handle.write(blob)
                restored.append(rel)
                continue
            warnings.append("No checkpoint content available for " + rel)

        return {
            "snapshot_id": snapshot_id,
            "kind": "worktree",
            "restored": restored,
            "removed": sorted(dict.fromkeys(removed)),
            "warnings": warnings,
        }

    def restore_snapshot(self, snapshot_id, paths=None):
        """snapshot_id から workspace に復元する。"""
        self._validate_snapshot_id(snapshot_id)
        snapshot_root = self._resolve(os.path.join(SNAPSHOT_DIR, snapshot_id))
        if not os.path.isdir(snapshot_root):
            raise FileNotFoundError(f"Snapshot not found: {snapshot_id}")
        manifest = self._load_snapshot_manifest(snapshot_id)
        if manifest.get("kind") == "worktree":
            return self._restore_worktree_snapshot(snapshot_id, snapshot_root, manifest, paths=paths)
        path_entries = {}
        for entry in manifest.get("paths", []):
            if not isinstance(entry, dict):
                continue
            if entry.get("requested_path"):
                path_entries[str(entry["requested_path"])] = entry
            if entry.get("path"):
                path_entries[str(entry["path"])] = entry
        selected = paths if paths else ["."]
        restored = []
        removed = []
        for item in selected:
            source = os.path.realpath(os.path.join(snapshot_root, item))
            if source != snapshot_root and not source.startswith(snapshot_root + os.sep):
                raise ValueError("Snapshot path traversal detected: " + str(item))
            entry = path_entries.get(str(item))
            if entry is None:
                try:
                    entry = path_entries.get(self._relative(self._resolve_user_path(item, operation="restore")))
                except Exception:
                    entry = None
            if entry and entry.get("existed") is False:
                destination = self._resolve_user_path(item, operation="restore")
                self._ensure_unprotected_mutation(destination)
                if os.path.isdir(destination):
                    shutil.rmtree(destination)
                    removed.append(str(item))
                elif os.path.exists(destination):
                    os.remove(destination)
                    removed.append(str(item))
                continue
            if not os.path.exists(source):
                continue
            destination = self._resolve_user_path(item, operation="restore")
            self._ensure_unprotected_mutation(destination)
            parent = os.path.dirname(destination)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            if os.path.isdir(source):
                shutil.copytree(source, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination)
            restored.append(item)
        return {
            "snapshot_id": snapshot_id,
            "restored": restored,
            "removed": removed,
        }

    def list_snapshots(self, limit=50):
        """List workspace snapshots newest-first."""
        root = self._resolve(SNAPSHOT_DIR)
        if not os.path.isdir(root):
            return []
        entries = []
        for name in sorted(os.listdir(root), reverse=True):
            path = os.path.join(root, name)
            if not os.path.isdir(path):
                continue
            manifest = {}
            manifest_path = self._snapshot_manifest_path(path)
            if os.path.isfile(manifest_path):
                try:
                    with open(manifest_path, "r", encoding="utf-8") as handle:
                        loaded = json.load(handle)
                    manifest = loaded if isinstance(loaded, dict) else {}
                except (OSError, ValueError):
                    manifest = {}
            entries.append({
                "snapshot_id": name,
                "path": self._relative(path),
                "kind": manifest.get("kind", "file"),
                "created_at": manifest.get("created_at"),
                "metadata": manifest.get("metadata", {}),
                "paths": manifest.get("paths", []),
                "worktree": {
                    "manifest_entries": len(manifest.get("worktree", {}).get("manifest", [])),
                    "captured_files": len(manifest.get("worktree", {}).get("captured_files", [])),
                    "git": {
                        "available": bool(manifest.get("worktree", {}).get("git", {}).get("available")),
                        "head": manifest.get("worktree", {}).get("git", {}).get("head"),
                        "branch": manifest.get("worktree", {}).get("git", {}).get("branch"),
                    },
                    "terminal_commands": len(
                        manifest.get("worktree", {}).get("terminal", {}).get("commands", [])
                    ),
                } if manifest.get("kind") == "worktree" else None,
            })
            if len(entries) >= int(limit):
                break
        return entries

    def search_files(self, pattern, directory="."):
        """globパターンでファイルを検索し、マッチしたパスのリストを返す。"""
        resolved_dir = self._resolve_user_path(directory, operation="search")
        if not os.path.isdir(resolved_dir):
            raise NotADirectoryError(f"Directory not found: {directory}")
        full_pattern = os.path.join(resolved_dir, pattern)
        matches = glob.glob(full_pattern, recursive=True)
        result = []
        for m in sorted(matches):
            real_m = os.path.realpath(m)
            # ワークスペース外のシンボリックリンク先を除外
            if real_m == self._root or real_m.startswith(self._root + os.sep):
                rel = self._relative(real_m)
                if not self._is_restricted_rel(rel):
                    result.append(rel)
        return result

    def list_files(self, directory=".", recursive=False):
        """ディレクトリ内のファイル一覧を返す。

        各エントリは {"name", "path", "is_dir", "size"} の辞書。
        """
        resolved_dir = self._resolve_user_path(directory, operation="list")
        if not os.path.isdir(resolved_dir):
            raise NotADirectoryError(f"Directory not found: {directory}")
        result = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(resolved_dir):
                dirnames[:] = [
                    dirname
                    for dirname in sorted(dirnames)
                    if self._is_inside_root(os.path.join(dirpath, dirname))
                    and not self._is_restricted_rel(self._relative(os.path.join(dirpath, dirname)))
                ]
                for d in sorted(dirnames):
                    full = os.path.join(dirpath, d)
                    rel = self._relative(full)
                    result.append({
                        "name": d,
                        "path": rel,
                        "is_dir": True,
                        "size": 0,
                    })
                for fname in sorted(filenames):
                    full = os.path.join(dirpath, fname)
                    if not self._is_inside_root(full):
                        continue
                    rel = self._relative(full)
                    if self._is_restricted_rel(rel):
                        continue
                    try:
                        size = os.path.getsize(full)
                    except OSError:
                        size = 0
                    result.append({
                        "name": fname,
                        "path": rel,
                        "is_dir": False,
                        "size": size,
                    })
        else:
            entries = sorted(os.listdir(resolved_dir))
            for entry in entries:
                full = os.path.join(resolved_dir, entry)
                if not self._is_inside_root(full):
                    continue
                rel = self._relative(full)
                if self._is_restricted_rel(rel):
                    continue
                is_dir = os.path.isdir(full)
                try:
                    size = 0 if is_dir else os.path.getsize(full)
                except OSError:
                    size = 0
                result.append({
                    "name": entry,
                    "path": rel,
                    "is_dir": is_dir,
                    "size": size,
                })
        return result
