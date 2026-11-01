"""Finite captured container-task providers; legacy receipt routing is excluded."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from core_runtime.host_provider_backend_v4 import (
    CapturedHostProviderV4,
    HostProviderCaptureContextV4,
    HostProviderContributionV4,
    HostProviderDataRequestV4,
    HostProviderInvocationContextV4,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_container import (
    CLOUD,
    ContainerTasks,
)
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_state import TaskState
from ecosystem.rumi_coding_sandbox_service_pack.runtime.task_execution import (
    GUEST_FILES,
)
from tobkiri_protocol.workspace_capsule_v1 import (
    canonical,
    content_digest,
    digest,
    identifier,
    parse_json,
)
from tobkiri_protocol.workspace_task_v1 import (
    SANDBOX_PACK,
    TASK_CONTRACT,
    TASK_RESOURCE,
    PREPARE,
    EXECUTE,
    RESOURCE,
    validate_task_image,
)

CLOUD_PACK = "tobkiri_cloud_workspace_pack"
DECLARATIONS = {
    "prepare": (TASK_CONTRACT, PREPARE),
    "execute": (TASK_CONTRACT, EXECUTE),
    "resource": (TASK_RESOURCE, RESOURCE),
}


def reviewed_recipe(context: HostProviderCaptureContextV4) -> dict[str, Any]:
    """Read immutable source-selected bytes instead of opening another Pack's path."""
    records = [
        file
        for record in context.declared_pack_data
        if record.pack_id == CLOUD_PACK and record.path_prefix == "container/"
        for file in record.files
        if file.path == "container/task-recipe.v1.json"
    ]
    if len(records) != 1:
        raise PermissionError("reviewed container task recipe is unavailable")
    file = records[0]
    if file.digest != digest(file.content):
        raise PermissionError("reviewed container task recipe changed")
    recipe = parse_json(file.content)
    if (
        set(recipe)
        != {
            "version",
            "image_reference",
            "portable_recipe_digest",
            "network",
            "nonroot",
            "read_only_root",
            "max_timeout_seconds",
            "max_work_bytes",
            "workspace_tmpfs_bytes",
            "workspace_tmpfs_inodes",
            "tmp_tmpfs_bytes",
            "tmp_tmpfs_inodes",
            "files",
        }
        or recipe["version"] != "tobkiri.workspace-task-recipe.v1"
        or recipe["network"] != "none"
        or recipe["nonroot"] is not True
        or recipe["read_only_root"] is not True
        or recipe["max_timeout_seconds"] != 120
        or recipe["max_work_bytes"] != 4 * 1024 * 1024
        or recipe["workspace_tmpfs_bytes"] != 8 * 1024 * 1024
        or recipe["workspace_tmpfs_inodes"] != 512
        or recipe["tmp_tmpfs_bytes"] != 16 * 1024 * 1024
        or recipe["tmp_tmpfs_inodes"] != 256
    ):
        raise PermissionError("reviewed container task recipe policy is invalid")
    validate_task_image(recipe["image_reference"])
    content_digest(recipe["portable_recipe_digest"])
    return recipe


def reviewed_guest_files(
    context: HostProviderCaptureContextV4, recipe: Mapping[str, Any]
) -> dict[str, bytes]:
    """Verify the exact recipe-bound guest bytes from immutable selected Pack data."""
    files = {
        file.path: file
        for record in context.declared_pack_data
        if record.pack_id == CLOUD_PACK and record.path_prefix == "container/"
        for file in record.files
    }
    declarations = recipe["files"]
    if (
        not isinstance(declarations, list)
        or len(declarations) != len(GUEST_FILES)
        or {entry.get("path") for entry in declarations} != set(GUEST_FILES)
    ):
        raise PermissionError("guest task recipe source set is invalid")
    result = {}
    for entry in declarations:
        if set(entry) != {"path", "digest"} or entry["path"] not in files:
            raise PermissionError("guest task source is unavailable")
        file = files[entry["path"]]
        if entry["digest"] != file.digest or digest(file.content) != file.digest:
            raise PermissionError("guest task source digest differs")
        result[file.path] = file.content
    return result


class ContainerTaskHostFactoryV4:
    """Bind exact task operations to authenticated Broker capture and owned state."""

    declared_pack_data = (HostProviderDataRequestV4(CLOUD_PACK, "container/"),)

    def __init__(self, kind: str) -> None:
        """Select only one statically reviewed task function."""
        self.kind = kind
        self.function_id = SANDBOX_PACK + ".task-" + kind
        self.contract_id, self.operation_id = DECLARATIONS[kind]
        if kind == "execute":
            self.cancellation_group = "workspace-task"
            self.cancellation_role = "execute"

    def capture(self, context: HostProviderCaptureContextV4) -> CapturedHostProviderV4:
        """Capture current Profile/Plan/source and reject incomplete finite bindings."""
        if context.user_data_root is None or len(context.provider_bindings) != 1:
            raise PermissionError("workspace task capture is incomplete")
        binding = context.provider_bindings[0]
        key = (self.contract_id, self.operation_id, binding.principal_ref.value)
        domain = context.domain_ids.get(key)
        if (
            binding.function.function_id != self.function_id
            or binding.operation.contract_id != self.contract_id
            or binding.operation.operation_id != self.operation_id
            or binding.operation.contract_version != "1.0.0"
            or not domain
        ):
            raise PermissionError("workspace task capture identity is invalid")
        recipe = reviewed_recipe(context)
        tasks = ContainerTasks(
            TaskState(context.user_data_root, context.profile_id),
            profile_id=context.profile_id,
            plan_digest=context.plan_digest,
            security_epoch=context.security_epoch,
            recipe=recipe,
            guest_files=reviewed_guest_files(context, recipe),
        )

        def invoke(
            operation_id: str,
            payload: Mapping[str, Any],
            invocation: HostProviderInvocationContextV4,
        ) -> Mapping[str, Any]:
            invocation.assert_current()
            envelope = invocation.envelope
            captured = envelope.context
            if (
                operation_id != self.operation_id
                or captured.profile_id != context.profile_id
                or captured.plan_digest != context.plan_digest
                or captured.security_epoch != context.security_epoch
                or envelope.target_principal.value != binding.principal_ref.value
                or captured.target_domain_id != domain
            ):
                raise PermissionError("workspace task invocation scope differs")
            # Transport session context is Broker-owned, not application authority.
            values = {
                key: value for key, value in payload.items() if key != "_session_id"
            }
            owner = digest(
                canonical(
                    [
                        invocation.presentation_owner_principal_id,
                        invocation.presentation_owner_session_id,
                    ]
                )
            )
            if self.kind == "resource":
                if values == {
                    "profile_id": context.profile_id,
                    "operation": "availability",
                }:
                    return {
                        "status": "ready",
                        "reason": "Local container argv tasks require Host approval",
                        "cloud_connected": False,
                        "workload_started": False,
                        "image_reference": tasks.recipe["image_reference"],
                    }
                if (
                    set(values) != {"profile_id", "operation", "task_id"}
                    or values["profile_id"] != context.profile_id
                    or values["operation"] not in {"status", "export"}
                ):
                    raise PermissionError("workspace task resource fields are invalid")
                result = tasks.resource(
                    identifier(values["task_id"]),
                    owner,
                    export=values["operation"] == "export",
                )
                invocation.assert_current()
                return result
            client = invocation.contract_client(
                allowed_contract_ids=frozenset({CLOUD}),
                consumer_pack_id=SANDBOX_PACK,
                include_credentials=False,
            )
            if self.kind == "prepare":
                return tasks.prepare(
                    values,
                    client,
                    owner=owner,
                    request_id=captured.request_id,
                    guard=invocation.assert_current,
                )
            return tasks.execute(
                values,
                client,
                owner=owner,
                guard=invocation.assert_current,
                cancel_event=envelope.cancellation_requested,
                track=invocation.cancellation.track,
            )

        return CapturedHostProviderV4(
            (
                HostProviderContributionV4(
                    contract_id=self.contract_id,
                    contract_version="1.0.0",
                    operation_id=self.operation_id,
                    principal_id=binding.principal_ref.value,
                    artifact_digest=binding.artifact.digest,
                    implementation_digest=binding.function.implementation_digest,
                    domain_id=domain,
                    invoke=invoke,
                ),
            ),
            lambda: None,
        )


HOST_PROVIDER_FACTORY = {
    SANDBOX_PACK + ".task-" + kind: ContainerTaskHostFactoryV4(kind)
    for kind in DECLARATIONS
}
