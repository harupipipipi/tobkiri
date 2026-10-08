"""Finite native-visible routes from actual verified Profile edges."""

from __future__ import annotations

from typing import Any

from core_runtime.authority.v4 import AuthorityScope, LeaseState

LOCAL_EXECUTOR = "rumi_tool_local_executor_pack.tool-executor.local"
COORDINATOR = "rumi_host_authority_bridge_pack.host-authority.interactive-effect"
FILE_TOOL = "rumi_default_tools_pack.file-create-tool"
LOCAL_OPERATIONS = frozenset(
    {
        "rumi_default_tools_pack.files-read-operation",
        "rumi_default_tools_pack.calculator-evaluate",
    }
)
INNER_CONTRACT = "tobkiri.service.file.create.v1"
INNER_OPERATION = "rumi_default_tools_pack.file-create"


def finite_saved_policy_routes(
    invocation: Any, *, store: Any, edges: tuple, bindings: tuple, workspace_binding: Any
) -> tuple[dict[str, Any], ...]:
    """Capture exact read/calculate and inner native file-create signed ceilings."""
    invocation.assert_current()
    parent, state = store.inspect_lease_token(invocation.envelope.lease.token.decode("ascii"))
    if state is not LeaseState.DISPATCHED:
        raise PermissionError("native policy executor parent is not dispatched")
    local_chain = parent.call_chain + (parent.caller.principal_id,)
    file_bindings = tuple(
        binding for binding in bindings if binding.function.function_id == FILE_TOOL
    )
    if len(file_bindings) != 1:
        raise PermissionError("native policy finite file caller unavailable")
    inner_chain = (file_bindings[0].principal_ref.value,)
    result = []
    for edge in edges:
        binding = edge.resolved_binding
        operation = binding.operation
        if edge.caller.function_id == LOCAL_EXECUTOR and operation.operation_id in LOCAL_OPERATIONS:
            chain = local_chain
        elif edge.caller.function_id == COORDINATOR and (
            operation.contract_id,
            operation.operation_id,
        ) == (INNER_CONTRACT, INNER_OPERATION):
            chain = inner_chain
        else:
            continue
        callers = tuple(
            item for item in bindings if item.principal_ref.value == edge.caller.principal_id
        )
        if len(callers) != 1 or edge.target.principal_id != binding.principal_ref.value:
            raise PermissionError("native policy finite signed caller changed")
        scope = edge.ceilings.caller_effect
        if (
            not isinstance(scope, AuthorityScope)
            or scope.opaque
            or scope.semantics_digest != operation.revision_digest
            or scope.dimensions.get("contract") != (operation.contract_id,)
            or scope.dimensions.get("operation") != (operation.operation_id,)
        ):
            raise PermissionError("native policy finite signed ceiling changed")
        result.append(
            {
                "contract": operation.contract_id,
                "operation": operation.operation_id,
                "caller_principal_id": edge.caller.principal_id,
                "caller_publisher_lineage": callers[0].artifact.publisher_lineage,
                "operation_class": operation.effect_class.value,
                "workspace_receipt": {
                    "workspace_id": workspace_binding.workspace_id,
                    "canonical_root": str(workspace_binding.canonical_root),
                    "mount_revision": workspace_binding.mount_revision,
                    "root_st_dev": workspace_binding.root_st_dev,
                    "root_st_ino": workspace_binding.root_st_ino,
                },
                "ancestor_chain": list(chain),
                "ceiling": scope.to_dict(),
            }
        )
    if len(result) != 3:
        raise PermissionError("native policy finite topology unavailable")
    invocation.assert_current()
    return tuple(result)
