from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import stat
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from domain.coding.file_ops import CHECKPOINT_SKIPPED_DIRS, FileOps
from domain.coding.workspace_jail import (
    PROTECTED_PATH_PARTS,
    SECRET_FILE_NAMES,
    SECRET_PATH_PARTS,
    SECRET_SUFFIXES,
    WorkspaceJail,
)
from domain.coding.workspace_resolver import WorkspaceResolver
from domain.coding.workspace_store import WorkspaceStore


MAX_SANDBOX_WORKSPACE_FILES = 4000
MAX_SANDBOX_WORKSPACE_TOTAL_BYTES = 64 * 1024 * 1024
MAX_SANDBOX_WORKSPACE_FILE_BYTES = 4 * 1024 * 1024
MAX_SANDBOX_POST_RUN_FILES = 8000
MAX_SANDBOX_POST_RUN_TOTAL_BYTES = 128 * 1024 * 1024
MAX_SANDBOX_TREE_DEPTH = 64
MAX_SANDBOX_DIFF_CHARS = 120_000
MAX_SANDBOX_ARTIFACT_BYTES = 32 * 1024 * 1024
SANDBOX_STATE_SCHEMA = 1
_LOCK_REGISTRY_GUARD = threading.Lock()
_LOCK_REGISTRY: dict[str, threading.RLock] = {}


@dataclass(frozen=True)
class SandboxWorkspace:
    sandbox_id: str
    state_root: Path
    base_root: Path
    work_root: Path
    artifact_root: Path
    host_workspace_root: Path
    workspace_id: str | None = None

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "workspace_id": self.workspace_id,
            "execution_boundary": "sandbox_workspace",
            "sandbox_ephemeral": True,
        }


class SandboxWorkspaceManager:
    """Maintain copy-on-write coding workspaces for sandbox-only tools."""

    def __init__(
        self,
        state_dir: str | os.PathLike[str] | None = None,
        *,
        workspace_store: WorkspaceStore | None = None,
    ) -> None:
        self.state_dir = Path(state_dir) if state_dir is not None else _default_state_dir()
        self._workspace_store = workspace_store

    def prepare(self, input_data: dict[str, Any] | None, context: dict[str, Any] | None) -> SandboxWorkspace:
        args = input_data or {}
        ctx = context or {}
        _reject_external_workspace_root(args)
        _reject_external_workspace_root(ctx)
        if isinstance(ctx.get("inputs"), dict):
            _reject_external_workspace_root(ctx["inputs"])
        if isinstance(ctx.get("profile_policy"), dict):
            _reject_external_workspace_root(ctx["profile_policy"])
        _reject_external_sandbox_id(args)
        workspace_id = _sandbox_workspace_id(args, ctx)
        if not workspace_id:
            raise ValueError("workspace_id is required for sandbox coding")
        resolution = WorkspaceResolver(self._workspace_store).resolve(
            {"workspace_id": workspace_id},
            {},
            allow_cwd_fallback=False,
        )
        _validate_workspace_resolution(resolution, ctx)
        host_root = Path(resolution.root_path).expanduser().resolve()
        if not host_root.is_dir():
            raise ValueError("workspace root must exist")
        owner = _sandbox_owner(ctx, resolution)
        sandbox_id = _sandbox_id(ctx, host_root, owner)
        state_root = (self.state_dir / sandbox_id).resolve()
        base_root = state_root / "base"
        work_root = state_root / "work"
        artifact_root = state_root / "artifacts"
        manifest_path = state_root / "manifest.json"
        with _lock_for_sandbox(state_root):
            reset = args.get("reset") is True or args.get("fresh") is True
            existing_manifest = _read_json(manifest_path)
            if existing_manifest:
                _validate_existing_manifest(existing_manifest, owner, host_root)
            can_reuse = (
                not reset
                and base_root.is_dir()
                and work_root.is_dir()
                and existing_manifest.get("schema") == SANDBOX_STATE_SCHEMA
                and existing_manifest.get("host_workspace_root") == str(host_root)
                and existing_manifest.get("owner") == owner
            )
            if not can_reuse:
                if state_root.exists():
                    shutil.rmtree(state_root)
                state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                stage_audit = _stage_workspace(host_root, base_root, include_paths=args.get("include_paths"))
                shutil.copytree(base_root, work_root, symlinks=False)
                artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                _write_json(
                    manifest_path,
                    {
                        "schema": SANDBOX_STATE_SCHEMA,
                        "sandbox_id": sandbox_id,
                        "host_workspace_root": str(host_root),
                        "workspace_id": resolution.workspace_id,
                        "owner": owner,
                        "created_at": _now(),
                        "updated_at": _now(),
                        "stage_audit": stage_audit,
                    },
                )
            else:
                artifact_root.mkdir(mode=0o700, parents=True, exist_ok=True)
                existing_manifest["updated_at"] = _now()
                _write_json(manifest_path, existing_manifest)
        return SandboxWorkspace(
            sandbox_id=sandbox_id,
            state_root=state_root,
            base_root=base_root,
            work_root=work_root,
            artifact_root=artifact_root,
            host_workspace_root=host_root,
            workspace_id=resolution.workspace_id,
        )

    def read_file(
        self,
        workspace: SandboxWorkspace,
        path: Any,
        *,
        start_line: Any = None,
        end_line: Any = None,
        max_chars: Any = None,
    ) -> dict[str, Any]:
        ops = FileOps(workspace.work_root)
        if start_line is not None or end_line is not None:
            window = ops.read_file_lines(path, start_line=_optional_int(start_line), end_line=_optional_int(end_line))
            content = window["content"]
            payload = {
                "path": str(path),
                "content": content,
                "size": len(content.encode("utf-8")),
                "encoding": "utf-8",
                **window,
            }
        else:
            content = ops.read_file(path)
            payload = {
                "path": str(path),
                "content": content,
                "size": len(content.encode("utf-8")),
                "encoding": "utf-8",
            }
        clipped, truncated, omitted = _clip_text(payload["content"], _max_chars(max_chars))
        if truncated:
            payload["content"] = clipped
            payload["truncated"] = True
            payload["omitted_chars"] = omitted
        return {**payload, **workspace.to_public_dict()}

    def write_file(self, workspace: SandboxWorkspace, path: Any, content: Any) -> dict[str, Any]:
        text = _bounded_content(content)
        ops = FileOps(workspace.work_root)
        before_diff = ops.diff_text(path, text)
        size = ops.write_file(path, text)
        preview = self.diff_preview(workspace)
        return {
            "path": str(path),
            "size": size,
            "written": True,
            "host_modified": False,
            "sandbox_only": True,
            "diff": before_diff,
            **_change_summary(preview),
            **workspace.to_public_dict(),
        }

    def patch_file(self, workspace: SandboxWorkspace, path: Any, old: Any, new: Any) -> dict[str, Any]:
        _bounded_content(new)
        ops = FileOps(workspace.work_root)
        result = ops.apply_patch_text(path, str(old), str(new))
        preview = self.diff_preview(workspace)
        return {
            **result,
            "host_modified": False,
            "sandbox_only": True,
            **_change_summary(preview),
            **workspace.to_public_dict(),
        }

    def diff_preview(self, workspace: SandboxWorkspace, *, max_chars: Any = None) -> dict[str, Any]:
        budget = _max_chars(max_chars) or MAX_SANDBOX_DIFF_CHARS
        changed = _changed_files(workspace.base_root, workspace.work_root)
        diff_parts: list[str] = []
        truncated = False
        for item in changed:
            if len("".join(diff_parts)) >= budget:
                truncated = True
                break
            text = _unified_diff_for_change(workspace.base_root, workspace.work_root, item, budget - len("".join(diff_parts)))
            if text:
                diff_parts.append(text)
        diff_text = "".join(diff_parts)
        if len(diff_text) > budget:
            diff_text = diff_text[: max(0, budget - 28)].rstrip() + "\n[diff truncated]\n"
            truncated = True
        return {
            "changed_files": changed,
            "changed_file_count": len(changed),
            "diff": diff_text,
            "diff_truncated": truncated,
            "diff_summary": _diff_summary(changed),
            "host_modified": False,
            "sandbox_only": True,
            **workspace.to_public_dict(),
        }

    def validate_post_run(self, workspace: SandboxWorkspace) -> dict[str, Any]:
        return _audit_post_run_tree(workspace.work_root)

    def export_artifacts(self, workspace: SandboxWorkspace, paths: Any = None) -> dict[str, Any]:
        changed = self.diff_preview(workspace)["changed_files"]
        requested = _artifact_paths(paths, changed)
        export_id = "art_" + uuid.uuid4().hex[:12]
        export_root = workspace.artifact_root / export_id
        export_root.mkdir(mode=0o700, parents=True, exist_ok=False)
        copied: list[dict[str, Any]] = []
        total_bytes = 0
        jail = WorkspaceJail(workspace.work_root)
        for raw_path in requested:
            source = jail.resolve_user_path(raw_path)
            rel = jail.relative(source)
            jail.ensure_allowed(rel, operation="artifact_export")
            source_kind = _safe_path_kind(source)
            if source_kind is None:
                continue
            target = export_root / rel
            if source_kind == "dir":
                bytes_used, files = _copy_artifact_tree(source, target, total_bytes)
                total_bytes += bytes_used
                copied.extend(files)
            elif source_kind == "file":
                file_stat = _safe_regular_stat(source)
                size = file_stat.st_size if file_stat else 0
                if total_bytes + size > MAX_SANDBOX_ARTIFACT_BYTES:
                    raise ValueError("sandbox artifact export is too large")
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                _safe_copy_regular_file(source, target)
                total_bytes += size
                copied.append({"path": rel, "artifact_ref": rel, "size": size})
        return {
            "artifact_id": export_id,
            "artifact_paths": [item["path"] for item in copied],
            "files": copied,
            "total_bytes": total_bytes,
            "host_modified": False,
            "sandbox_only": True,
            **workspace.to_public_dict(),
        }


def _default_state_dir() -> Path:
    override = os.environ.get("RUMI_SANDBOX_CODING_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2] / "user_data" / "shared" / "sandbox_coding"


def _lock_for_sandbox(state_root: Path) -> threading.RLock:
    key = str(state_root.expanduser().resolve())
    with _LOCK_REGISTRY_GUARD:
        lock = _LOCK_REGISTRY.get(key)
        if lock is None:
            lock = threading.RLock()
            _LOCK_REGISTRY[key] = lock
        return lock


def _reject_external_workspace_root(args: dict[str, Any]) -> None:
    for key in ("workspace_root", "cwd"):
        if args.get(key) not in (None, ""):
            raise ValueError(f"{key} is not accepted by sandbox coding; use workspace_id")


def _reject_external_sandbox_id(args: dict[str, Any]) -> None:
    for key in ("sandbox_id", "sandbox_workspace_id"):
        if args.get(key) not in (None, ""):
            raise ValueError(f"{key} is assigned by the server")


def _sandbox_workspace_id(args: dict[str, Any], context: dict[str, Any]) -> str:
    for source in (args, context):
        value = str(source.get("workspace_id") or "").strip()
        if value:
            return value
    return ""


def _validate_workspace_resolution(resolution: Any, context: dict[str, Any]) -> None:
    host_root = Path(resolution.root_path).expanduser().resolve()
    if host_root == Path("/").resolve():
        raise ValueError("workspace root cannot be filesystem root")
    try:
        if host_root == Path.home().resolve():
            raise ValueError("workspace root cannot be the user home directory")
    except RuntimeError:
        pass
    if getattr(resolution, "workspace_id", None) and not bool(getattr(resolution, "trusted", False)):
        raise ValueError("workspace must be trusted before sandbox coding can stage it")
    record = getattr(resolution, "record", None)
    raw_metadata = record.get("metadata") if isinstance(record, dict) else None
    metadata = raw_metadata if isinstance(raw_metadata, dict) else {}
    owner = str(metadata.get("owner_profile_id") or metadata.get("profile_id") or "").strip()
    profile_id = _profile_id(context)
    if owner and profile_id and owner != profile_id:
        raise ValueError("workspace belongs to a different profile")


def _sandbox_owner(context: dict[str, Any], resolution: Any) -> dict[str, Any]:
    return {
        "profile_id": _profile_id(context),
        "conversation_id": str(context.get("conversation_id") or context.get("chat_id") or "").strip(),
        "workspace_id": str(getattr(resolution, "workspace_id", "") or "").strip(),
    }


def _profile_id(context: dict[str, Any]) -> str:
    principal = context.get("_authenticated_principal")
    if isinstance(principal, dict) and str(principal.get("profile_id") or "").strip():
        return str(principal.get("profile_id") or "").strip()
    for key in ("profile_id", "principal_id"):
        value = str(context.get(key) or "").strip()
        if value:
            return value.split(":", 1)[1].split("__", 1)[0] if value.startswith("profile:") else value
    return "default"


def _sandbox_id(context: dict[str, Any], host_root: Path, owner: dict[str, Any]) -> str:
    session_id = _sandbox_session_id(context)
    seed = json.dumps(
        {
            "host_root": str(host_root),
            "owner": owner,
            "pack_id": str(context.get("pack_id") or context.get("_source_pack_id") or ""),
            "session_id": session_id,
        },
        sort_keys=True,
    )
    candidate = "sbx_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    safe = "".join(ch if ch.isalnum() or ch in "_.:-" else "_" for ch in candidate)[:96]
    return safe if safe.startswith("sbx_") else "sbx_" + safe


def _sandbox_session_id(context: dict[str, Any]) -> str:
    for key in ("_sandbox_session_id",):
        value = str(context.get(key) or "").strip()
        if _is_safe_opaque_id(value):
            return value
    value = "sess_" + uuid.uuid4().hex
    try:
        context["_sandbox_session_id"] = value
    except Exception:
        pass
    return value


def _is_safe_opaque_id(value: str) -> bool:
    return bool(value) and len(value) <= 96 and "/" not in value and "\x00" not in value


def _validate_existing_manifest(manifest: dict[str, Any], owner: dict[str, Any], host_root: Path) -> None:
    if manifest.get("schema") != SANDBOX_STATE_SCHEMA:
        return
    if manifest.get("owner") != owner:
        raise ValueError("sandbox workspace belongs to a different owner")
    if manifest.get("host_workspace_root") != str(host_root):
        raise ValueError("sandbox workspace root changed; create a new sandbox")


def _stage_workspace(source_root: Path, target_root: Path, *, include_paths: Any = None) -> dict[str, int]:
    target_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    roots = _selected_stage_roots(source_root, include_paths)
    audit = {"files": 0, "bytes": 0, "skipped": 0}
    for source in roots:
        if source.is_file():
            _stage_file(source_root, source, target_root, audit)
        elif source.is_dir():
            for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
                current_path = Path(current)
                rel_dir = current_path.relative_to(source_root)
                dirs[:] = [
                    name
                    for name in dirs
                    if not _should_skip_rel((rel_dir / name).as_posix())
                    and not _is_special_or_link(current_path / name)
                ]
                (target_root / rel_dir).mkdir(mode=0o700, parents=True, exist_ok=True)
                for file_name in files:
                    file_path = current_path / file_name
                    if _should_skip_rel((rel_dir / file_name).as_posix()) or _is_special_or_link(file_path):
                        audit["skipped"] += 1
                        continue
                    _stage_file(source_root, file_path, target_root, audit)
    return audit


def _selected_stage_roots(source_root: Path, include_paths: Any) -> list[Path]:
    if include_paths in (None, "", []):
        return [source_root]
    values = include_paths if isinstance(include_paths, list) else [include_paths]
    jail = WorkspaceJail(source_root)
    roots: list[Path] = []
    for value in values:
        resolved = jail.resolve_user_path(value)
        rel = jail.relative(resolved)
        jail.ensure_allowed(rel, operation="sandbox_stage")
        roots.append(resolved)
    return roots or [source_root]


def _stage_file(source_root: Path, source: Path, target_root: Path, audit: dict[str, int]) -> None:
    stat_result = _safe_regular_stat(source)
    if stat_result is None:
        audit["skipped"] += 1
        return
    size = int(stat_result.st_size)
    if size > MAX_SANDBOX_WORKSPACE_FILE_BYTES:
        audit["skipped"] += 1
        return
    if audit["files"] + 1 > MAX_SANDBOX_WORKSPACE_FILES:
        raise ValueError("sandbox workspace has too many files")
    if audit["bytes"] + size > MAX_SANDBOX_WORKSPACE_TOTAL_BYTES:
        raise ValueError("sandbox workspace is too large")
    rel = source.relative_to(source_root)
    target = target_root / rel
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _safe_copy_regular_file(source, target)
    os.chmod(target, stat_result.st_mode & 0o700)
    audit["files"] += 1
    audit["bytes"] += size


def _audit_post_run_tree(root: Path) -> dict[str, Any]:
    audit = {"files": 0, "bytes": 0, "max_depth": 0}
    for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_dir = current_path.relative_to(root)
        depth = 0 if rel_dir == Path(".") else len(rel_dir.parts)
        if depth > MAX_SANDBOX_TREE_DEPTH:
            raise ValueError("sandbox workspace tree is too deep")
        audit["max_depth"] = max(audit["max_depth"], depth)
        safe_dirs: list[str] = []
        for name in dirs:
            child = current_path / name
            if _safe_path_kind(child) != "dir":
                raise ValueError("sandbox workspace contains symlink or special directory: " + (rel_dir / name).as_posix())
            safe_dirs.append(name)
        dirs[:] = safe_dirs
        for name in files:
            path = current_path / name
            rel = (rel_dir / name).as_posix()
            stat_result = _safe_regular_stat(path)
            if stat_result is None:
                raise ValueError("sandbox workspace contains symlink, special, linked, or oversized file: " + rel)
            audit["files"] += 1
            audit["bytes"] += int(stat_result.st_size)
            if audit["files"] > MAX_SANDBOX_POST_RUN_FILES:
                raise ValueError("sandbox workspace has too many files after execution")
            if audit["bytes"] > MAX_SANDBOX_POST_RUN_TOTAL_BYTES:
                raise ValueError("sandbox workspace is too large after execution")
    return audit


def _should_skip_rel(rel: str) -> bool:
    parts = tuple(part for part in str(rel or "").replace("\\", "/").split("/") if part)
    if not parts:
        return False
    if any(part in PROTECTED_PATH_PARTS for part in parts):
        return True
    if any(part in SECRET_PATH_PARTS for part in parts):
        return True
    if parts[0] in CHECKPOINT_SKIPPED_DIRS:
        return True
    name = parts[-1].lower()
    if name == ".env" or (name.startswith(".env.") and not name.endswith((".example", ".sample", ".template"))):
        return True
    if name in SECRET_FILE_NAMES or name.endswith(SECRET_SUFFIXES):
        return True
    return False


def _is_special_or_link(path: Path) -> bool:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return True
    return not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)) or stat.S_ISLNK(mode)


def _safe_path_kind(path: Path) -> str | None:
    try:
        mode = path.lstat().st_mode
    except OSError:
        return None
    if stat.S_ISLNK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode) or stat.S_ISCHR(mode) or stat.S_ISBLK(mode):
        return None
    if stat.S_ISDIR(mode):
        return "dir"
    if stat.S_ISREG(mode):
        return "file"
    return None


def _safe_regular_stat(path: Path) -> os.stat_result | None:
    try:
        stat_result = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode) or stat_result.st_nlink > 1:
        return None
    if stat_result.st_size > MAX_SANDBOX_WORKSPACE_FILE_BYTES:
        return None
    return stat_result


def _safe_read_regular_file(path: Path, *, max_bytes: int) -> bytes:
    stat_result = _safe_regular_stat(path)
    if stat_result is None:
        raise ValueError("not a safe regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened_stat = os.fstat(fd)
        if not stat.S_ISREG(opened_stat.st_mode) or opened_stat.st_nlink > 1:
            raise ValueError("not a safe regular file")
        if opened_stat.st_size > max_bytes:
            raise ValueError("file is too large")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            raw = handle.read(max_bytes + 1)
        if len(raw) > max_bytes:
            raise ValueError("file is too large")
        return raw
    finally:
        os.close(fd)


def _safe_copy_regular_file(source: Path, target: Path) -> None:
    raw = _safe_read_regular_file(source, max_bytes=MAX_SANDBOX_WORKSPACE_FILE_BYTES)
    if len(raw) > MAX_SANDBOX_WORKSPACE_FILE_BYTES:
        raise ValueError("file is too large")
    target.write_bytes(raw)


def _changed_files(base_root: Path, work_root: Path) -> list[dict[str, Any]]:
    base_files = _file_map(base_root)
    work_files = _file_map(work_root)
    changed: list[dict[str, Any]] = []
    for rel in sorted(set(base_files) | set(work_files)):
        base = base_files.get(rel)
        work = work_files.get(rel)
        if base is None:
            work_stat = _safe_regular_stat(work) if work else None
            changed.append({"path": rel, "status": "added", "size": work_stat.st_size if work_stat else 0})
        elif work is None:
            base_stat = _safe_regular_stat(base)
            changed.append({"path": rel, "status": "deleted", "size": base_stat.st_size if base_stat else 0})
        elif _sha256(base) != _sha256(work):
            work_stat = _safe_regular_stat(work)
            changed.append({"path": rel, "status": "modified", "size": work_stat.st_size if work_stat else 0})
    return changed


def _file_map(root: Path) -> dict[str, Path]:
    result: dict[str, Path] = {}
    if not root.is_dir():
        return result
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [
            name
            for name in dirs
            if not _should_skip_rel((current_path / name).relative_to(root).as_posix())
            and _safe_path_kind(current_path / name) == "dir"
        ]
        for name in files:
            path = current_path / name
            if _safe_regular_stat(path) is not None:
                result[path.relative_to(root).as_posix()] = path
    return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    raw = _safe_read_regular_file(path, max_bytes=MAX_SANDBOX_WORKSPACE_FILE_BYTES)
    digest.update(raw)
    return digest.hexdigest()


def _unified_diff_for_change(base_root: Path, work_root: Path, change: dict[str, Any], budget: int) -> str:
    if budget <= 0:
        return ""
    rel = str(change.get("path") or "")
    old_path = base_root / rel
    new_path = work_root / rel
    old_text = "" if change.get("status") == "added" else _read_text_for_diff(old_path)
    new_text = "" if change.get("status") == "deleted" else _read_text_for_diff(new_path)
    if old_text is None or new_text is None:
        return f"diff -- {rel}\n[binary or too-large file omitted]\n"
    diff = "".join(
        difflib.unified_diff(
            old_text.splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{rel}",
            tofile=f"b/{rel}",
            lineterm="",
        )
    )
    if not diff.endswith("\n"):
        diff += "\n"
    if len(diff) > budget:
        return diff[: max(0, budget - 28)].rstrip() + "\n[diff truncated]\n"
    return diff


def _read_text_for_diff(path: Path) -> str | None:
    try:
        raw = _safe_read_regular_file(path, max_bytes=MAX_SANDBOX_WORKSPACE_FILE_BYTES)
    except (OSError, ValueError):
        return None
    if b"\0" in raw:
        return None
    return raw.decode("utf-8", errors="replace")


def _diff_summary(changed: list[dict[str, Any]]) -> str:
    if not changed:
        return "Sandbox has no file changes."
    counts: dict[str, int] = {}
    for item in changed:
        status = str(item.get("status") or "modified")
        counts[status] = counts.get(status, 0) + 1
    parts = [f"{count} {status}" for status, count in sorted(counts.items())]
    return "Sandbox changed {} file(s): {}.".format(len(changed), ", ".join(parts))


def _change_summary(preview: dict[str, Any]) -> dict[str, Any]:
    return {
        "changed_files": preview.get("changed_files", []),
        "changed_file_count": preview.get("changed_file_count", 0),
        "diff_summary": preview.get("diff_summary", ""),
    }


def _artifact_paths(paths: Any, changed: list[dict[str, Any]]) -> list[str]:
    if paths in (None, "", []):
        return [str(item.get("path") or "") for item in changed if item.get("status") != "deleted"]
    values = paths if isinstance(paths, list) else [paths]
    return [str(value) for value in values if str(value).strip()]


def _bounded_content(content: Any) -> str:
    text = str(content)
    if len(text.encode("utf-8")) > MAX_SANDBOX_WORKSPACE_FILE_BYTES:
        raise ValueError("sandbox file content is too large")
    return text


def _copy_artifact_tree(source: Path, target: Path, current_bytes: int) -> tuple[int, list[dict[str, Any]]]:
    copied: list[dict[str, Any]] = []
    total = 0
    for current, dirs, files in os.walk(source, topdown=True, followlinks=False):
        current_path = Path(current)
        rel_dir = current_path.relative_to(source)
        dirs[:] = [
            name
            for name in dirs
            if not _should_skip_rel((rel_dir / name).as_posix())
            and _safe_path_kind(current_path / name) == "dir"
        ]
        for name in files:
            item = current_path / name
            item_stat = _safe_regular_stat(item)
            if item_stat is None:
                continue
            rel = rel_dir / name
            size = item_stat.st_size
            if current_bytes + total + size > MAX_SANDBOX_ARTIFACT_BYTES:
                raise ValueError("sandbox artifact export is too large")
            dest = target / rel
            dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _safe_copy_regular_file(item, dest)
            total += size
            copied.append({"path": rel.as_posix(), "artifact_ref": rel.as_posix(), "size": size})
    return total, copied


def _clip_text(text: Any, max_chars: int | None) -> tuple[str, bool, int]:
    content = str(text or "")
    if max_chars is None or len(content) <= max_chars:
        return content, False, 0
    clipped = content[: max(0, max_chars - 24)].rstrip() + "\n[truncated]"
    return clipped, True, len(content) - len(clipped)


def _max_chars(value: Any) -> int | None:
    if value in (None, ""):
        return None
    parsed = int(value)
    if parsed <= 0:
        raise ValueError("max_chars must be > 0")
    return min(max(parsed, 200), MAX_SANDBOX_DIFF_CHARS)


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    return int(value)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp-" + uuid.uuid4().hex[:8])
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
