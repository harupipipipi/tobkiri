"""Quota-bound guest execution and exact created-container ownership cleanup."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import shutil
import threading
from typing import Any, Callable, Mapping
import uuid

from core_runtime.bounded_process_runner import ProcessExecutionCancelled
from tobkiri_protocol.workspace_capsule_v1 import canonical, digest, import_archive

CID = re.compile(r"[0-9a-f]{64}\Z")
GUEST_FILES = {
    "container/task_runner.py": "/opt/task_runner.py",
    "container/workspace_capsule_v1.py": "/opt/tobkiri_protocol/workspace_capsule_v1.py",
    "container/workspace_tree_v1.py": "/opt/tobkiri_protocol/workspace_tree_v1.py",
}


class TaskExecution:
    """Own one bounded task's guest transport and confirmed Docker object identity."""

    def __init__(self, tasks: Any, transport: Mapping[str, Any], token: str) -> None:
        """Use only server-measured local Docker transport and private ownership."""
        self.tasks, self.transport, self.token = tasks, transport, token
        self.docker = transport["path"]

    def command(
        self,
        argv: list[str],
        *,
        timeout: int = 10,
        cancel: threading.Event | None = None,
        output_limit: int = 32768,
    ) -> Any:
        """Submit exactly one bounded argv through the captured Host substrate."""
        return self.tasks._command(
            [self.docker, *argv],
            self.tasks.state.root,
            timeout=timeout,
            cancel=cancel,
            transport=self.transport,
            output_limit=output_limit,
        )

    def inspect(self, reference: str) -> dict[str, Any] | None:
        """Read exact CID/token/state without accepting a name as deletion authority."""
        template = '{"id":{{json .Id}},"token":{{json (index .Config.Labels "tobkiri.task-token")}},"state":{{json .State}}}'
        result = self.command(["container", "inspect", "--format", template, reference])
        if (
            result.exit_code == 1
            and not result.timed_out
            and "No such container" in result.stderr
        ):
            return None
        if result.exit_code != 0 or result.timed_out or result.stdout_truncated:
            raise RuntimeError("task container identity is unavailable")
        value = json.loads(result.stdout)
        if (
            not isinstance(value, dict)
            or set(value) != {"id", "token", "state"}
            or not isinstance(value["id"], str)
            or CID.fullmatch(value["id"]) is None
            or (CID.fullmatch(reference) is not None and value["id"] != reference)
        ):
            raise RuntimeError("task container identity is invalid")
        return value

    def cleanup(self, reference: str) -> bool:
        """Remove only a confirmed owned CID on the same measured local daemon."""
        try:
            if self.tasks._daemon_id(self.transport) != self.transport["daemon_id"]:
                return False
            owned = self.inspect(reference)
            if owned is None:
                return True
            if owned["token"] != self.token:
                # A foreign name is not ours; a confirmed CID still present is
                # ambiguous and must never count as verified cleanup.
                return CID.fullmatch(reference) is None
            removed = self.command(["rm", "-f", owned["id"]])
            if removed.exit_code != 0 or removed.timed_out:
                return False
            return self.inspect(owned["id"]) is None
        except Exception:
            return False

    def run(
        self,
        plan: Mapping[str, Any],
        row: Mapping[str, Any],
        guard: Callable[[], None],
        outer_cancel: threading.Event,
    ) -> dict[str, Any]:
        """Run work on bounded tmpfs and validate the guest envelope after cleanup."""
        task_id = plan["task_id"]
        directory = self.tasks.state.root / task_id
        name = "tobkiri-task-" + self.token
        reference = name
        created, cleanup_ok, output = False, False, None
        stop, done = threading.Event(), threading.Event()
        receipt: dict[str, Any] = {
            "task_id": task_id,
            "status": "ambiguous",
            "executed": False,
        }
        self.tasks.state.bind_container(task_id, name, self.token)
        try:
            manifest, _ = import_archive(bytes(row["archive"]))
            directory.mkdir(mode=0o700, exist_ok=False)
            created = True
            archive_path = directory / "input.zip"
            archive_path.write_bytes(bytes(row["archive"]))
            archive_path.chmod(0o600)
            uid, gid = os.getuid(), os.getgid()
            argv = [
                "create",
                "--pull=never",
                "--name",
                name,
                "--label",
                f"tobkiri.task-token={self.token}",
                "--log-driver",
                "none",
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
                f"{uid}:{gid}",
                "--tmpfs",
                f"/workspace:rw,noexec,nosuid,nodev,size=8m,nr_inodes=512,uid={uid},gid={gid},mode=700",
                "--tmpfs",
                "/tmp:rw,noexec,nosuid,nodev,size=16m,nr_inodes=256,mode=1777",
                "--mount",
                f"type=bind,src={archive_path},dst=/input/workspace.zip,readonly",
                "--workdir",
                "/workspace",
            ]
            for path, destination in GUEST_FILES.items():
                local = directory / Path(path).name
                data = self.tasks.guest_files[path]
                local.write_bytes(data)
                local.chmod(0o600)
                argv.extend(
                    ["--mount", f"type=bind,src={local},dst={destination},readonly"]
                )
            if any(c in str(directory) for c in (",", "\n", "\r")):
                raise PermissionError("task input mount path is unavailable")
            argv.extend(
                [
                    "--entrypoint",
                    "python3",
                    plan["image_reference"],
                    "-B",
                    "/opt/task_runner.py",
                    "--input",
                    "/input/workspace.zip",
                    "--work",
                    "/workspace",
                    "--argv-json",
                    canonical(plan["argv"]).decode(),
                    "--timeout",
                    str(plan["timeout_seconds"]),
                ]
            )
            guard()
            made = self.command(argv)
            if (
                made.exit_code != 0
                or made.timed_out
                or made.stdout_truncated
                or CID.fullmatch(made.stdout.strip()) is None
            ):
                raise RuntimeError("task container creation failed")
            reference = made.stdout.strip()
            owned = self.inspect(reference)
            if (
                owned is None
                or owned["id"] != reference
                or owned["token"] != self.token
            ):
                raise RuntimeError("created task container ownership differs")
            self.tasks.state.bind_container(task_id, name, self.token, reference)

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
            process = None
            try:
                process = self.command(
                    ["start", "--attach", reference],
                    timeout=plan["timeout_seconds"] + 5,
                    cancel=stop,
                    output_limit=8 * 1024 * 1024,
                )
            except ProcessExecutionCancelled as exc:
                process = exc.result
                receipt["status"] = "cancelled"
            finally:
                done.set()
                thread.join(timeout=1)
            observed = self.inspect(reference)
            state = (
                observed["state"]
                if observed and observed["token"] == self.token
                else {}
            )
            started = bool(
                isinstance(state.get("StartedAt"), str)
                and state["StartedAt"]
                and not state["StartedAt"].startswith("0001-")
            )
            receipt.update(
                container_started=started,
                workload_started=False,
                execution_boundary="local_container_argv",
            )
            cleanup_ok = self.cleanup(reference)
            if not cleanup_ok:
                receipt["status"] = "ambiguous"
            elif stop.is_set() or receipt["status"] == "cancelled":
                receipt["status"] = "cancelled"
            elif (
                process is None
                or process.timed_out
                or process.transport_error
                or process.stdout_truncated
                or not started
                or state.get("Running") is not False
                or state.get("ExitCode") != 0
                or process.exit_code != 0
            ):
                receipt["status"] = "failed"
            else:
                guard()
                output = self._output(process.stdout, manifest, receipt)
                guard()
        except Exception:
            cleanup_ok = self.cleanup(reference)
            receipt["status"] = "failed" if cleanup_ok else "ambiguous"
            output = None
        finally:
            done.set()
            receipt["container_cleanup_verified"] = cleanup_ok
            self.tasks.state.finish(task_id, receipt, output)
            if cleanup_ok and created:
                shutil.rmtree(directory)
        return receipt

    def _output(
        self, text: str, source: Mapping[str, Any], receipt: dict[str, Any]
    ) -> bytes | None:
        value = json.loads(text)
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "receipt", "archive_base64", "archive_digest"}
            or value["version"] != "tobkiri.workspace-task-output.v1"
        ):
            raise ValueError("guest task output envelope is invalid")
        result = value["receipt"]
        if (
            not isinstance(result, dict)
            or set(result)
            != {
                "exit_code",
                "timed_out",
                "stdout",
                "stderr",
                "stdout_truncated",
                "stderr_truncated",
            }
            or type(result["exit_code"]) is not int
            or any(
                type(result[key]) is not bool
                for key in ("timed_out", "stdout_truncated", "stderr_truncated")
            )
            or any(
                not isinstance(result[key], str) or len(result[key]) > 32768
                for key in ("stdout", "stderr")
            )
        ):
            raise ValueError("guest task receipt is invalid")
        receipt.update(result, executed=True, workload_started=True)
        if result["exit_code"] != 0 or result["timed_out"]:
            receipt["status"] = "failed"
            return None
        raw = value["archive_base64"]
        if not isinstance(raw, str) or len(raw) > 7_000_000:
            raise ValueError("guest task capsule exceeds the size limit")
        archive = base64.b64decode(raw, validate=True)
        manifest, _ = import_archive(archive)
        if (
            digest(archive) != value["archive_digest"]
            or manifest["workspace_id"] != source["workspace_id"]
            or manifest["revision"] != source["revision"] + 1
            or manifest["parent_digest"] != source["manifest_digest"]
            or manifest["source"] != source["source"]
            or manifest["recipe_digest"] != source["recipe_digest"]
            or manifest["total_bytes"] > 4 * 1024 * 1024
        ):
            raise ValueError("guest task capsule binding differs")
        before = {entry["path"]: entry["digest"] for entry in source["files"]}
        after = {entry["path"]: entry["digest"] for entry in manifest["files"]}
        changes = [
            path
            for path in sorted(before.keys() | after.keys())
            if before.get(path) != after.get(path)
        ]
        receipt.update(
            status="completed",
            output_checkpoint_digest=manifest["manifest_digest"],
            diff=changes[:128],
            diff_truncated=len(changes) > 128,
        )
        return archive


def execute_task(
    tasks: Any,
    plan: Mapping[str, Any],
    row: Mapping[str, Any],
    guard: Callable[[], None],
    cancellation: threading.Event,
) -> dict[str, Any]:
    """Execute one journal-claimed task with a private random container token."""
    return TaskExecution(tasks, row["executable"], uuid.uuid4().hex).run(
        plan, row, guard, cancellation
    )
