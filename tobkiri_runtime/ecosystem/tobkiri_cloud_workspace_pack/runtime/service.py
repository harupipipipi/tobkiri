"""Portable local workspaces reached only through captured public operations."""

from __future__ import annotations

import base64
from typing import Any, Callable, Mapping

from ecosystem.tobkiri_cloud_workspace_pack.runtime.capsule import (
    MAX_ARCHIVE_BYTES,
    MAX_FILES,
    canonical,
    digest,
    export_archive,
    handoff_offer,
    identifier,
    import_archive,
    integer,
    make_capsule,
    safe_path,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.store import (
    Conflict,
    WorkspaceStore,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.recipe import RECIPE_DIGEST

WORKSPACE = "tobkiri.resource.workspace.v1"
WORKSPACE_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
INSPECT = "tobkiri.service.file.inspect.v1"
INSPECT_OPERATION = "rumi_file_inspect_pack.file-inspect"
CONTAINER_PORT = "tobkiri.action.workspace.container.v1"
DEPLOY_PORT = "tobkiri.service.workspace.deploy.v1"


class CloudWorkspace:
    """Keep local capture/restore distinct from unimplemented cloud provisioning."""

    def __init__(
        self,
        store: WorkspaceStore,
        client: Any,
        *,
        plan_digest: str,
        actor: str,
        request_id: str,
        guard: Callable[[], None],
    ) -> None:
        self.store, self.client = store, client
        self.plan_digest, self.actor = plan_digest, actor
        self.request_id, self.guard = request_id, guard

    def snapshot(self, conversation_id: str) -> dict[str, Any]:
        """Expose a compact public state near the composer without private paths."""
        workspace_id = self.workspace_id(conversation_id)
        workspace = self.store.get(workspace_id) or {
            "id": workspace_id,
            "revision": 0,
            "checkpoint_digest": "",
            "file_count": 0,
            "total_bytes": 0,
            "status": "not_initialized",
            "container_status": "not_started",
            "writer_epoch": 0,
        }
        return {
            "workspace": workspace,
            "workspaces": self.store.list(),
            "container": {
                "status": "unavailable",
                "started": False,
                "reason": "Host container provisioning port is not bound",
            },
            "cloud": {
                "status": "unavailable",
                "connected": False,
                "reason": "Cloud deployment Pack is deferred",
            },
        }

    @staticmethod
    def workspace_id(conversation_id: str) -> str:
        """Derive a stable local identity from finite conversation context."""
        identifier(conversation_id)
        return "workspace-" + digest(conversation_id.encode())[7:31]

    def invoke(self, action: str, values: Mapping[str, Any]) -> dict[str, Any]:
        """Dispatch Broker-approved local operations with strict domain fields."""
        self.guard()
        conversation_id = identifier(values["conversation_id"])
        workspace_id = self.workspace_id(conversation_id)
        expected = integer(values["expected_revision"])
        writer_epoch = integer(values["expected_writer_epoch"])
        fingerprint = digest(
            canonical(
                {
                    "action": action,
                    "values": dict(values),
                    "actor": self.actor,
                    "profile_id": self.store.profile_id,
                    "plan_digest": self.plan_digest,
                }
            )
        )
        replayed = self.store.replay(self.request_id, fingerprint)
        if replayed is not None:
            return replayed
        current = self.store.get(workspace_id)
        if (current["revision"] if current else 0) != expected:
            raise Conflict("workspace checkpoint revision is stale")
        if (current["writer_epoch"] if current else 0) != writer_epoch:
            raise Conflict("workspace writer epoch is stale")
        if action in {"initialize", "capture", "import"}:
            if action == "initialize":
                if current is not None:
                    raise Conflict("workspace is already initialized")
                files = {"README.md": b"# Tobkiri workspace\n\nLocal work capsule.\n"}
            elif action == "capture":
                files = self._capture(values["paths"])
            else:
                raw = values["archive_base64"]
                if (
                    not isinstance(raw, str)
                    or len(raw) > (MAX_ARCHIVE_BYTES + 2) // 3 * 4
                ):
                    raise ValueError("capsule import exceeds the size limit")
                try:
                    archive = base64.b64decode(raw, validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValueError("capsule import encoding is invalid") from exc
                imported, blobs = import_archive(archive)
                if imported["recipe_digest"] != RECIPE_DIGEST:
                    raise ValueError("capsule recipe is unsupported")
                # Foreign provenance never activates a Profile/Plan. A new
                # local checkpoint seals these files to the captured Host.
                files = {
                    entry["path"]: blobs[entry["digest"]] for entry in imported["files"]
                }
            manifest, blobs = make_capsule(
                workspace_id=workspace_id,
                profile_id=self.store.profile_id,
                plan_digest=self.plan_digest,
                revision=expected + 1,
                recipe_digest=RECIPE_DIGEST,
                files=files,
                parent_digest=current["checkpoint_digest"] if current else "",
            )
            return self.store.publish(
                manifest,
                blobs,
                expected_revision=expected,
                expected_writer_epoch=writer_epoch,
                actor=self.actor,
                request_id=self.request_id,
                fingerprint=fingerprint,
                guard=self.guard,
            )
        if current is None:
            raise LookupError("workspace has no local checkpoint")
        checkpoint = current["checkpoint_digest"]
        if action == "restore":
            return self.store.restore_local(checkpoint, guard=self.guard)
        if action == "prepare_handoff":
            manifest, _ = self.store.read_capsule(checkpoint)
            self.store.release_writer(
                workspace_id,
                expected_revision=expected,
                expected_writer_epoch=writer_epoch,
                actor=self.actor,
                guard=self.guard,
            )
            return handoff_offer(manifest, values.get("expected_receiver_head", ""))
        raise ValueError("cloud workspace action is unsupported")

    def export(self, workspace_id: str, expected_revision: int) -> dict[str, Any]:
        """Return a verified archive without including host paths or live authority."""
        current = self.store.get(identifier(workspace_id))
        if current is None or current["revision"] != integer(expected_revision, 1):
            raise Conflict("workspace checkpoint revision is stale")
        manifest, blobs = self.store.read_capsule(current["checkpoint_digest"])
        archive = export_archive(manifest, blobs)
        self.guard()
        return {
            "version": "tobkiri.workspace-export.v1",
            "checkpoint_digest": manifest["manifest_digest"],
            "archive_digest": digest(archive),
            "archive_bytes": len(archive),
            "archive_base64": base64.b64encode(archive).decode("ascii"),
        }

    def verify(self, workspace_id: str, expected_revision: int) -> dict[str, Any]:
        """Verify retained work and recipe without claiming container execution."""
        result = self.export(workspace_id, expected_revision)
        return {k: v for k, v in result.items() if k != "archive_base64"} | {
            "status": "verified_locally",
            "container_started": False,
        }

    def _capture(self, raw_paths: Any) -> dict[str, bytes]:
        if not isinstance(raw_paths, str) or len(raw_paths) > 32_768:
            raise ValueError("workspace paths are invalid")
        paths = [
            safe_path(item.strip())
            for item in raw_paths.replace(",", "\n").splitlines()
            if item.strip()
        ]
        if not 1 <= len(paths) <= MAX_FILES or len(set(paths)) != len(paths):
            raise ValueError("choose a bounded unique list of workspace paths")
        before = self.client.invoke(
            WORKSPACE,
            WORKSPACE_OPERATION,
            {"operation": "list", "profile_id": self.store.profile_id},
        )
        selected = identifier(before["selected_workspace_id"])
        selected_mount = next(
            (m for m in before["mounts"] if m["id"] == selected), None
        )
        if selected_mount is None:
            raise LookupError("select a Host workspace before capturing files")
        files = {}
        for path in paths:
            self.guard()
            value = self.client.invoke(
                INSPECT,
                INSPECT_OPERATION,
                {
                    "operation": "read",
                    "profile_id": self.store.profile_id,
                    "workspace_id": selected,
                    "path": path,
                    "encoding": "utf-8",
                    "require_selected": True,
                },
            )
            if (
                value.get("path") != path
                or value.get("workspace_id") != selected
                or not isinstance(value.get("content"), str)
            ):
                raise ValueError("Host workspace capture returned an invalid file")
            data = value["content"].encode("utf-8")
            # File inspection is text-only. Reject truncated, lossy or partial
            # output instead of labelling it a complete binary checkpoint.
            if (
                value.get("size") != len(data)
                or value.get("encoding") != "utf-8"
                or value.get("start_line") != 1
                or value.get("end_line") != value.get("total_lines")
            ):
                raise ValueError("Host workspace capture requires complete UTF-8 files")
            files[path] = data
        after = self.client.invoke(
            WORKSPACE,
            WORKSPACE_OPERATION,
            {"operation": "list", "profile_id": self.store.profile_id},
        )
        after_mount = next((m for m in after["mounts"] if m["id"] == selected), None)
        if (
            after["selected_workspace_id"] != selected
            or after_mount != selected_mount
            or after["revision"] != before["revision"]
        ):
            raise Conflict("selected Host workspace changed during capture")
        self.guard()
        return files
