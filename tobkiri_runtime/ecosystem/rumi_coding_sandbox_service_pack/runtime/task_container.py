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

from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_execution import (
    execute_task,
)
from core_runtime.bounded_process_runner import (
    HostBoundedProcessRunner,
    ProcessExecutionPolicy,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_state import TaskState
from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    digest,
    import_archive,
)
from tobkiri_protocol.workspace_task_v1 import (
    TASK_VERSION,
    REQUEST_FIELDS,
    task_identity,
    validate_task_plan,
    validate_task_request,
)
from tobkiri_protocol.workspace_tree_v1 import read_work_tree as read_work_tree

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
        guest_files: Mapping[str, bytes],
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
        self.guest_files = dict(guest_files)

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
        executable["daemon_id"] = self._image(executable, guard)
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
        if docker_identity() != {
            key: value for key, value in row["executable"].items() if key != "daemon_id"
        }:
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

    def _image(self, executable: Mapping[str, Any], guard: Callable[[], None]) -> str:
        guard()
        daemon_id = self._daemon_id(executable)
        if executable.get("daemon_id", daemon_id) != daemon_id:
            raise PermissionError("captured Docker daemon identity changed")
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
            transport=executable,
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
        return daemon_id

    def _command(
        self,
        argv: list[str],
        cwd: Path,
        *,
        timeout: int,
        transport: Mapping[str, Any],
        output_limit: int = MAX_OUTPUT,
        cancel: threading.Event | None = None,
    ) -> Any:
        self.state.ensure_root()
        endpoint = transport["endpoint"]
        if socket_identity(endpoint) != (
            transport["socket_device"],
            transport["socket_inode"],
        ):
            raise PermissionError("captured local Docker socket changed")
        if not endpoint.startswith("unix:///"):
            raise PermissionError("workspace tasks require a local Docker socket")
        configuration = self.state.root / "empty-docker-config"
        if configuration.is_symlink():
            raise PermissionError("task Docker configuration is unsafe")
        configuration.mkdir(mode=0o700, exist_ok=True)
        configuration.chmod(0o700)
        if any(configuration.iterdir()):
            raise PermissionError("task Docker configuration must remain empty")
        environment = {
            "PATH": os.defpath,
            "DOCKER_HOST": endpoint,
            "DOCKER_CONFIG": str(configuration),
        }
        return self.runner.run_local(
            argv=argv,
            cwd=cwd,
            stdin=None,
            timeout_seconds=timeout,
            environment=environment,
            cancel_event=cancel,
            policy=ProcessExecutionPolicy(
                allowed_executables=frozenset({argv[0]}),
                allowed_argv=(tuple(argv),),
                allowed_cwds=(cwd,),
                allowed_environment=frozenset(environment),
                max_stdin_bytes=1,
                max_stdout_bytes=output_limit,
                max_stderr_bytes=MAX_OUTPUT,
                max_timeout_seconds=timeout,
                redact_values=(
                    str(self.state.root),
                    endpoint,
                    endpoint.removeprefix("unix://"),
                ),
            ),
        )

    def _run(
        self,
        plan: Mapping[str, Any],
        row: Mapping[str, Any],
        guard: Callable[[], None],
        outer_cancel: threading.Event,
    ) -> dict[str, Any]:
        return execute_task(self, plan, row, guard, outer_cancel)

    def _daemon_id(self, transport: Mapping[str, Any]) -> str:
        result = self._command(
            [transport["path"], "info", "--format", "{{json .ID}}"],
            self.state.root,
            timeout=10,
            transport=transport,
        )
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
            raise PermissionError("local Docker daemon identity is unavailable")
        value = json.loads(result.stdout)
        if not isinstance(value, str) or not 1 <= len(value) <= 128:
            raise PermissionError("local Docker daemon identity is invalid")
        return value


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
        hasher = hashlib.sha256()
        for chunk in iter(lambda: stream.read(65536), b""):
            hasher.update(chunk)
        checksum = "sha256:" + hasher.hexdigest()
    candidates = [Path("/var/run/docker.sock"), Path.home() / ".docker/run/docker.sock"]
    endpoint = None
    for candidate in candidates:
        try:
            local = candidate.resolve(strict=True)
            socket = local.stat()
        except OSError:
            continue
        if stat.S_ISSOCK(socket.st_mode):
            endpoint = {
                "endpoint": "unix://" + str(local),
                "socket_device": socket.st_dev,
                "socket_inode": socket.st_ino,
            }
            break
    if endpoint is None:
        raise PermissionError("a local Docker daemon socket is unavailable")
    return {
        **endpoint,
        "path": str(path),
        "digest": checksum,
        "device": metadata.st_dev,
        "inode": metadata.st_ino,
        "size": metadata.st_size,
        "mtime_ns": metadata.st_mtime_ns,
    }


def socket_identity(endpoint: str) -> tuple[int, int]:
    """Measure only a local Unix socket before every bounded Docker command."""
    if not endpoint.startswith("unix:///"):
        raise PermissionError("a local Docker socket is required")
    metadata = Path(endpoint.removeprefix("unix://")).stat()
    if not stat.S_ISSOCK(metadata.st_mode):
        raise PermissionError("local Docker socket is invalid")
    return metadata.st_dev, metadata.st_ino
