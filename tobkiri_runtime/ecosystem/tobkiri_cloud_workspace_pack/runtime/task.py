"""Cloud workspace task orchestration through finite public approved contracts."""

from __future__ import annotations

import base64
import json
from typing import Any, Mapping

from ecosystem.tobkiri_cloud_workspace_pack.runtime.recipe import (
    RECIPE_DIGEST,
    TASK_RECIPE_DIGEST,
)
from ecosystem.tobkiri_cloud_workspace_pack.runtime.store import Conflict
from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    digest,
    identifier,
    import_archive,
)
from tobkiri_protocol.workspace_task_v1 import (
    TASK_RESOURCE,
    RESOURCE,
    task_identity,
    validate_task_plan,
    validate_task_request,
)

EFFECT = "tobkiri.service.interactive-effect.v1"
EFFECT_OPERATION = "interactive_effect.manage"
DEPENDENCIES = frozenset({EFFECT, TASK_RESOURCE})


class WorkspaceTasks:
    """Prepare ordinary approvals and reconcile verified task output with CAS."""

    def __init__(self, workspace: Any, private_owner: str) -> None:
        """Use the Cloud provider's captured client and private originating owner."""
        self.workspace, self.owner = workspace, private_owner

    def snapshot(self, workspace_id: str) -> dict[str, Any]:
        """Show public task state and distinguish available local work from cloud."""
        current = self.workspace.store.task(workspace_id, self.workspace.actor)
        try:
            if current:
                result = self._resource("status", current["task_id"])
                return {**current, **result, "available": True}
            return self._resource("availability") | {"available": True}
        except (LookupError, ValueError, PermissionError, RuntimeError):
            return {
                "status": "unavailable",
                "available": False,
                "reason": "The approved local container task provider is not bound",
            }

    def invoke(
        self, action: str, values: Mapping[str, Any], *, fingerprint: str
    ) -> dict[str, Any]:
        """Never execute argv here; the Host coordinator captures the approved plan."""
        workspace = self.workspace
        store = workspace.store
        workspace_id = workspace.workspace_id(values["conversation_id"])
        revision, epoch = values["expected_revision"], values["expected_writer_epoch"]
        current = store.get(workspace_id)
        if (
            current is None
            or current["revision"] != revision
            or current["writer_epoch"] != epoch
        ):
            raise Conflict("workspace task checkpoint is stale")
        workspace.guard()
        if action == "task_prepare":
            raw = values["argv_json"]
            if not isinstance(raw, str) or len(raw.encode("utf-8")) > 24_000:
                raise ValueError("workspace task argv JSON is invalid")
            argv = json.loads(raw)
            epoch = store.renew_task_writer(
                workspace_id, revision, epoch, workspace.actor, workspace.guard
            )
            request = validate_task_request(
                {
                    "task_request_id": "request-"
                    + digest(workspace.request_id.encode())[7:39],
                    "profile_id": store.profile_id,
                    "workspace_id": workspace_id,
                    "expected_revision": revision,
                    "expected_writer_epoch": epoch,
                    "argv": argv,
                    "timeout_seconds": 60,
                }
            )
            effect = workspace.client.invoke(
                EFFECT,
                EFFECT_OPERATION,
                {
                    "phase": "prepare",
                    "effect_kind": "workspace_task",
                    "request": request,
                    "correlation_id": request["task_request_id"],
                },
            )
            if not isinstance(effect, Mapping) or not isinstance(
                effect.get("effect_id"), str
            ):
                raise ValueError("workspace task approval is unavailable")
            value = {
                "task_id": task_identity(request, self.owner),
                "effect_id": identifier(effect["effect_id"]),
                "status": "awaiting_approval",
                "request": request,
                "checkpoint_digest": current["checkpoint_digest"],
            }
            store.record_task(workspace_id, workspace.actor, value, workspace.guard)
            return {"task": value, "approval": dict(effect)}
        task = store.task(workspace_id, workspace.actor)
        if task is None:
            raise LookupError("prepare a workspace task first")
        if action in {"task_resume", "task_status", "task_cancel"}:
            phase = action.removeprefix("task_")
            status = workspace.client.invoke(
                EFFECT,
                EFFECT_OPERATION,
                {"phase": phase, "effect_id": task["effect_id"]},
            )
            return {"task": self.snapshot(workspace_id), "approval": status}
        if action != "task_apply":
            raise ValueError("workspace task action is invalid")
        result = self._resource("export", task["task_id"])
        plan = validate_task_plan(result["task_plan"])
        request = task["request"]
        if (
            plan["request_digest"] != digest(canonical(request))
            or plan["expected_revision"] != revision
            or plan["expected_writer_epoch"] != epoch
            or any(plan[key] != value for key, value in request.items())
            or plan["task_id"] != task["task_id"]
            or plan["plan_digest"] != workspace.plan_digest
            or plan["recipe_digest"] != TASK_RECIPE_DIGEST
            or plan["checkpoint_digest"] != current["checkpoint_digest"]
            or result["receipt"].get("status") != "completed"
            or result["receipt"].get("container_cleanup_verified") is not True
        ):
            raise PermissionError("workspace task result binding differs")
        raw = result["archive_base64"]
        if not isinstance(raw, str) or len(raw) > 7_000_000:
            raise ValueError("workspace task result exceeds the task limit")
        archive = base64.b64decode(raw, validate=True)
        manifest, blobs = import_archive(archive)
        if (
            digest(archive) != result["archive_digest"]
            or manifest["workspace_id"] != workspace_id
            or manifest["parent_digest"] != current["checkpoint_digest"]
            or manifest["revision"] != revision + 1
            or manifest["recipe_digest"] != RECIPE_DIGEST
            or manifest["source"]
            != {"profile_id": store.profile_id, "plan_digest": workspace.plan_digest}
            or manifest["manifest_digest"]
            != result["receipt"]["output_checkpoint_digest"]
        ):
            raise PermissionError("workspace task result capsule differs")

        def writer_guard() -> None:
            workspace.guard()
            store.assert_writer(workspace_id, revision, epoch, workspace.actor)

        writer_guard()
        return store.publish(
            manifest,
            blobs,
            expected_revision=revision,
            expected_writer_epoch=epoch,
            actor=workspace.actor,
            request_id=workspace.request_id,
            fingerprint=fingerprint,
            guard=writer_guard,
        )

    def _resource(self, operation: str, task_id: str | None = None) -> dict[str, Any]:
        payload = {
            "profile_id": self.workspace.store.profile_id,
            "operation": operation,
        }
        if task_id is not None:
            payload["task_id"] = task_id
        result = self.workspace.client.invoke(TASK_RESOURCE, RESOURCE, payload)
        if not isinstance(result, Mapping):
            raise ValueError("workspace task resource is invalid")
        self.workspace.guard()
        return dict(result)
