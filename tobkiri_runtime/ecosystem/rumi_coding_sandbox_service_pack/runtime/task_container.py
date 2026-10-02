"""Captured, one-shot COW container tasks using the existing bounded Host runner."""

from __future__ import annotations

import base64
from collections.abc import Mapping
from contextlib import nullcontext
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import threading
import time
from typing import Any, Callable

from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
    ProcessExecutionCancelled,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_state import TaskState
from tobkiri_protocol.workspace_capsule_v1 import (
    MAX_FILES,
    MAX_FILE_BYTES,
    MAX_TOTAL_BYTES,
    canonical,
    digest,
    export_archive,
    import_archive,
    make_capsule,
    safe_path,
)
from tobkiri_protocol.workspace_task_v1 import (
    TASK_VERSION,
    REQUEST_FIELDS,
    task_identity,
    validate_task_plan,
    validate_task_request,
)

CLOUD = "tobkiri.resource.cloud.workspace.v1"
CLOUD_OPERATION = "tobkiri_cloud_workspace_pack.workspace-resource"
MAX_OUTPUT = 32 * 1024
MAX_TASK_BYTES = 4 * 1024 * 1024
MAX_TASK_ARCHIVE_BYTES = 5 * 1024 * 1024


class ContainerTasks:
    """Run only immutable captured plans, without cloud connections or host shells."""

    def __init__(
        self,
        state: TaskState,
        *,
        profile_id: str,
        plan_digest: str,
        security_epoch: int,
        recipe: Mapping[str, Any],
        clock: Callable[[], float] = time.time,
    ) -> None:
        """Capture source-reviewed image policy and private Profile state."""
        self.state, self.profile_id, self.plan_digest = state, profile_id, plan_digest
        self.security_epoch, self.recipe, self.clock = (
            security_epoch,
            dict(recipe),
            clock,
        )
        self.recipe_digest = digest(canonical(recipe))
        self.runner = HostBoundedProcessRunner()

    def prepare(
        self,
        request: Mapping[str, Any],
        client: Any,
        *,
        owner: str,
        request_id: str,
        guard: Callable[[], None],
    ) -> dict[str, Any]:
        """Seal a live checkpoint and a cached immutable image without running work."""
        request = validate_task_request(request)
        self._scope(request)
        guard()
        archive, manifest = self._source(client, request)
        executable = docker_identity()
        self._image(executable, guard)
        task_id = task_identity(request, owner)
        plan = {
            **request,
            "version": TASK_VERSION,
            "task_id": task_id,
            "plan_digest": self.plan_digest,
            "security_epoch": self.security_epoch,
            "checkpoint_digest": manifest["manifest_digest"],
            "recipe_digest": self.recipe_digest,
            "image_reference": self.recipe["image_reference"],
            "expires_at_ms": int(self.clock() * 1000) + 60_000,
            "request_digest": digest(canonical(request)),
        }
        validate_task_plan(plan)
        guard()
        plan = self.state.prepare(plan, archive, executable, owner)
        return {
            "executed": False,
            "task_plan": plan,
            "task_plan_digest": digest(canonical(plan)),
        }

    def execute(
        self,
        payload: Mapping[str, Any],
        client: Any,
        *,
        owner: str,
        guard: Callable[[], None],
        cancel_event: threading.Event,
        track: Callable[[str], Any] | None = None,
    ) -> dict[str, Any]:
        """Consume one Broker-authorized plan and retain a verified result capsule."""
        if set(payload) != {"task_plan", "task_plan_digest"}:
            raise PermissionError("workspace task execute fields are invalid")
        plan = validate_task_plan(payload["task_plan"])
        self._scope(plan)
        if (
            payload["task_plan_digest"] != digest(canonical(plan))
            or plan["recipe_digest"] != self.recipe_digest
            or plan["image_reference"] != self.recipe["image_reference"]
            or plan["security_epoch"] != self.security_epoch
            or plan["plan_digest"] != self.plan_digest
        ):
            raise PermissionError("workspace task captured plan differs")
        row = self.state.read(plan["task_id"], owner)
        if row["plan"] != plan:
            raise PermissionError("workspace task prepared input differs")
        if row["receipt"] is not None:
            guard()
            return row["receipt"]
        if plan["expires_at_ms"] <= int(self.clock() * 1000):
            raise PermissionError("workspace task approval plan expired")
        guard()
        source, manifest = self._source(
            client, {key: plan[key] for key in REQUEST_FIELDS}
        )
        if (
            source != bytes(row["archive"])
            or manifest["manifest_digest"] != plan["checkpoint_digest"]
        ):
            raise PermissionError("workspace task checkpoint changed")
        if docker_identity() != row["executable"]:
            raise PermissionError("workspace task Docker executable changed")
        self._image(row["executable"], guard)
        if not hasattr(os, "getuid") or os.getuid() == 0:
            raise PermissionError("workspace tasks require a nonroot local user")
        guard()
        with track(plan["task_id"]) if track else nullcontext():
            guard()
            self.state.claim(plan["task_id"], owner, plan)
            return self._run(plan, row, guard, cancel_event)

    def resource(self, task_id: str, owner: str, *, export: bool) -> dict[str, Any]:
        """Expose a bounded receipt or portable output, never live Host state."""
        row = self.state.read(task_id, owner)
        if not export:
            return {
                "task_id": task_id,
                "status": row["status"],
                "receipt": row["receipt"],
            }
        if row["output"] is None or row["status"] != "completed":
            raise LookupError("workspace task has no verified output")
        archive = bytes(row["output"])
        manifest, _ = import_archive(archive)
        return {
            "task_id": task_id,
            "archive_base64": base64.b64encode(archive).decode("ascii"),
            "archive_digest": digest(archive),
            "checkpoint_digest": manifest["manifest_digest"],
            "task_plan": row["plan"],
            "receipt": row["receipt"],
        }

    def _scope(self, value: Mapping[str, Any]) -> None:
        if value["profile_id"] != self.profile_id:
            raise PermissionError("workspace task Profile differs")

    def _source(
        self, client: Any, request: Mapping[str, Any]
    ) -> tuple[bytes, dict[str, Any]]:
        source = client.invoke(
            CLOUD,
            CLOUD_OPERATION,
            {
                "operation": "task_source",
                **{
                    key: request[key]
                    for key in (
                        "profile_id",
                        "workspace_id",
                        "expected_revision",
                        "expected_writer_epoch",
                    )
                },
            },
        )
        raw = source.get("archive_base64")
        if not isinstance(raw, str) or len(raw) > (MAX_TASK_ARCHIVE_BYTES + 2) // 3 * 4:
            raise ValueError("workspace task source is invalid")
        archive = base64.b64decode(raw, validate=True)
        manifest, _ = import_archive(archive)
        if (
            manifest["workspace_id"] != request["workspace_id"]
            or manifest["revision"] != request["expected_revision"]
            or manifest["source"]
            != {"profile_id": self.profile_id, "plan_digest": self.plan_digest}
            or manifest["recipe_digest"] != self.recipe["portable_recipe_digest"]
            or manifest["total_bytes"] > MAX_TASK_BYTES
            or digest(archive) != source.get("archive_digest")
        ):
            raise PermissionError("workspace task source provenance differs")
        return archive, manifest

    def _image(self, executable: Mapping[str, Any], guard: Callable[[], None]) -> None:
        guard()
        result = self._command(
            [
                executable["path"],
                "image",
                "inspect",
                "--format",
                "{{json .RepoDigests}}",
                self.recipe["image_reference"],
            ],
            self.state.root,
            timeout=10,
        )
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
            raise PermissionError(
                "reviewed task image is not cached; no image is pulled"
            )
        images = json.loads(result.stdout)
        if (
            not isinstance(images, list)
            or len(images) > 64
            or self.recipe["image_reference"] not in images
        ):
            raise PermissionError("cached task image digest is unverified")
        guard()

    def _command(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int,
        cancel: threading.Event | None = None,
    ) -> Any:
        self.state.ensure_root()
        return self.runner.run_local(
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=timeout,
            environment={"PATH": os.defpath},
            cancel_event=cancel,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({argv[0]}),
                allowed_argv=(tuple(argv),),
                allowed_cwds=(cwd,),
                allowed_environment=frozenset({"PATH"}),
                max_stdin_bytes=1,
                max_stdout_bytes=MAX_OUTPUT,
                max_stderr_bytes=MAX_OUTPUT,
                max_timeout_seconds=timeout,
                redact_values=(str(self.state.root),),
            ),
        )

    def _run(
        self,
        plan: Mapping[str, Any],
        row: Mapping[str, Any],
        guard: Callable[[], None],
        outer_cancel: threading.Event,
    ) -> dict[str, Any]:
        task_id = plan["task_id"]
        directory = self.state.root / task_id
        work = directory / "work"
        name = "tobkiri-" + task_id
        docker = row["executable"]["path"]
        stop, done = threading.Event(), threading.Event()
        output, cleanup_ok, process, created = None, False, None, False
        receipt: dict[str, Any] = {
            "task_id": task_id,
            "status": "ambiguous",
            "executed": False,
        }
        try:
            manifest, blobs = import_archive(bytes(row["archive"]))
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
            work.mkdir(mode=0o700, exist_ok=False)
            for entry in manifest["files"]:
                destination = work / entry["path"]
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                destination.write_bytes(blobs[entry["digest"]])
                destination.chmod(0o600)
            if any(c in str(work) for c in (",", "\n", "\r")):
                raise PermissionError("workspace task mount path is unavailable")
            argv = [
                docker,
                "run",
                "--pull=never",
                "--name",
                name,
                "--network",
                "none",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--pids-limit",
                "64",
                "--memory",
                "256m",
                "--cpus",
                "1",
                "--read-only",
                "--user",
                f"{os.getuid()}:{os.getgid()}",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,size=16m",
                "--mount",
                f"type=bind,src={work},dst=/workspace,rw",
                "--workdir",
                "/workspace",
                "--entrypoint",
                plan["argv"][0],
                plan["image_reference"],
                *plan["argv"][1:],
            ]

            def monitor() -> None:
                while not done.wait(0.1):
                    try:
                        guard()
                        if outer_cancel.is_set():
                            stop.set()
                            return
                    except Exception:
                        stop.set()
                        return

            guard()
            thread = threading.Thread(target=monitor, daemon=True)
            thread.start()
            try:
                process = self._command(
                    argv, self.state.root, timeout=plan["timeout_seconds"], cancel=stop
                )
            except ProcessExecutionCancelled as exc:
                process = exc.result
                receipt["status"] = "cancelled"
            finally:
                done.set()
                thread.join(timeout=1)
            # Reaping Docker CLI does not prove daemon-side workload cancellation.
            # Cleanup only this journal-owned name, then verify it is absent.
            container = self._container_state(docker, name)
            started = bool(
                container
                and container.get("StartedAt")
                and not container["StartedAt"].startswith("0001-")
            )
            cleanup_ok = self._cleanup(docker, name)
            if process is not None:
                receipt.update(
                    {
                        "executed": started,
                        "workload_started": started,
                        "exit_code": process.exit_code,
                        "stdout": process.stdout,
                        "stderr": process.stderr,
                        "stdout_truncated": process.stdout_truncated,
                        "stderr_truncated": process.stderr_truncated,
                        "timed_out": process.timed_out,
                        "transport_error": process.transport_error,
                        "execution_boundary": "local_container_argv",
                    }
                )
            if not cleanup_ok:
                receipt["status"] = "ambiguous"
            elif receipt["status"] == "cancelled" or stop.is_set():
                receipt["status"] = "cancelled"
            elif (
                process is None
                or process.timed_out
                or process.transport_error
                or not started
                or container.get("Running") is not False
                or container.get("ExitCode") != process.exit_code
            ):
                receipt["status"] = "failed"
            else:
                guard()
                files = read_work_tree(work)
                next_manifest, next_blobs = make_capsule(
                    workspace_id=plan["workspace_id"],
                    profile_id=self.profile_id,
                    plan_digest=self.plan_digest,
                    revision=plan["expected_revision"] + 1,
                    parent_digest=plan["checkpoint_digest"],
                    recipe_digest=manifest["recipe_digest"],
                    files=files,
                )
                output = export_archive(next_manifest, next_blobs)
                before = {entry["path"]: entry["digest"] for entry in manifest["files"]}
                after = {
                    entry["path"]: entry["digest"] for entry in next_manifest["files"]
                }
                receipt.update(
                    {
                        "status": "completed",
                        "output_checkpoint_digest": next_manifest["manifest_digest"],
                        "diff": [
                            path
                            for path in sorted(before.keys() | after.keys())
                            if before.get(path) != after.get(path)
                        ],
                    }
                )
                guard()
        except Exception:
            if not cleanup_ok:
                cleanup_ok = self._cleanup(docker, name)
            receipt["status"] = "failed" if cleanup_ok else "ambiguous"
            output = None
        finally:
            done.set()
            receipt["container_cleanup_verified"] = cleanup_ok
            self.state.finish(task_id, receipt, output)
            if cleanup_ok and created and directory.exists():
                shutil.rmtree(directory)
        return receipt

    def _cleanup(self, docker: str, name: str) -> bool:
        try:
            self._command([docker, "rm", "-f", name], self.state.root, timeout=10)
            result = self._command(
                [docker, "container", "inspect", name], self.state.root, timeout=10
            )
            return (
                result.exit_code == 1
                and not result.timed_out
                and not result.stderr_truncated
                and "No such container" in result.stderr
            )
        except Exception:
            return False

    def _container_state(self, docker: str, name: str) -> dict[str, Any]:
        result = self._command(
            [docker, "container", "inspect", "--format", "{{json .State}}", name],
            self.state.root,
            timeout=10,
        )
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
            return {}
        state = json.loads(result.stdout)
        if (
            not isinstance(state, dict)
            or not isinstance(state.get("StartedAt"), str)
            or len(state["StartedAt"]) > 100
            or type(state.get("ExitCode")) is not int
            or type(state.get("Running")) is not bool
        ):
            return {}
        return state


def docker_identity() -> dict[str, Any]:
    """Measure the server-resolved Docker executable; never accept a client path."""
    raw = shutil.which("docker")
    if not raw:
        raise PermissionError("Docker CLI is unavailable")
    path = Path(raw).resolve(strict=True)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128 * 1024 * 1024:
        raise PermissionError("Docker CLI is invalid")
    with path.open("rb") as stream:
        checksum = "sha256:" + hashlib.file_digest(stream, "sha256").hexdigest()
    return {
        "path": str(path),
        "digest": checksum,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def read_work_tree(root: Path) -> dict[str, bytes]:
    """Read bounded regular output after verified container cleanup, rejecting links."""
    files: dict[str, bytes] = {}
    total, entries = 0, 0
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise PermissionError("descriptor-safe task output inspection is unavailable")

    def visit(directory: int, prefix: str) -> None:
        nonlocal total, entries
        with os.scandir(directory) as children:
            for child in children:
                entries += 1
                if entries > MAX_FILES * 4:
                    raise ValueError("workspace task output contains too many entries")
                relative = safe_path(prefix + child.name)
                metadata = child.stat(follow_symlinks=False)
                if stat.S_ISLNK(metadata.st_mode):
                    raise PermissionError(
                        "workspace task output contains a symbolic link"
                    )
                if stat.S_ISDIR(metadata.st_mode):
                    descriptor = os.open(
                        child.name,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory,
                    )
                    try:
                        pinned = os.fstat(descriptor)
                        if (pinned.st_dev, pinned.st_ino) != (
                            metadata.st_dev,
                            metadata.st_ino,
                        ):
                            raise PermissionError(
                                "workspace task output directory changed"
                            )
                        visit(descriptor, relative + "/")
                    finally:
                        os.close(descriptor)
                    continue
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_size > MAX_FILE_BYTES
                ):
                    raise ValueError("workspace task output file is invalid")
                total += metadata.st_size
                if len(files) >= MAX_FILES or total > min(
                    MAX_TOTAL_BYTES, MAX_TASK_BYTES
                ):
                    raise ValueError("workspace task output exceeds the capsule limit")
                descriptor = os.open(
                    child.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
                )
                with os.fdopen(descriptor, "rb") as stream:
                    pinned = os.fstat(stream.fileno())
                    if (pinned.st_dev, pinned.st_ino, pinned.st_size) != (
                        metadata.st_dev,
                        metadata.st_ino,
                        metadata.st_size,
                    ):
                        raise PermissionError("workspace task output changed")
                    data = stream.read(MAX_FILE_BYTES + 1)
                if len(data) != metadata.st_size:
                    raise ValueError("workspace task output size changed")
                files[relative] = data

    descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        visit(descriptor, "")
    finally:
        os.close(descriptor)
    return files
