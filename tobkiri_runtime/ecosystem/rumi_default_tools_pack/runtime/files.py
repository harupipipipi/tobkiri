"""Finite file tools projected through the selected workspace's read owner."""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

from core_runtime.host_provider_backend_v4 import (
    HostProviderCaptureContextV4,
    HostProviderInvocationContextV4,
)
from core_runtime.host_provider_function_v4 import (
    HostFunction,
    SingleOperationHostFactoryV4,
)
from ecosystem.rumi_workspace_mount_pack.runtime.mounts import (
    capture_selected_workspace_binding,
)

PACK_ID = "rumi_default_tools_pack"
FUNCTION = f"{PACK_ID}.files-read"
CONTRACT = "tobkiri.service.tool.local.operation.v1"
OPERATION = f"{PACK_ID}.files-read-operation"
FILE_INSPECT = "tobkiri.service.file.inspect.v1"
FILE_OPERATION = "rumi_file_inspect_pack.file-inspect"
WORKSPACE = "tobkiri.resource.workspace.v1"
WORKSPACE_OPERATION = "rumi_workspace_mount_pack.workspace-resource"
ALLOWED_CONTRACTS = frozenset({FILE_INSPECT, WORKSPACE})
TOOLS = {
    "file_reader": ("read", frozenset({"path"}), frozenset({"path"})),
    "coding_file_read": (
        "read",
        frozenset({"path", "start_line", "end_line"}),
        frozenset({"path"}),
    ),
    "coding_file_list": (
        "list",
        frozenset({"directory", "recursive"}),
        frozenset(),
    ),
    "coding_file_search": (
        "search",
        frozenset({"pattern", "directory"}),
        frozenset({"pattern"}),
    ),
}


def _arguments(payload: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    if (
        set(payload) != {"tool_id", "tool_call_id", "arguments"}
        or not isinstance(payload["tool_id"], str)
        or payload["tool_id"] not in TOOLS
        or not isinstance(payload["tool_call_id"], str)
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,255}", payload["tool_call_id"])
        is None
        or not isinstance(payload["arguments"], dict)
    ):
        raise ValueError("File tool invocation payload is invalid")
    name, allowed, required = TOOLS[payload["tool_id"]]
    arguments = dict(payload["arguments"])
    if not required <= arguments.keys() or arguments.keys() - allowed:
        raise ValueError("File tool argument schema is invalid")
    for key, value in arguments.items():
        if key in {"start_line", "end_line"}:
            valid = type(value) is int and 1 <= value <= 10_000_000
        elif key == "recursive":
            valid = type(value) is bool
        else:
            valid = (
                isinstance(value, str) and bool(value.strip()) and len(value) <= 4096
            )
        if not valid:
            raise ValueError("File tool argument schema is invalid")
    if arguments.get("end_line", 10_000_000) < arguments.get("start_line", 1):
        raise ValueError("File tool line range is invalid")
    return name, arguments


def _bind(context: HostProviderCaptureContextV4) -> HostFunction:
    if context.user_data_root is None:
        raise PermissionError("File tools require an owned user data root")
    profile_id = context.profile_id
    user_data_root = context.user_data_root

    def invoke(
        payload: Mapping[str, Any],
        invocation: HostProviderInvocationContextV4,
    ) -> Mapping[str, Any]:
        name, arguments = _arguments(payload)
        invocation.assert_current()
        client = invocation.contract_client(
            allowed_contract_ids=ALLOWED_CONTRACTS,
            consumer_pack_id=PACK_ID,
            include_credentials=False,
        )
        binding = capture_selected_workspace_binding(
            profile_id,
            user_data_root=user_data_root,
        )
        invocation.assert_current()
        if binding.get("access") != "read_only":
            raise PermissionError("File tools require a read-only workspace binding")
        workspace_id = binding["workspace_id"]

        def verify() -> None:
            invocation.assert_current()
            selected = client.invoke(
                WORKSPACE,
                WORKSPACE_OPERATION,
                {"operation": "list", "profile_id": profile_id},
            )
            invocation.assert_current()
            mount = client.invoke(
                WORKSPACE,
                WORKSPACE_OPERATION,
                {
                    "operation": "get",
                    "profile_id": profile_id,
                    "workspace_id": workspace_id,
                },
            )
            invocation.assert_current()
            if (
                not isinstance(selected, Mapping)
                or selected.get("selected_workspace_id") != workspace_id
                or not isinstance(mount, Mapping)
                or mount.get("workspace_id") != workspace_id
                or mount.get("root_path") != binding.get("canonical_root")
                or mount.get("mount_revision") != binding.get("mount_revision")
            ):
                raise PermissionError("Selected workspace binding changed")

        verify()
        result = client.invoke(
            FILE_INSPECT,
            FILE_OPERATION,
            {
                **arguments,
                "name": name,
                "profile_id": profile_id,
                "workspace_id": workspace_id,
                "require_selected": True,
                "_workspace_binding": binding,
            },
        )
        verify()
        if (
            not isinstance(result, Mapping)
            or result.get("workspace_id") != workspace_id
        ):
            raise PermissionError("File inspection owner response is invalid")
        return {
            "result": json.dumps(dict(result), ensure_ascii=False, sort_keys=True),
            "is_error": False,
            "widget": None,
        }

    return invoke


HOST_PROVIDER_FACTORY = SingleOperationHostFactoryV4(
    function_id=FUNCTION,
    contract_id=CONTRACT,
    operation_id=OPERATION,
    bind=_bind,
)
