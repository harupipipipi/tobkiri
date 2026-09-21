"""Copy-on-write coding workspaces with no-downgrade Docker execution."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from core_runtime.bounded_process_runner import (
    BoundedProcessResult,
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from core_runtime.paths import USER_DATA_DIR

AUTHORITY = "rumi.service.host.authorize.v1"
WORKSPACE = "rumi.resource.workspace.v1"
SERVICE_PACK_ID = "rumi_coding_sandbox_service_pack"
_IMAGE = re.compile(r"^[A-Za-z0-9._/-]+@sha256:[0-9a-f]{64}$")
_SKIP_DIRS = {".git", ".rumi_snapshots", "node_modules", ".venv", "target", "dist", "build"}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "id_rsa",
    "id_ed25519",
}
_SECRET_SUFFIXES = {".pem", ".key", ".p12", ".pfx"}
_SECRET_PARTS = {".ssh", ".aws", ".gnupg", ".kube"}
_MAX_FILES = 4_000
_MAX_BYTES = 64 * 1024 * 1024
_MAX_FILE = 4 * 1024 * 1024
_MAX_OUTPUT = 256 * 1024


class CodingSandboxRuntime:
    """Own bounded staged coding sandboxes for one profile."""

    def __init__(self, client: Any, profile_id: str) -> None:
        self.client = client
        self.profile_id = profile_id
        self.root = (
            Path(USER_DATA_DIR) / "packs" / SERVICE_PACK_ID / "profiles" / profile_id / "sandboxes"
        )
        self.lock = threading.RLock()
        self.records: dict[str, dict[str, Any]] = {}

    def observe(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Observe staged content without control authority."""
        sandbox = self._required(str(payload.get("sandbox_id") or ""))
        if name == "get":
            return self._public(sandbox)
        if name == "read":
            path = _jailed(sandbox["work"], payload.get("path"), must_exist=True)
            if not path.is_file() or path.stat().st_size > _MAX_FILE:
                raise ValueError("sandbox file is unavailable or too large")
            return {
                "sandbox_id": sandbox["id"],
                "path": path.relative_to(sandbox["work"]).as_posix(),
                "content": path.read_text(encoding=str(payload.get("encoding") or "utf-8")),
                "host_modified": False,
            }
        if name == "diff":
            return self._diff(sandbox)
        if name == "list":
            with self.lock:
                return {"sandboxes": [self._public(item) for item in self.records.values()]}
        raise ValueError(f"unknown coding sandbox observe operation: {name}")

    def control(self, name: str, payload: Mapping[str, Any]) -> Any:
        """Apply one receipt-gated sandbox mutation or execution."""
        arguments: dict[str, Any]
        if name == "prepare":
            arguments = {
                "workspace_id": str(payload.get("workspace_id") or ""),
                "include_paths": _string_list(payload.get("include_paths")),
            }
        elif name in {"write", "patch"}:
            arguments = {
                "sandbox_id": str(payload.get("sandbox_id") or ""),
                "path": str(payload.get("path") or ""),
                "content": str(payload.get("content") or "") if name == "write" else "",
                "old": str(payload.get("old") or "") if name == "patch" else "",
                "new": str(payload.get("new") or "") if name == "patch" else "",
            }
        elif name == "execute":
            command = payload.get("command")
            if not isinstance(command, list) or not command:
                raise ValueError("sandbox command must be a nonempty argv list")
            arguments = {
                "sandbox_id": str(payload.get("sandbox_id") or ""),
                "image": str(payload.get("image") or ""),
                "command": [str(item) for item in command],
                "timeout": max(1, min(300, int(payload.get("timeout") or 60))),
            }
        elif name == "discard":
            arguments = {"sandbox_id": str(payload.get("sandbox_id") or "")}
        else:
            raise ValueError(f"unknown coding sandbox control operation: {name}")
        workspace_id = str(payload.get("workspace_id") or "")
        if name != "prepare":
            sandbox = self._required(str(arguments["sandbox_id"]))
            workspace_id = sandbox["workspace_id"]
            arguments["workspace_id"] = workspace_id
        self._redeem(name, payload, arguments, workspace_id)
        if name == "prepare":
            return self._prepare(payload, arguments)
        if name == "write":
            return self._write(sandbox, arguments)
        if name == "patch":
            return self._patch(sandbox, arguments)
        if name == "execute":
            return self._execute(sandbox, arguments)
        return self._discard(sandbox)

    def _prepare(self, payload: Mapping[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        mount = self.client.invoke(
            WORKSPACE,
            "get",
            {"profile_id": self.profile_id, "workspace_id": arguments["workspace_id"]},
        )
        if not isinstance(mount, Mapping):
            raise KeyError("workspace mount is unknown")
        source = Path(str(mount.get("root_path") or "")).resolve(strict=True)
        sandbox_id = str(uuid.uuid4())
        state = self.root / sandbox_id
        base = state / "base"
        work = state / "work"
        state.mkdir(mode=0o700, parents=True, exist_ok=False)
        try:
            audit = _stage(source, base, arguments["include_paths"])
            shutil.copytree(base, work, symlinks=False)
        except Exception:
            shutil.rmtree(state, ignore_errors=True)
            raise
        record = {
            "id": sandbox_id,
            "workspace_id": arguments["workspace_id"],
            "source": source,
            "state": state,
            "base": base,
            "work": work,
            "status": "ready",
            "stage_audit": audit,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self.lock:
            self.records[sandbox_id] = record
        return self._public(record)

    def _write(self, sandbox: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        data = str(arguments["content"]).encode("utf-8")
        if len(data) > _MAX_FILE:
            raise ValueError("sandbox file exceeds size limit")
        path = _jailed(sandbox["work"], arguments["path"], must_exist=False)
        _atomic(path, data)
        sandbox["updated_at"] = time.time()
        return {
            "sandbox_id": sandbox["id"],
            "path": arguments["path"],
            "size": len(data),
            "host_modified": False,
        }

    def _patch(self, sandbox: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        path = _jailed(sandbox["work"], arguments["path"], must_exist=True)
        before = path.read_text(encoding="utf-8")
        old = str(arguments["old"])
        if not old or before.count(old) != 1:
            raise ValueError("sandbox patch old text must match exactly once")
        after = before.replace(old, str(arguments["new"]), 1)
        _atomic(path, after.encode("utf-8"))
        sandbox["updated_at"] = time.time()
        return {
            "sandbox_id": sandbox["id"],
            "path": arguments["path"],
            "patched": True,
            "host_modified": False,
        }

    def _execute(self, sandbox: dict[str, Any], arguments: Mapping[str, Any]) -> dict[str, Any]:
        image = str(arguments["image"])
        if not _IMAGE.fullmatch(image):
            raise ValueError("sandbox image must be digest pinned")
        docker = _docker_executable()
        inspect = _run_docker_command(
            (docker, "image", "inspect", image),
            cwd=sandbox["work"],
            timeout_seconds=20,
        )
        if inspect.timed_out or inspect.exit_code != 0:
            raise RuntimeError("pinned sandbox image is not available locally")
        container_name = "rumi-coding-" + sandbox["id"].replace("-", "")[:20]
        command = (
            docker,
            "run",
            "--rm",
            "--name",
            container_name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "256",
            "--memory",
            "1g",
            "--cpus",
            "2",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--mount",
            f"type=bind,src={sandbox['work']},dst=/workspace,rw",
            "--workdir",
            "/workspace",
            image,
            *arguments["command"],
        )
        completed = _run_docker_command(
            command,
            cwd=sandbox["work"],
            timeout_seconds=float(arguments["timeout"]),
        )
        if completed.timed_out:
            # The bounded Host runner has already killed the docker CLI's full
            # process tree. Docker can leave its detached container behind, so
            # always make one separately bounded best-effort cleanup attempt.
            try:
                _run_docker_command(
                    (docker, "rm", "-f", container_name),
                    cwd=sandbox["work"],
                    timeout_seconds=20,
                )
            except OSError:
                pass
            raise RuntimeError("sandbox execution timed out and was cancelled")
        if completed.exit_code is None:
            raise RuntimeError("sandbox execution transport failed")
        sandbox["updated_at"] = time.time()
        return {
            "sandbox_id": sandbox["id"],
            "image": image,
            "exit_code": completed.exit_code,
            "stdout": _bounded_output(completed.stdout, completed.stdout_truncated),
            "stderr": _bounded_output(completed.stderr, completed.stderr_truncated),
            "network": "none",
            "host_downgrade": False,
            "host_modified": False,
            "diff": self._diff(sandbox),
        }

    def _diff(self, sandbox: Mapping[str, Any]) -> dict[str, Any]:
        changed = _changes(sandbox["base"], sandbox["work"])
        return {"sandbox_id": sandbox["id"], "changed_files": changed, "host_modified": False}

    def _discard(self, sandbox: dict[str, Any]) -> dict[str, Any]:
        shutil.rmtree(sandbox["state"], ignore_errors=False)
        sandbox["status"] = "discarded"
        sandbox["updated_at"] = time.time()
        return self._public(sandbox)

    def _redeem(
        self, name: str, payload: Mapping[str, Any], arguments: Mapping[str, Any], workspace_id: str
    ) -> None:
        result = self.client.invoke(
            AUTHORITY,
            "redeem",
            {
                "receipt": str(payload.get("authority_receipt") or ""),
                "service_pack_id": SERVICE_PACK_ID,
                "operation": f"coding.sandbox.{name}",
                "authority": "coding.sandbox.control",
                "caller_id": str(payload.get("caller_id") or ""),
                "caller_pack_id": str(payload.get("caller_pack_id") or ""),
                "caller_function_id": str(payload.get("caller_function_id") or ""),
                "profile_id": self.profile_id,
                "workspace_id": workspace_id,
                "session_id": str(payload.get("session_id") or ""),
                "arguments": dict(arguments),
            },
        )
        if not result.get("authorized"):
            raise PermissionError(str(result.get("reason") or "sandbox control denied"))

    def _required(self, sandbox_id: str) -> dict[str, Any]:
        with self.lock:
            value = self.records.get(sandbox_id)
        if value is None or value["status"] == "discarded":
            raise KeyError("coding sandbox is unknown")
        return value

    @staticmethod
    def _public(value: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "id": value["id"],
            "workspace_id": value["workspace_id"],
            "status": value["status"],
            "stage_audit": value["stage_audit"],
            "created_at": value["created_at"],
            "updated_at": value["updated_at"],
            "isolation": "docker_no_network",
            "host_downgrade": False,
            "host_modified": False,
        }


_RUNTIMES: dict[str, CodingSandboxRuntime] = {}
_LOCK = threading.Lock()


def create_sandbox_observe(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create sandbox observe operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).observe(name, payload)

    return operation


def create_sandbox_control(client: Any) -> Callable[[str, Mapping[str, Any]], Any]:
    """Create receipt-gated sandbox control operations."""

    def operation(name: str, payload: Mapping[str, Any]) -> Any:
        return _runtime(client, payload).control(name, payload)

    return operation


def _runtime(client: Any, payload: Mapping[str, Any]) -> CodingSandboxRuntime:
    profile_id = str(payload.get("profile_id") or "default")
    with _LOCK:
        return _RUNTIMES.setdefault(profile_id, CodingSandboxRuntime(client, profile_id))


def _stage(source: Path, target: Path, include_paths: list[str]) -> dict[str, Any]:
    target.mkdir(mode=0o700, parents=True, exist_ok=False)
    roots = [source / item for item in include_paths] if include_paths else [source]
    files = total = skipped = 0
    for root in roots:
        resolved_root = root.resolve(strict=True)
        try:
            resolved_root.relative_to(source)
        except ValueError as exc:
            raise PermissionError("sandbox include path escapes workspace") from exc
        candidates = [resolved_root] if resolved_root.is_file() else resolved_root.rglob("*")
        for candidate in candidates:
            if any(part in _SKIP_DIRS for part in candidate.relative_to(source).parts):
                skipped += 1
                continue
            info = candidate.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                continue
            relative = candidate.relative_to(source)
            if _secret(relative):
                skipped += 1
                continue
            if (
                info.st_size > _MAX_FILE
                or files + 1 > _MAX_FILES
                or total + info.st_size > _MAX_BYTES
            ):
                raise ValueError("sandbox staging limit exceeded")
            destination = target / relative
            destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            shutil.copyfile(candidate, destination, follow_symlinks=False)
            files += 1
            total += info.st_size
    return {"files": files, "bytes": total, "skipped": skipped}


def _secret(path: Path) -> bool:
    folded_parts = {part.casefold() for part in path.parts}
    name = path.name.casefold()
    return (
        bool(folded_parts.intersection(_SECRET_PARTS))
        or name in _SECRET_NAMES
        or name.startswith(".env.")
        or path.suffix.casefold() in _SECRET_SUFFIXES
    )


def _jailed(root: Path, value: Any, *, must_exist: bool) -> Path:
    raw = Path(str(value or ""))
    if not str(raw) or raw.is_absolute() or ".." in raw.parts or _secret(raw):
        raise PermissionError("sandbox path is restricted")
    candidate = root / raw
    resolved = (
        candidate.resolve(strict=True)
        if must_exist
        else candidate.parent.resolve(strict=True) / candidate.name
    )
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("sandbox path escapes work root") from exc
    return resolved


def _atomic(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _changes(base: Path, work: Path) -> list[dict[str, Any]]:
    paths: set[str] = set()
    for root in (base, work):
        files = total = 0
        for path in root.rglob("*"):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError("sandbox diff contains an unsupported symlink")
            if not stat.S_ISREG(info.st_mode):
                continue
            files += 1
            total += info.st_size
            if info.st_size > _MAX_FILE or files > _MAX_FILES or total > _MAX_BYTES:
                raise ValueError("sandbox diff limit exceeded")
            paths.add(path.relative_to(root).as_posix())
    result = []
    for relative in sorted(paths):
        before = base / relative
        after = work / relative
        before_hash = _hash(before) if before.is_file() else None
        after_hash = _hash(after) if after.is_file() else None
        if before_hash != after_hash:
            result.append({"path": relative, "before_sha256": before_hash, "sha256": after_hash})
    return result


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _docker_executable() -> str:
    executable = shutil.which("docker")
    if executable is None:
        raise RuntimeError("Docker CLI is unavailable")
    return str(Path(executable).resolve())


def _run_docker_command(
    argv: tuple[str, ...],
    *,
    cwd: Path,
    timeout_seconds: float,
) -> BoundedProcessResult:
    """Run one exact Docker CLI command through the Host process boundary."""
    process_cwd = Path(cwd).resolve()
    environment = {"PATH": os.defpath}
    return HostBoundedProcessRunner().run_local(
        argv=argv,
        cwd=process_cwd,
        stdin=None,
        timeout_seconds=timeout_seconds,
        environment=environment,
        policy=ProcessExecutionPolicy(
            allowed_executables=frozenset({argv[0]}),
            allowed_argv=(argv,),
            allowed_cwds=(process_cwd,),
            allowed_environment=frozenset(environment),
            max_stdin_bytes=1,
            max_stdout_bytes=_MAX_OUTPUT,
            max_stderr_bytes=_MAX_OUTPUT,
            max_timeout_seconds=timeout_seconds,
        ),
    )


def _bounded_output(value: str, truncated: bool) -> str:
    return value + ("\n[truncated]\n" if truncated else "")


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("sandbox include_paths must be a list")
    return [str(item) for item in value]
