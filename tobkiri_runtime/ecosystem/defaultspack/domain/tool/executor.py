from collections.abc import Callable, Mapping
from typing import Any, Protocol

from .registry import ToolRegistry
from .mcp_client import McpClient
from .mcp_registry import McpRegistry
from .autonomy import autonomous_tool_execution_allowed
from .eligibility import rejection_result
from .permission_resolver import ToolPermissionResolver
from .schema_adapter import (
    is_tool_rejected_by_policy,
    list_or_empty,
    mapping_or_empty,
    policy_from_context,
)
from .service_catalog import infer_action_class
from .security import (
    appears_write_or_execute_capable,
    execution_type,
    is_sandbox_capability_tool,
    is_safe_first_party_memo_tool,
    is_trusted_pack_id,
    requires_approval_for_security,
    untrusted_tool_security_rejection,
    unsupported_execution_reason,
)
from domain.adaptive.guard import guard_tool_execution, tool_guard_response
from domain.tool_policy.audit import audit_tool_policy
from domain.tool_policy.internal_context import (
    internal_tool_decision_allows,
    mark_tool_server_approval_context,
    seal_tool_context,
    tool_server_approval_context_is_internal,
)
from domain.tool_policy.profile_permission import resolve_profile_tool_permission
from domain.tool_policy.risk import resolve_tool_risk
from core_runtime.capability_plan import (
    CapabilityPlanValidationError,
    validate_capability_plan,
)
from pathlib import Path
import hashlib
import inspect
import json
import logging
import os
import re
import time

logger = logging.getLogger(__name__)


class SubagentRunner(Protocol):
    """Typed boundary for the pack-owned nested-agent runner."""

    def run(
        self,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> Mapping[str, Any]:
        """Run a nested-agent request and return its structured result."""


SubagentFactory = Callable[[], SubagentRunner]


# P1-2: サンドボックス用の安全なビルトイン一覧
_SAFE_BUILTINS = {
    "None": None,
    "True": True,
    "False": False,
    "abs": abs,
    "all": all,
    "any": any,
    "bool": bool,
    "bytes": bytes,
    "callable": callable,
    "chr": chr,
    "dict": dict,
    "divmod": divmod,
    "enumerate": enumerate,
    "filter": filter,
    "float": float,
    "format": format,
    "frozenset": frozenset,
    "getattr": getattr,
    "hasattr": hasattr,
    "hash": hash,
    "hex": hex,
    "id": id,
    "int": int,
    "isinstance": isinstance,
    "issubclass": issubclass,
    "iter": iter,
    "len": len,
    "list": list,
    "map": map,
    "max": max,
    "min": min,
    "next": next,
    "oct": oct,
    "ord": ord,
    "pow": pow,
    "print": print,
    "range": range,
    "repr": repr,
    "reversed": reversed,
    "round": round,
    "set": set,
    "slice": slice,
    "sorted": sorted,
    "str": str,
    "sum": sum,
    "tuple": tuple,
    "type": type,
    "zip": zip,
    # 明示的に除外: __import__, exec, eval, compile, open, globals, locals,
    #               breakpoint, exit, quit, input, memoryview, vars, dir,
    #               delattr, setattr, super, classmethod, staticmethod,
    #               property, object, __build_class__
}

_STALE_APPROVAL_TOKEN_CODES = {
    "APPROVAL_ARGUMENTS_CHANGED",
    "APPROVAL_OPERATION_MISMATCH",
    "APPROVAL_PACK_MISMATCH",
    "APPROVAL_CONVERSATION_MISMATCH",
    "APPROVAL_EXPIRED",
    "APPROVAL_NOT_APPROVED",
    "APPROVAL_REQUEST_MISSING",
    "APPROVAL_TOKEN_USED",
}
_FRONTEND_PERMISSION_FAIL_CLOSED_ACTIONS = {"create", "update", "send", "execute", "computer", "delete"}
_FRONTEND_PERMISSION_FAIL_CLOSED_RISKS = {
    "file_write",
    "file_delete",
    "shell",
    "computer",
    "credential",
    "git_write",
    "git_push",
    "external_message",
    "scheduler_create",
    "capability_mutation",
    "pack_install",
}
_COMPUTER_APPROVAL_PROMPT = (
    "承認してください。foreground/on-screen 操作も利用できます。"
    "リクエストを承認するか、表/前面で作業しますか?"
)


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _capability_plan_tool_rejection(
    tool_name,
    tool_def,
    context,
    *,
    require_plan=False,
):
    """Fail closed unless a canonical plan attaches this exact Tool.

    The public Capability API owns persisted approval and owner binding.  The
    executor still validates the detached plan at the last non-core boundary,
    so a direct adapter call cannot use a legacy alias or an unsigned plan to
    reach a reviewed pack function.
    """

    if not isinstance(context, dict):
        if not require_plan:
            return None
        return {
            "result": "CapabilityPlan is required for tool execution",
            "is_error": True,
            "widget": None,
            "error_type": "capability_plan_required",
        }
    plan = context.get("capability_plan")
    if not isinstance(plan, dict):
        if not require_plan:
            return None
        return {
            "result": "CapabilityPlan is required for tool execution",
            "is_error": True,
            "widget": None,
            "error_type": "capability_plan_required",
        }
    try:
        plan = validate_capability_plan(plan)
    except (CapabilityPlanValidationError, TypeError, ValueError):
        return {
            "result": "CapabilityPlan authority is invalid",
            "is_error": True,
            "widget": None,
            "error_type": "capability_plan_invalid",
        }
    tools = plan.get("tools")
    if not isinstance(tools, dict):
        return {
            "result": "CapabilityPlan Tool authority is invalid",
            "is_error": True,
            "widget": None,
            "error_type": "capability_plan_invalid",
        }
    attached = {
        str(item).strip()
        for item in tools.get("attached") or []
        if str(item or "").strip()
    }
    canonical_name = str(
        tool_def.get("tool_id")
        or tool_def.get("name")
        or tool_name
        or ""
    ).strip()
    requested_name = str(tool_name or "").strip()
    if requested_name != canonical_name:
        return {
            "result": "Legacy Tool aliases cannot authorize execution",
            "is_error": True,
            "widget": None,
            "error_type": "legacy_tool_alias",
        }
    if canonical_name not in attached:
        return {
            "result": "Tool is not attached by the active CapabilityPlan",
            "is_error": True,
            "widget": None,
            "error_type": "tool_not_attached",
        }
    schema_hashes = tools.get("schema_hashes")
    expected_hash = (
        str(schema_hashes.get(canonical_name) or "").strip()
        if isinstance(schema_hashes, dict)
        else ""
    )
    schema = tool_def.get("schema")
    if not isinstance(schema, dict):
        contract = tool_def.get("contract")
        schema = (
            contract.get("input_schema")
            if isinstance(contract, dict)
            and isinstance(contract.get("input_schema"), dict)
            else {}
        )
    actual_hash = hashlib.sha256(
        json.dumps(
            schema,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    if not expected_hash or expected_hash != actual_hash:
        return {
            "result": "Tool schema does not match the active CapabilityPlan",
            "is_error": True,
            "widget": None,
            "error_type": "tool_schema_revision_mismatch",
        }
    plan_owner = plan.get("owner") or plan.get("authority_owner")
    if plan_owner is not None:
        if not isinstance(plan_owner, dict):
            return {
                "result": "CapabilityPlan owner binding is invalid",
                "is_error": True,
                "widget": None,
                "error_type": "capability_plan_owner_mismatch",
            }
        context_owner = context.get("capability_plan_owner")
        if not isinstance(context_owner, dict):
            context_owner = context
        for field in ("principal_id", "workspace_id", "conversation_id", "profile_id"):
            expected = str(plan_owner.get(field) or "").strip()
            actual = str(context_owner.get(field) or "").strip()
            if expected and actual != expected:
                return {
                    "result": "CapabilityPlan owner does not match execution scope",
                    "is_error": True,
                    "widget": None,
                    "error_type": "capability_plan_owner_mismatch",
                }
    return None


def _tool_requires_capability_plan(tool_def):
    """Return whether this executor path is an authority-bearing action."""

    if not isinstance(tool_def, dict):
        return True
    exec_type = execution_type(tool_def)
    if exec_type in {
        "capability",
        "global_contract",
        "handler",
        "mcp",
        "rumi_function",
        "dynamic",
    }:
        return True
    return appears_write_or_execute_capable(tool_def)


def _approval_module():
    from domain.safety import approval

    return approval


class ToolExecutor:
    """ツール実行エンジン"""

    def __init__(self, *, subagent_factory: SubagentFactory | None = None):
        try:
            from domain.integrations.secrets import load_integration_secrets_into_env

            load_integration_secrets_into_env()
        except Exception:
            pass
        self._registry = ToolRegistry()
        self._mcp_client = McpClient()
        self._subagent_factory = subagent_factory

    def execute(self, tool_name, arguments, context):
        """
        ツールを実行する。
        戻り値: {"result": str, "is_error": bool, "widget": dict|None}
        """
        if _is_cancelled(context):
            return _cancelled_tool_result(tool_name)
        filtered_rejection = _filtered_tool_rejection(tool_name, context)
        if filtered_rejection is not None:
            return {
                "result": "Tool '{}' was rejected: {}".format(tool_name, filtered_rejection.get("reason") or filtered_rejection.get("code")),
                "is_error": True,
                "widget": {"type": "tool_rejection", **filtered_rejection},
                **filtered_rejection,
            }
        tool_def = self._registry.get(tool_name)
        if tool_def is None:
            return {
                "result": "Tool '{}' not found".format(tool_name),
                "is_error": True,
                "widget": None
            }
        plan_rejection = _capability_plan_tool_rejection(
            tool_name,
            tool_def,
            context,
            require_plan=_tool_requires_capability_plan(tool_def),
        )
        if plan_rejection is not None:
            return plan_rejection
        adaptive_decision = guard_tool_execution(
            tool_name,
            arguments if isinstance(arguments, dict) else {},
            context if isinstance(context, dict) else {},
            tool_def=tool_def if isinstance(tool_def, dict) else None,
        )
        if adaptive_decision is not None:
            return tool_guard_response(adaptive_decision, tool_name)
        policy = policy_from_context(context if isinstance(context, dict) else {})
        if is_tool_rejected_by_policy(tool_def, policy):
            return {
                "result": "Tool '{}' rejected by runtime policy".format(tool_name),
                "is_error": True,
                "widget": None,
                "rejected_by_policy": True,
            }
        security_rejection = unsupported_execution_reason(tool_def)
        if security_rejection is not None:
            return {
                "result": "Tool '{}' rejected by tool security policy: {}".format(
                    tool_name,
                    security_rejection,
                ),
                "is_error": True,
                "widget": None,
                "rejected_by_security": True,
            }
        untrusted_rejection = untrusted_tool_security_rejection(tool_def)
        if untrusted_rejection is not None:
            return {
                "result": "Tool '{}' rejected by tool security policy: {}".format(
                    tool_name,
                    untrusted_rejection,
                ),
                "is_error": True,
                "widget": {
                    "type": "tool_execution_denied",
                    "tool_name": tool_name,
                    "reason": untrusted_rejection,
                },
                "rejected_by_security": True,
            }

        context, delegated_review_response = _preflight_delegated_approval(
            tool_name,
            tool_def,
            arguments,
            context,
        )
        if delegated_review_response is not None:
            return delegated_review_response

        context, permission_response = _preflight_profile_tool_permission(
            tool_name,
            tool_def,
            arguments,
            context,
            policy,
        )
        if permission_response is not None:
            return permission_response

        context, settings_permission_response = _preflight_frontend_tool_permission(
            tool_name,
            tool_def,
            arguments,
            context,
            policy,
        )
        if settings_permission_response is not None:
            return settings_permission_response

        execution = tool_def.get("execution", {})
        exec_type = execution.get("type", "local")

        if exec_type == "mcp":
            server_name = execution.get("server_name", "")
            mcp_tool_name = execution.get("mcp_tool_name", tool_name)
            if not McpRegistry().is_approved(server_name):
                return {
                    "result": "MCP server '{}' is not approved for tool execution".format(server_name),
                    "is_error": True,
                    "widget": None,
                    "approval_required": True,
                }
            try:
                from domain.safety.audit import record_execution

                record_execution(
                    "tool.mcp_call",
                    "medium",
                    {"server_name": server_name, "tool_name": mcp_tool_name},
                )
            except Exception:
                pass
            return self._mcp_client.invoke(server_name, mcp_tool_name, arguments)

        if exec_type == "rumi_function":
            approval = _preflight_user_requested_computer_approval(tool_name, tool_def, arguments, context)
            if approval is not None:
                return approval
            return self._execute_rumi_function(tool_def, arguments, context)

        if exec_type == "capability":
            return self._execute_capability(tool_def, arguments, context)

        if exec_type == "global_contract":
            return self._execute_global_contract(tool_def, arguments, context)

        if exec_type == "dynamic":
            return self._execute_dynamic(tool_def, arguments, context)

        handler = str(execution.get("handler") or "")
        if handler and not handler.endswith(":ToolExecutor.execute"):
            return self._execute_handler(tool_def, arguments, context)

        return self._execute_local_with_tool_def(tool_name, arguments, context, tool_def)

    def _execute_rumi_function(self, tool_def, arguments, context):
        execution = tool_def.get("execution", {}) if isinstance(tool_def, dict) else {}
        qualified_name = str(execution.get("qualified_name") or "").strip()
        if not qualified_name:
            return {
                "result": "Tool '{}' has no rumi_function qualified_name".format(
                    tool_def.get("name", tool_def.get("tool_id", "tool"))
                ),
                "is_error": True,
                "widget": None,
            }
        pack_id, _, function_id = qualified_name.partition(":")
        request = {
            "type": "function.call",
            "qualified_name": qualified_name,
            "args": arguments or {},
        }
        timeout_seconds = _execution_timeout_seconds(tool_def)
        if timeout_seconds is not None:
            request["timeout_seconds"] = timeout_seconds
        if isinstance(context, dict) and context.get("request_id"):
            request["request_id"] = context.get("request_id")
        approved_context, approval_error = _context_with_tool_approval_token(context, tool_def, arguments)
        if approval_error is not None:
            return approval_error
        local_tool = self._eager_first_party_local_tool_for_function(pack_id, function_id)
        if local_tool:
            if _requires_approval(tool_def) and not _context_has_tool_server_approval(approved_context):
                return _approval_required_tool_response(tool_def, arguments or {}, approved_context)
            return self._execute_local_with_tool_def(
                local_tool,
                arguments or {},
                approved_context,
                tool_def,
            )
        if _requires_rumi_api_request_approval(tool_def, arguments) and not _context_has_tool_server_approval(approved_context):
            approval_arguments = _browser_computer_preflight_approval_arguments(
                _tool_approval_tool_name(tool_def),
                arguments,
                approved_context,
            )
            return _approval_required_tool_response(tool_def, approval_arguments, approved_context)
        forwarded_context = _function_call_context(approved_context, tool_def)
        if forwarded_context:
            request["context"] = forwarded_context
        return self._execute_capability_request(tool_def, request, approved_context)

    def _execute_capability(self, tool_def, arguments, context):
        execution = tool_def.get("execution", {}) if isinstance(tool_def, dict) else {}
        permission_id = str(execution.get("permission_id") or "").strip()
        if not permission_id:
            return {
                "result": "Tool '{}' has no capability permission_id".format(
                    tool_def.get("name", tool_def.get("tool_id", "tool"))
                ),
                "is_error": True,
                "widget": None,
            }
        request = {
            "permission_id": permission_id,
            "args": arguments or {},
        }
        qualified_name = str(execution.get("qualified_name") or "").strip()
        if qualified_name:
            request["qualified_name"] = qualified_name
        approved_context, approval_error = _context_with_tool_approval_token(context, tool_def, arguments)
        if approval_error is not None:
            return approval_error
        return self._execute_capability_request(tool_def, request, approved_context)

    def _execute_global_contract(self, tool_def, arguments, context):
        """Invoke one selected global service without importing its provider."""

        from core_runtime.di_container import get_container
        from core_runtime.global_contract_dispatch import (
            GlobalContractInvocationError,
            GlobalContractUnavailable,
            captured_profile_id,
            invoke_global_contract,
            invoke_selected_global_provider,
        )

        execution = (
            tool_def.get("execution", {})
            if isinstance(tool_def, dict)
            else {}
        )
        contract_id = str(execution.get("contract_id") or "").strip()
        operation = str(execution.get("operation") or "").strip()
        provider_instance_id = str(
            execution.get("provider_instance_id") or ""
        ).strip()
        if not contract_id or not operation:
            return {
                "result": "Global contract execution descriptor is incomplete",
                "is_error": True,
                "widget": None,
            }
        requirements = tool_def.get("capability_requirements")
        requirements = (
            requirements if isinstance(requirements, dict) else {}
        )
        declared_connections = {
            str(item)
            for item in requirements.get("connections") or []
            if str(item).strip()
        }
        if contract_id not in declared_connections:
            return {
                "result": "Global contract was not declared by the Tool",
                "is_error": True,
                "widget": None,
            }
        registry = (
            context.get("v4_dispatch_session")
            if isinstance(context, dict)
            else None
        ) or get_container().get_or_none("v4_dispatch_session")
        if registry is None:
            return {
                "result": "Global contract runtime is unavailable",
                "is_error": True,
                "widget": None,
            }
        arguments = dict(arguments or {})
        requested_profile = str(arguments.get("profile_id") or "").strip()
        profile_id = captured_profile_id(registry)
        if requested_profile and requested_profile != profile_id:
            return {
                "result": "Tool requested an inactive profile",
                "is_error": True,
                "widget": None,
            }
        source_pack_id = str(
            tool_def.get("source_pack_id")
            or (
                tool_def.get("metadata", {}).get("source_pack_id")
                if isinstance(tool_def.get("metadata"), dict)
                else ""
            )
            or ""
        ).strip()
        context_workspace_id = str(
            (context.get("workspace_id") or "")
            if isinstance(context, dict)
            else ""
        ).strip()
        requested_workspace_id = str(
            arguments.get("workspace_id") or ""
        ).strip()
        if requested_workspace_id and (
            not context_workspace_id
            or requested_workspace_id != context_workspace_id
        ):
            return {
                "result": "Tool requested a workspace outside the active binding",
                "is_error": True,
                "widget": None,
                "error_type": "workspace_binding_mismatch",
            }
        if "workspace_id" in arguments or context_workspace_id:
            if not context_workspace_id:
                return {
                    "result": "Active workspace binding is required",
                    "is_error": True,
                    "widget": None,
                    "error_type": "workspace_binding_missing",
                }
            arguments["workspace_id"] = context_workspace_id
        workspace_binding = None
        if context_workspace_id:
            try:
                workspace_snapshot = invoke_global_contract(
                    registry,
                    "rumi.resource.workspace.v1",
                    "list",
                    {"profile_id": profile_id},
                )
                selected_workspace_id = (
                    str(workspace_snapshot.get("selected_workspace_id") or "").strip()
                    if isinstance(workspace_snapshot, dict)
                    else ""
                )
                if (
                    not isinstance(workspace_snapshot, dict)
                    or selected_workspace_id != context_workspace_id
                ):
                    raise ValueError(
                        "selected workspace changed "
                        f"(expected {context_workspace_id!r}, "
                        f"active {selected_workspace_id!r})"
                    )
                workspace_mount = invoke_global_contract(
                    registry,
                    "rumi.resource.workspace.v1",
                    "get",
                    {
                        "profile_id": profile_id,
                        "workspace_id": context_workspace_id,
                    },
                )
                if not isinstance(workspace_mount, dict):
                    raise ValueError("workspace mount is unavailable")
                root = Path(
                    str(workspace_mount.get("root_path") or "")
                ).resolve(strict=True)
                root_stat = root.stat()
                mount_revision = str(
                    workspace_mount.get("revision")
                    or workspace_mount.get("updated_at_ms")
                    or workspace_mount.get("updated_at")
                    or ""
                )
                workspace_binding = {
                    "workspace_id": context_workspace_id,
                    "access": "read_only",
                    "mount_revision": mount_revision,
                    "canonical_root": str(root),
                    "root_st_dev": int(root_stat.st_dev),
                    "root_st_ino": int(root_stat.st_ino),
                }
                workspace_binding["root_identity"] = hashlib.sha256(
                    json.dumps(
                        workspace_binding,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
            except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                workspace_binding = _trusted_full_access_workspace_binding(
                    context,
                    context_workspace_id,
                )
                if workspace_binding is None:
                    return {
                        "result": f"Active workspace binding failed: {exc}",
                        "is_error": True,
                        "widget": None,
                        "error_type": "workspace_binding_invalid",
                    }
        payload = {
            **arguments,
            "profile_id": profile_id,
            "_contract_consumer_pack_id": source_pack_id,
            "_contract_consumer_function_id": str(
                tool_def.get("tool_id") or tool_def.get("name") or ""
            ),
        }
        if isinstance(context, dict):
            capability_plan = context.get("capability_plan")
            if isinstance(capability_plan, dict):
                payload["capability_plan"] = dict(capability_plan)
            payload["registry_revision"] = str(
                context.get("registry_revision")
                or context.get("catalog_revision")
                or (
                    capability_plan.get("registry_revision")
                    if isinstance(capability_plan, dict)
                    else ""
                )
                or getattr(registry, "plan_digest", "")
                or ""
            )
            if not payload["registry_revision"]:
                return {
                    "result": "Active registry revision is required",
                    "is_error": True,
                    "widget": None,
                    "error_type": "registry_revision_missing",
                }
            payload["topology_revision"] = str(
                context.get("topology_revision") or ""
            )
            timeout_ms = max(
                1,
                int(execution.get("timeout_ms") or 120_000),
            )
            payload["_deadline_epoch_ms"] = int(time.time() * 1000) + timeout_ms
            payload["_cancellation_token"] = context.get(
                "cancellation_token"
            ) or context.get("is_cancelled")
            payload["_host_enforcement"] = {
                "tool_allowlist": "host_enforced",
                "workspace_scope": "host_enforced",
                "output_schema": "host_validated",
                "system_prompt": "behavioral_only",
            }
            host_allowed = [
                "file.inspect",
                "ai.gateway.generate",
                "subagent.placement.compile",
            ]
            tool_capability_grants = (
                capability_plan.get("tools", {}).get(
                    "capability_grants", {}
                )
                if isinstance(capability_plan, dict)
                and isinstance(capability_plan.get("tools"), dict)
                else {}
            )
            exact_tool_grants = (
                tool_capability_grants.get(
                    str(
                        tool_def.get("tool_id")
                        or tool_def.get("name")
                        or ""
                    ),
                    [],
                )
                if isinstance(tool_capability_grants, dict)
                else []
            )
            if "repository.content.external_share" in {
                str(value) for value in exact_tool_grants
            }:
                host_allowed.append("repository.content.external_share")
            host_denied = [
                "file.write",
                "git.publish",
                "secret.read",
                "terminal.execute",
            ]
            for target in (
                "_profile_policy",
                "_workspace_policy",
                "_host_policy",
                "_task_grant",
            ):
                payload[target] = {
                    "allowed_capabilities": list(host_allowed),
                    "denied_capabilities": list(host_denied),
                }
            if workspace_binding is None:
                return {
                    "result": "Active workspace binding is required",
                    "is_error": True,
                    "widget": None,
                    "error_type": "workspace_binding_missing",
                }
            payload["_workspace_binding"] = workspace_binding
            canonical_arguments = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            idempotency_dimensions = {
                "query": str(arguments.get("query") or ""),
                "model": str(
                    arguments.get("model")
                    or context.get("model")
                    or context.get("selected_model")
                    or ""
                ),
                "prompt_hash": str(
                    context.get("prompt_hash")
                    or context.get("prompt_revision")
                    or ""
                ),
                "file_hashes": sorted(
                    str(value)
                    for value in (
                        arguments.get("file_hashes")
                        or context.get("file_hashes")
                        or []
                    )
                    if str(value).strip()
                ),
                "batch": {
                    "max_candidates": arguments.get("max_candidates"),
                    "max_selected": arguments.get("max_selected"),
                    "max_file_bytes": arguments.get("max_file_bytes"),
                    "total_read_bytes": arguments.get("total_read_bytes"),
                },
                "budget": {
                    "timeout_ms": timeout_ms,
                    "maximum_cost": arguments.get("maximum_cost"),
                },
            }
            payload["_invocation_key"] = hashlib.sha256(
                json.dumps(
                    {
                        "profile_id": profile_id,
                        "tool_id": tool_def.get("tool_id")
                        or tool_def.get("name"),
                        "arguments": arguments,
                        "workspace": payload["_workspace_binding"],
                        "capability_plan": (
                            capability_plan.get("digest")
                            if isinstance(capability_plan, dict)
                            else ""
                        ),
                        "registry_revision": payload["registry_revision"],
                        "dimensions": idempotency_dimensions,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            payload["_invocation_digest"] = hashlib.sha256(
                json.dumps(
                    {
                        "tool_id": tool_def.get("tool_id")
                        or tool_def.get("name"),
                        "arguments": arguments,
                        "workspace": payload["_workspace_binding"],
                        "capability_plan": (
                            capability_plan.get("digest")
                            if isinstance(capability_plan, dict)
                            else ""
                        ),
                        "registry_revision": payload["registry_revision"],
                        "dimensions": idempotency_dimensions,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                ).encode("utf-8")
            ).hexdigest()
            authority_arguments = {
                "consumer_pack_id": source_pack_id,
                "consumer_function_id": str(
                    tool_def.get("tool_id") or tool_def.get("name") or ""
                ),
                "capability_plan_digest": (
                    capability_plan.get("digest")
                    if isinstance(capability_plan, dict)
                    else ""
                ),
                "attached_tool_id": str(
                    tool_def.get("tool_id") or tool_def.get("name") or ""
                ),
                "tool_definition_hash": hashlib.sha256(
                    json.dumps(
                        tool_def,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                        default=str,
                    ).encode("utf-8")
                ).hexdigest(),
                "workspace_binding": payload["_workspace_binding"],
                "external_share_granted": (
                    "repository.content.external_share" in host_allowed
                ),
                "profile_policy": payload["_profile_policy"],
                "workspace_policy": payload["_workspace_policy"],
                "host_policy": payload["_host_policy"],
                "task_grant": payload["_task_grant"],
                "host_enforcement": payload["_host_enforcement"],
                "registry_revision": payload["registry_revision"],
                "arguments_hash": hashlib.sha256(
                    canonical_arguments.encode("utf-8")
                ).hexdigest(),
                "deadline_epoch_ms": payload["_deadline_epoch_ms"],
                "invocation_key": payload["_invocation_key"],
                "invocation_digest": payload["_invocation_digest"],
                "budget": {
                    "timeout_ms": timeout_ms,
                    "maximum_cost": arguments.get("maximum_cost"),
                },
            }
            if not authority_arguments["external_share_granted"]:
                return {
                    "result": (
                        "CapabilityPlan does not grant repository content "
                        "external sharing"
                    ),
                    "is_error": True,
                    "widget": None,
                    "error_type": "external_share_not_granted",
                }
            authority = invoke_global_contract(
                registry,
                "rumi.service.host.authorize.v1",
                "authorize",
                {
                    "_contract_consumer_pack_id": source_pack_id,
                    "service_pack_id": source_pack_id,
                    "operation": "repository.context.prepare",
                    "authority": "repository.content.external_share",
                    "caller_id": f"tool-executor:{profile_id}",
                    "caller_pack_id": source_pack_id,
                    "caller_function_id": str(
                        tool_def.get("tool_id")
                        or tool_def.get("name")
                        or ""
                    ),
                    "profile_id": profile_id,
                    "workspace_id": context_workspace_id,
                    "session_id": str(
                        context.get("conversation_id")
                        or context.get("session_id")
                        or ""
                    ),
                    "arguments": authority_arguments,
                    "approval_required": False,
                },
            )
            if (
                not isinstance(authority, dict)
                or not authority.get("authorized")
                or not authority.get("receipt")
            ):
                return {
                    "result": "Host authority denied repository context",
                    "is_error": True,
                    "widget": None,
                    "error_type": "host_authority_denied",
                }
            payload["_authority_receipt"] = str(authority["receipt"])
            payload["_authority_scope"] = {
                "service_pack_id": source_pack_id,
                "operation": "repository.context.prepare",
                "authority": "repository.content.external_share",
                "caller_id": f"tool-executor:{profile_id}",
                "caller_pack_id": source_pack_id,
                "caller_function_id": str(
                    tool_def.get("tool_id") or tool_def.get("name") or ""
                ),
                "profile_id": profile_id,
                "workspace_id": context_workspace_id,
                "session_id": str(
                    context.get("conversation_id")
                    or context.get("session_id")
                    or ""
                ),
                "arguments": authority_arguments,
            }
        try:
            if provider_instance_id:
                value = invoke_selected_global_provider(
                    registry,
                    contract_id,
                    provider_instance_id,
                    operation,
                    payload,
                )
            else:
                value = invoke_global_contract(
                    registry,
                    contract_id,
                    operation,
                    payload,
                )
        except (GlobalContractInvocationError, GlobalContractUnavailable) as exc:
            return {
                "result": str(exc),
                "is_error": True,
                "widget": None,
            }
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            return {
                "result": "Global contract request failed: {}".format(exc),
                "is_error": True,
                "widget": None,
            }
        widget = None
        if (
            isinstance(value, dict)
            and value.get("schema_version")
            == "tobkiri.repository-evidence/v1"
        ):
            widget = {
                "type": "repository_evidence",
                "statistics": dict(value.get("statistics") or {}),
                "selected_files": list(value.get("selected_files") or []),
                "excluded_reason_counts": dict(
                    value.get("excluded_reason_counts") or {}
                ),
                "excluded_sample": list(value.get("excluded_files") or []),
                "excluded_artifact_ref": str(
                    value.get("excluded_artifact_ref") or ""
                ),
            }
        return {"result": value, "is_error": False, "widget": widget}

    def _execute_capability_request(self, tool_def, request, context):
        principal_id = self._principal_id(tool_def, context)
        try:
            executor = self._capability_executor(context)
            approval_error = self._prepare_deferred_tool_approval(
                tool_def,
                request,
                context,
                executor,
            )
            if approval_error is not None:
                return approval_error
            consume_error = self._consume_deferred_tool_approval(context)
            if consume_error is not None:
                return consume_error
            response = executor.execute(principal_id, request)
            if getattr(response, "error_type", "") == "caller_requires_denied":
                logger.warning(
                    "tool capability caller_requires denied: tool=%s principal=%s request_type=%s qualified_name=%s approved=%s source=%s approval_id=%s context_keys=%s",
                    _tool_approval_tool_name(tool_def) if isinstance(tool_def, dict) else "",
                    principal_id,
                    str(request.get("type") or ""),
                    str(request.get("qualified_name") or request.get("permission_id") or ""),
                    bool(isinstance(context, dict) and context.get("_tool_server_approved") is True),
                    str((context or {}).get("source") or "") if isinstance(context, dict) else "",
                    str((context or {}).get("approval_id") or "") if isinstance(context, dict) else "",
                    sorted(str(key) for key in (context or {}).keys())[:40] if isinstance(context, dict) else [],
                )
        except Exception as exc:
            return {
                "result": "Capability execution failed: {}".format(exc),
                "is_error": True,
                "widget": None,
            }
        return self._tool_response_from_capability(
            response,
            tool_def,
            request.get("args") or {},
            context,
        )

    @staticmethod
    def _function_call_pack_approval_status(capability_executor, pack_id):
        manager = getattr(capability_executor, "_approval_manager", None)
        if manager is None:
            try:
                from core_runtime.approval_manager import get_approval_manager

                manager = get_approval_manager()
            except Exception:
                manager = None
        if manager is None:
            return False, "approval_manager_unavailable"
        approved = manager.is_pack_approved_and_verified(pack_id)
        if isinstance(approved, tuple):
            return bool(approved[0]), approved[1]
        return bool(approved), None

    def _consume_deferred_tool_approval(self, context):
        if not isinstance(context, dict):
            return None
        if context.get("_tool_server_approval_consumed") is True:
            return None
        token = str(context.get("_tool_server_approval_token") or "").strip()
        operation = str(context.get("_tool_server_approval_operation") or "").strip()
        args_hash = str(context.get("_tool_server_approval_args_hash") or "").strip()
        pack_id = str(context.get("_tool_server_approval_pack_id") or "").strip()
        conversation_id = str(context.get("_tool_server_approval_conversation_id") or "").strip()
        if not token or not operation or not args_hash:
            return None
        verification = _approval_module().verify_execution_token(
            token,
            operation,
            args_hash,
            consume=True,
            pack_id=pack_id,
            conversation_id=conversation_id,
        )
        if verification.valid:
            context["_tool_server_approval_consumed"] = True
            return None
        return {
            "result": verification.message or "approval token is invalid",
            "is_error": True,
            "widget": None,
        }

    def _prepare_deferred_tool_approval(self, tool_def, request, context, capability_executor):
        if not tool_server_approval_context_is_internal(context):
            return None
        if str(request.get("type") or "").strip() == "function.call":
            qualified_name = str(request.get("qualified_name") or "").strip()
            pack_id, _, function_id = qualified_name.partition(":")
            if pack_id:
                if self._first_party_browser_computer_tool_for_function(pack_id, function_id):
                    context["_tool_server_approved"] = True
                    return None
                if is_sandbox_capability_tool(tool_def) and pack_id == "defaultspack":
                    context["_tool_server_approved"] = True
                    return None
                if is_trusted_pack_id(pack_id) and not _requires_approval(tool_def):
                    context["_tool_server_approved"] = True
                    return None
                approved, reason = self._function_call_pack_approval_status(
                    capability_executor, pack_id
                )
                if not approved:
                    result = {
                        "result": "Pack not approved: {}".format(pack_id),
                        "is_error": True,
                        "widget": None,
                        "error_type": "pack_not_approved",
                        "pack_not_approved_reason": reason,
                    }
                    tool_name = _tool_approval_tool_name(tool_def)
                    if tool_name not in {
                        "browser_computer",
                        "browser_use",
                        "computer_use",
                    }:
                        result["widget"] = {
                            "type": "tool_execution_denied",
                            "tool_name": tool_name,
                            "reason": "Pack not approved: {}".format(pack_id),
                        }
                    return result
        context["_tool_server_approved"] = True
        return None

    def _fallback_local_tool_if_first_party_capability_denied(self, tool_def, request, context, response):
        # Capability denials are terminal. Re-entering through a local adapter
        # would bypass the plan, policy snapshot, and exact invocation approval.
        return None

    @staticmethod
    def _first_party_browser_computer_tool_for_function(pack_id, function_id):
        # Pack identity is not an execution capability. Browser/computer
        # authority is resolved from the Tool manifest and Capability Plan.
        del pack_id, function_id
        return None

    @staticmethod
    def _local_tool_fallback_for_capability_response(response, request):
        # Registry failures must remain failures instead of silently selecting
        # an implementation outside the immutable capability snapshot.
        return None

    @staticmethod
    def _fallback_function_call_if_first_party_unapproved(tool_def, request, context, response):
        # A rejected or unavailable function call cannot be retried through a
        # second execution path. The caller must create a new Capability Plan.
        return None

    @staticmethod
    def _first_party_local_tool_for_function(pack_id, function_id):
        if pack_id == "defaultspack":
            return {
                "tool_calculator": "calculator",
                "tool_web_search": "web_search",
                "tool_reddit_search": "reddit_search",
                "tool_subagent": "subagent",
                "tool_todo": "todo",
            }.get(function_id)
        return None

    @staticmethod
    def _eager_first_party_local_tool_for_function(pack_id, function_id):
        # Subagent runs a nested chat turn, so keep it in-process instead of the
        # generic function subprocess timeout envelope.
        return {
            ("defaultspack", "tool_subagent"): "subagent",
            ("rumi_default_tools_pack", "subagent"): "subagent",
        }.get((pack_id, function_id))

    @staticmethod
    def _allows_direct_first_party_function_fallback(pack_id, function_id):
        return (pack_id, function_id) in {
            ("defaultspack", "tool_calculator"),
            ("defaultspack", "coding_file_create"),
            ("defaultspack", "coding_file_write"),
            ("defaultspack", "knowledge_create"),
            ("defaultspack", "knowledge_get"),
            ("defaultspack", "knowledge_list"),
            ("defaultspack", "knowledge_search"),
            ("defaultspack", "knowledge_update"),
            ("rumi_default_tools_pack", "calculator"),
            ("rumi_default_tools_pack", "rumi_api"),
        }

    @staticmethod
    def _tool_response_from_pack_function_output(output):
        if isinstance(output, dict) and output.get("status") in {"ok", "error"}:
            if output.get("status") == "error":
                error_payload = output.get("error")
                if isinstance(error_payload, dict):
                    message = error_payload.get("message") or error_payload.get("code")
                else:
                    message = error_payload
                return {
                    "result": str(message or "Pack function failed"),
                    "is_error": True,
                    "widget": None,
                }
            output = output.get("data")
        if isinstance(output, dict):
            if "result" in output or "is_error" in output or "widget" in output:
                return {
                    "result": output.get("result", output.get("summary", "")),
                    "is_error": bool(output.get("is_error", False)),
                    "widget": output.get("widget"),
                }
            return {
                "result": json_dumps(output),
                "is_error": False,
                "widget": output,
            }
        return {"result": "" if output is None else str(output), "is_error": False, "widget": None}

    @staticmethod
    def _capability_executor(context):
        if isinstance(context, dict):
            for key in ("capability_executor", "_capability_executor"):
                executor = context.get(key)
                if executor is not None and callable(getattr(executor, "execute", None)):
                    return executor
        try:
            from core_runtime.di_container import get_container

            executor = get_container().get_or_none("capability_executor")
            if executor is not None and callable(getattr(executor, "execute", None)):
                if not getattr(executor, "_initialized", False) and callable(getattr(executor, "initialize", None)):
                    executor.initialize()
                return executor
        except Exception:
            pass
        raise RuntimeError(
            "CapabilityExecutor is not bound; implicit executor creation is forbidden"
        )

    @staticmethod
    def _principal_id(tool_def, context):
        if isinstance(context, dict):
            for key in ("principal_id", "pack_id", "_source_pack_id"):
                value = context.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        source_pack_id = tool_def.get("source_pack_id") or tool_def.get("metadata", {}).get("source_pack_id")
        if isinstance(source_pack_id, str) and source_pack_id.strip():
            return source_pack_id.strip()
        return "defaultspack"

    @staticmethod
    def _tool_response_from_capability(response, tool_def=None, arguments=None, context=None):
        success = bool(getattr(response, "success", False))
        output = getattr(response, "output", None)
        error = getattr(response, "error", None)
        if not success:
            if isinstance(output, dict) and output.get("approval_required"):
                summary = output.get("display_summary") or output.get("message") or output.get("reason") or "Authority approval required"
                return {
                    "result": str(summary),
                    "is_error": True,
                    "widget": dict(output),
                    "approval_required": True,
                    "authority": True,
                    **dict(output),
                }
            if getattr(response, "error_type", None) == "pack_not_approved":
                tool_name = _tool_approval_tool_name(tool_def) if isinstance(tool_def, dict) else ""
                if (
                    isinstance(tool_def, dict)
                    and _requires_approval(tool_def)
                    and not _context_has_tool_server_approval(context)
                    and not (
                        tool_name in {"browser_computer", "browser_use", "computer_use"}
                        and isinstance(context, dict)
                        and context.get("user_requested_computer_use")
                    )
                ):
                    return _approval_required_tool_response_for_context(tool_def, arguments or {}, context)
                result = {
                    "result": str(error or "Pack not approved"),
                    "is_error": True,
                    "widget": None,
                }
                if tool_name not in {"browser_computer", "browser_use", "computer_use"}:
                    result["widget"] = {
                        "type": "tool_execution_denied",
                        "tool_name": tool_name or "tool",
                        "reason": str(error or "Pack not approved"),
                    }
                return result
            if (
                getattr(response, "error_type", None) in {"caller_requires_denied", "requires_denied"}
                and isinstance(tool_def, dict)
                and _requires_approval(tool_def)
            ):
                if _context_has_tool_server_approval(context):
                    return {
                        "result": str(error or "Capability execution denied after server approval"),
                        "is_error": True,
                        "widget": {
                            "type": "tool_execution_denied",
                            "tool_name": _tool_approval_tool_name(tool_def),
                            "reason": str(error or "capability execution denied"),
                        },
                    }
                return _approval_required_tool_response_for_context(tool_def, arguments or {}, context)
            return {
                "result": str(error or "Capability execution failed"),
                "is_error": True,
                "widget": None,
            }
        if isinstance(output, dict) and output.get("status") in {"ok", "error"}:
            return ToolExecutor._tool_response_from_pack_function_output(output)
        if isinstance(output, dict):
            if "result" in output or "is_error" in output or "widget" in output:
                return {
                    "result": output.get("result", output.get("summary", "")),
                    "is_error": bool(output.get("is_error", False)),
                    "widget": output.get("widget"),
                }
            return {
                "result": json_dumps(output),
                "is_error": False,
                "widget": output,
            }
        return {
            "result": "" if output is None else str(output),
            "is_error": False,
            "widget": None,
        }

    def _execute_dynamic(self, tool_def, arguments, context):
        """Permanently reject retired in-process Python Tool definitions."""

        del arguments, context
        return {
            "result": (
                "Dynamic Python Tool '{}' is retired and cannot execute. "
                "Migrate it to a reviewed pack, MCP server, or connector."
            ).format(tool_def.get("name", tool_def.get("tool_id", "unknown"))),
            "is_error": True,
            "widget": {
                "type": "tool_migration_required",
                "migration_required": True,
                "supported_targets": [
                    "reviewed_pack",
                    "mcp_server",
                    "connector",
                ],
            },
            "code": "MIGRATION_REQUIRED",
        }

    def _execute_handler(self, tool_def, arguments, context):
        import importlib
        import json

        execution = tool_def.get("execution", {})
        handler = str(execution.get("handler") or "")
        if ":" not in handler:
            return {
                "result": "Tool handler '{}' must use module:callable format".format(handler),
                "is_error": True,
                "widget": None,
            }

        policy = policy_from_context(context if isinstance(context, dict) else {})
        next_arguments = dict(arguments or {})
        next_context, approval_error = _context_with_tool_approval_token(context, tool_def, next_arguments)
        if approval_error is not None:
            return approval_error

        def finish_handler_result(result):
            return result

        if _truthy(policy.get("yolo_mode")):
            mark_tool_server_approval_context(next_context)
        elif _is_policy_allow_context(context):
            mark_tool_server_approval_context(next_context)
        elif _context_has_tool_server_approval(next_context):
            pass
        elif _requires_approval(tool_def):
            return _approval_required_tool_response_for_context(tool_def, next_arguments, next_context)

        consume_error = self._consume_deferred_tool_approval(next_context)
        if consume_error is not None:
            return consume_error
        module_name, attr_name = handler.split(":", 1)
        try:
            module = importlib.import_module(module_name)
            callable_obj = getattr(module, attr_name)
            result = callable_obj(next_arguments, next_context)
        except Exception as exc:
            return {
                "result": "Tool handler execution failed: {}".format(exc),
                "is_error": True,
                "widget": None,
            }

        if not isinstance(result, dict):
            return finish_handler_result({"result": str(result), "is_error": False, "widget": None})

        if "result" in result or "is_error" in result or "widget" in result:
            return finish_handler_result({
                "result": result.get("result", ""),
                "is_error": bool(result.get("is_error", False)),
                "widget": result.get("widget"),
            })

        if result.get("status") == "error":
            error_info = result.get("error", {})
            return {
                "result": str(error_info.get("message") if isinstance(error_info, dict) else error_info),
                "is_error": True,
                "widget": result,
            }

        data = result.get("data", result)
        result_text = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
        is_approval = isinstance(data, dict) and bool(data.get("approval_required"))
        return finish_handler_result({
            "result": result_text,
            "is_error": False,
            "widget": {"type": "approval_request", **data} if is_approval else result,
        })

    def _execute_local_with_tool_def(self, tool_name, arguments, context, tool_def):
        had_previous = hasattr(self, "_current_local_tool_def")
        previous = getattr(self, "_current_local_tool_def", None)
        self._current_local_tool_def = tool_def
        try:
            consume_error = self._consume_deferred_tool_approval(context)
            if consume_error is not None:
                return consume_error
            result = self._execute_local(tool_name, arguments, context)
            return result
        finally:
            if had_previous:
                self._current_local_tool_def = previous
            else:
                delattr(self, "_current_local_tool_def")

    def _execute_local(self, tool_name, arguments, context, tool_def=None):
        """
        ローカルツール実行（最小動作版: 固定レスポンスを返す）
        """
        if _is_cancelled(context):
            return _cancelled_tool_result(tool_name)
        explicit_tool_def = tool_def if isinstance(tool_def, dict) else getattr(self, "_current_local_tool_def", None)
        if not isinstance(tool_def, dict):
            registry = getattr(self, "_registry", None)
            tool_def = registry.get(tool_name) if registry is not None else {}
            tool_def = tool_def or {}
        adaptive_decision = guard_tool_execution(
            tool_name,
            arguments if isinstance(arguments, dict) else {},
            context if isinstance(context, dict) else {},
            tool_def=explicit_tool_def if isinstance(explicit_tool_def, dict) else tool_def,
        )
        if adaptive_decision is not None:
            return tool_guard_response(adaptive_decision, tool_name)
        if tool_name == "web_search":
            from domain.research.providers import ExternalWebProvider, compact_provider_result

            query = arguments.get("query", "")
            result = ExternalWebProvider().search(
                query,
                limit=int(arguments.get("limit", 5)),
                allow_network=bool(arguments.get("allow_network", True)),
                domains=arguments.get("domains"),
                official_only=bool(arguments.get("official_only", False)),
                fetch_pages=bool(arguments.get("fetch_pages", False)),
            )
            result = compact_provider_result(
                result,
                max_chars=arguments.get("max_chars") or arguments.get("max_output_chars"),
                max_tokens=arguments.get("max_tokens") or arguments.get("max_output_tokens"),
            )
            return {
                "result": result.summary,
                "is_error": False,
                "widget": {"type": "research_sources", **result.as_dict()}
            }
        elif tool_name == "reddit_search":
            from domain.research.providers import RedditProvider

            query = arguments.get("query", "")
            result = RedditProvider().search(
                query,
                subreddit=arguments.get("subreddit"),
                sort=arguments.get("sort", "relevance"),
                limit=int(arguments.get("limit", 10)),
                allow_network=bool(arguments.get("allow_network", True)),
            )
            return {
                "result": result.summary,
                "is_error": False,
                "widget": {"type": "research_sources", **result.as_dict()}
            }
        elif tool_name in {"browser_computer", "browser_use", "computer_use"}:
            from domain.host_bridge.computer_router import run_computer_action

            next_arguments = dict(arguments or {})
            current_tool_def = explicit_tool_def
            effective_tool_def = tool_def if isinstance(tool_def, dict) else current_tool_def
            approval_tool_def = effective_tool_def if isinstance(effective_tool_def, dict) else {
                "tool_id": tool_name,
                "name": tool_name,
                "requires_approval": True,
            }
            action, payload = _browser_computer_action_payload(tool_name, next_arguments)
            next_context, approval_error = _context_with_tool_approval_token(
                context,
                approval_tool_def,
                next_arguments,
                action,
                str(next_arguments.get("action") or "").strip(),
            )
            if approval_error is not None:
                return approval_error
            token = _approval_token_from_arguments(next_arguments) or _approval_token_from_context(
                next_context,
                approval_tool_def,
                next_arguments,
                action,
                str(next_arguments.get("action") or "").strip(),
            )
            if token and "approval_token" not in payload:
                payload["approval_token"] = token
            policy = policy_from_context(next_context if isinstance(next_context, dict) else {})
            if _is_cancelled(context):
                return _cancelled_tool_result(tool_name, action=action)
            user_requested = bool(isinstance(next_context, dict) and next_context.get("user_requested_computer_use"))
            if user_requested and action == "browser.open_url" and not any(
                key in payload for key in ("persistent", "profile_id", "session_id")
            ):
                payload["persistent"] = False
            payload = _computer_use_payload_with_context_defaults(action, payload, next_context)
            router_kwargs = {
                "tool_name": tool_name,
                "artifact_root": _conversation_tool_artifact_root(next_context),
                "yolo_mode": _truthy(policy.get("yolo_mode")) or _context_has_tool_server_approval(next_context),
            }
            if (
                isinstance(current_tool_def, dict)
                and "tool_arguments" in inspect.signature(run_computer_action).parameters
            ):
                router_kwargs["tool_arguments"] = next_arguments
            result = run_computer_action(
                action,
                payload,
                next_context if isinstance(next_context, dict) else None,
                **router_kwargs,
            )
            if _is_cancelled(context):
                return _cancelled_tool_result(tool_name, action=action)
            is_error = bool(result.get("is_error"))
            summary = "{} {} {}".format(
                tool_name,
                result.get("action", "action"),
                "failed" if is_error else "completed",
            )
            if is_error and result.get("reason"):
                summary += ": {}".format(result.get("reason"))
            if result.get("path"):
                summary += "; artifact: {}".format(result.get("path"))
            output = {
                "result": summary,
                "is_error": is_error,
                "widget": {"type": tool_name, **result},
            }
            if isinstance(result.get("recovery"), dict):
                output["recovery"] = result.get("recovery")
            return output
        elif tool_name in {"task_board", "tool_task_board"}:
            from domain.tool.task_board import TaskBoardController

            result = TaskBoardController().run(arguments, context if isinstance(context, dict) else {})
            return {
                "result": result.get("summary", "task board updated"),
                "is_error": False,
                "widget": {"type": "task_board", **result},
            }
        elif tool_name == "tool_task_board_agent_session":
            from domain.tool.task_board_agent_session import TaskBoardAgentSessionController

            result = TaskBoardAgentSessionController().run(arguments, context if isinstance(context, dict) else {})
            return {
                "result": result.get("summary", "task board agent session updated"),
                "is_error": False,
                "widget": {"type": "task_board_agent_session", **result},
            }
        elif tool_name == "calculator":
            expression = arguments.get("expression", "")
            calculation = _safe_calculate(expression)
            if calculation["is_error"]:
                return {
                    "result": calculation["error"],
                    "is_error": True,
                    "widget": None,
                }
            return {
                "result": "Calculated: {} = {}".format(expression, calculation["result"]),
                "is_error": False,
                "widget": None
            }
        elif tool_name == "subagent":
            if self._subagent_factory is None:
                return {
                    "result": "Subagent runner is not configured",
                    "is_error": True,
                    "widget": {
                        "type": "subagent",
                        "error_type": "subagent_runner_unavailable",
                    },
                }
            delegated = self._subagent_factory().run(
                arguments if isinstance(arguments, dict) else {},
                context if isinstance(context, dict) else {},
            )
            delegated = dict(delegated)
            failed = bool(
                delegated.get("is_error")
                or delegated.get("status") == "error"
            )
            return {
                "result": str(
                    delegated.get("summary")
                    or ("Subagent failed" if failed else "Subagent completed")
                ),
                "is_error": failed,
                "widget": {"type": "subagent", **delegated},
            }
        elif tool_name == "file_reader":
            from blocks.coding.file_read import run as file_read_run

            path = arguments.get("path", "")
            call_context = context if isinstance(context, dict) else {}
            workspace_id = str(call_context.get("workspace_id") or "").strip()
            result = file_read_run(
                {
                    "path": path,
                    "workspace_id": workspace_id,
                    "start_line": arguments.get("start_line"),
                    "end_line": arguments.get("end_line"),
                    "max_chars": arguments.get("max_chars") or arguments.get("max_output_chars"),
                    "max_tokens": arguments.get("max_tokens") or arguments.get("max_output_tokens"),
                },
                call_context,
            )
            if result.get("status") != "ok":
                err = result.get("error", {})
                message = (
                    err.get("message", "file read failed")
                    if isinstance(err, dict)
                    else str(err)
                )
                return {
                    "result": message,
                    "is_error": True,
                    "widget": {"type": "file_reader", "path": path, "error": err},
                }
            data = result.get("data", {})
            return {
                "result": data.get("content", ""),
                "is_error": False,
                "widget": {"type": "file_reader", **data},
            }
        else:
            return {
                "result": "Tool '{}' is not implemented".format(tool_name),
                "is_error": True,
                "widget": {
                    "type": tool_name,
                    "error": "not_implemented",
                    "arguments": arguments,
                },
            }


def _is_cancelled(context):
    checker = context.get("is_cancelled") if isinstance(context, dict) else None
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return False


def _cancelled_tool_result(tool_name, *, action=""):
    detail = " during {}".format(action) if action else ""
    return {
        "result": "Tool '{}' cancelled{}".format(tool_name, detail),
        "is_error": True,
        "widget": {
            "type": tool_name,
            "action": action or "cancelled",
            "is_error": True,
            "cancelled": True,
            "reason": "Tool execution cancelled by user.",
        },
        "cancelled": True,
    }


def _truthy(value):
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "on"}
    return bool(value)


def _filtered_tool_rejection(tool_name, context):
    if not isinstance(context, dict):
        return None
    entries = context.get("tool_filter_result")
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if str(entry.get("tool_name") or "") != str(tool_name or ""):
                continue
            if str(entry.get("status") or "") in {"blocked", "hidden"}:
                return rejection_result(str(tool_name or ""), entry)
    capability_graph = mapping_or_empty(context.get("capability_graph"))
    connected = list_or_empty(capability_graph.get("connected_tools"))
    if connected and str(tool_name or "") not in {str(item) for item in connected if str(item or "").strip()}:
        return rejection_result(
            str(tool_name or ""),
            {
                "reason_code": "not_connected_to_profile",
                "reason": "tool is not connected to the active runtime profile",
                "required": {"runtime_capabilities": ["runtime.connected_tools"]},
                "actual": {"runtime_capabilities": list(connected)},
                "repair_suggestions": ["Connect the tool in the active runtime profile."],
            },
        )
    return None


def _safe_calculate(expression):
    import ast
    import operator

    operators: dict[type[ast.AST], Callable[..., Any]] = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    def evaluate(node):
        if isinstance(node, ast.Expression):
            return evaluate(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            left = evaluate(node.left)
            right = evaluate(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 100:
                raise ValueError("Exponent is too large")
            return operators[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](evaluate(node.operand))
        raise ValueError("Unsupported calculator expression")

    try:
        parsed = ast.parse(str(expression or ""), mode="eval")
        result = evaluate(parsed)
    except Exception as exc:
        return {"is_error": True, "error": "Calculator error: {}".format(exc)}
    return {"is_error": False, "result": result}


_URL_IN_TEXT_RE = re.compile(r"(?:https?://|file://|www\.)[^\s\"'<>]+")


def _browser_open_url_candidates(value):
    if not isinstance(value, str):
        return []
    text = value.strip()
    if not text:
        return []
    urls = []
    seen = set()
    for match in _URL_IN_TEXT_RE.finditer(text):
        url = match.group(0).rstrip(".,;)")
        if url.startswith("www."):
            url = "https://" + url
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _browser_open_url_from_value(value):
    candidates = _browser_open_url_candidates(value)
    return candidates[0] if candidates else ""


def _single_browser_open_url_from_context(context):
    if not isinstance(context, dict):
        return ""
    urls = []
    seen = set()
    for key in ("user_text", "conversation_user_text"):
        for url in _browser_open_url_candidates(context.get(key)):
            if url not in seen:
                seen.add(url)
                urls.append(url)
    return urls[0] if len(urls) == 1 else ""


def _canonical_browser_computer_action(raw_action, action_map):
    action = str(raw_action or "").strip()
    if action in action_map:
        return action_map[action]
    first_token = action.split(maxsplit=1)[0] if action else ""
    if first_token in action_map:
        return action_map[first_token]
    return action


def _normalize_browser_open_url_payload(action, payload, *url_candidates):
    payload = dict(payload or {})
    if action != "browser.open_url" or payload.get("url"):
        return payload
    for key in ("value", "text", "target", "href", "link", "url_contains", "title", "title_contains"):
        candidate = _browser_open_url_from_value(str(payload.get(key) or ""))
        if candidate:
            payload["url"] = candidate
            return payload
    for value in url_candidates:
        candidate = _browser_open_url_from_value(value)
        if candidate:
            payload["url"] = candidate
            return payload
    return payload


def _browser_computer_action_payload(tool_name, arguments):
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool_name == "browser_computer":
        action = str(arguments.get("action", "browser.session"))
        return action, _normalize_browser_open_url_payload(
            action,
            dict(arguments.get("payload") or {}),
            str(arguments.get("action") or ""),
        )

    raw_payload = dict(arguments.get("payload") or {})
    raw_action = str(arguments.get("action") or "").strip()
    if tool_name == "browser_use":
        action_map = {
            "": "browser.session",
            "session": "browser.session",
            "open_url": "browser.open_url",
            "browser_open_url": "browser.open_url",
            "open": "browser.open_url",
            "context/apps/windows": "computer.context",
            "context_apps_windows": "computer.context",
            "context": "computer.context",
            "app_context": "computer.context",
            "state": "computer.context",
            "screenshot": "computer.screenshot",
            "move": "computer.move",
            "cursor_move": "computer.move",
            "mouse_move": "computer.move",
            "click": "computer.click",
            "drag": "computer.drag",
            "mouse_drag": "computer.drag",
            "type": "computer.type",
            "key": "computer.key",
            "backspace": "computer.backspace",
            "delete_back": "computer.backspace",
            "scroll": "computer.scroll",
            "apps": "computer.apps",
            "applications": "computer.apps",
            "open_apps": "computer.apps",
            "list_apps": "computer.apps",
            "select_app": "computer.select_app",
            "app": "computer.select_app",
            "show_app": "computer.show_app",
            "focus_app": "computer.show_app",
            "activate_app": "computer.show_app",
            "main_app": "computer.show_app",
            "show": "computer.show_app",
            "select_window": "computer.select_window",
            "window": "computer.select_window",
            "windows": "computer.windows",
            "list_windows": "computer.windows",
        }
        action = _canonical_browser_computer_action(raw_action, action_map)
        for key in (
            "url",
            "url_contains",
            "browser",
            "browser_app",
            "profile_id",
            "session_id",
            "persistent",
            "target_app",
            "x",
            "y",
            "point",
            "points",
            "point_order",
            "coordinate",
            "coordinates",
            "normalized_point",
            "normalized_x",
            "normalized_y",
            "x1",
            "y1",
            "x2",
            "y2",
            "from_x",
            "from_y",
            "to_x",
            "to_y",
            "text",
            "value",
            "key",
            "modifier",
            "modifiers",
            "amount",
            "target",
            "scope",
            "app",
            "application",
            "name",
            "title",
            "title_contains",
            "window",
            "window_index",
            "tab_index",
            "button",
            "include_screenshot",
            "coordinate_space",
            "physical",
            "virtual_only",
            "crop",
            "zoom",
            "crop_box",
            "zoom_box",
            "source",
            "detail",
            "crop_x",
            "crop_y",
            "crop_width",
            "crop_height",
            "normalized_box",
            "normalized_width",
            "normalized_height",
            "normalized_right",
            "normalized_bottom",
            "box_format",
            "width_height",
            "focus",
            "open",
            "launch",
            "limit",
            "include_installed",
            "include_installed_apps",
            "mode",
            "method",
            "driver",
            "background",
            "foreground",
            "fallback",
        ):
            if key in arguments:
                raw_payload[key] = arguments.get(key)
    else:
        action_map = {
            "": "computer.screenshot",
            "open_url": "browser.open_url",
            "browser_open_url": "browser.open_url",
            "open": "browser.open_url",
            "context/apps/windows": "computer.context",
            "context_apps_windows": "computer.context",
            "context": "computer.context",
            "app_context": "computer.context",
            "state": "computer.context",
            "screenshot": "computer.screenshot",
            "move": "computer.move",
            "cursor_move": "computer.move",
            "mouse_move": "computer.move",
            "click": "computer.click",
            "drag": "computer.drag",
            "mouse_drag": "computer.drag",
            "type": "computer.type",
            "key": "computer.key",
            "backspace": "computer.backspace",
            "delete_back": "computer.backspace",
            "scroll": "computer.scroll",
            "apps": "computer.apps",
            "applications": "computer.apps",
            "open_apps": "computer.apps",
            "list_apps": "computer.apps",
            "select_app": "computer.select_app",
            "app": "computer.select_app",
            "show_app": "computer.show_app",
            "focus_app": "computer.show_app",
            "activate_app": "computer.show_app",
            "main_app": "computer.show_app",
            "show": "computer.show_app",
            "select_window": "computer.select_window",
            "window": "computer.select_window",
            "windows": "computer.windows",
            "list_windows": "computer.windows",
        }
        action = _canonical_browser_computer_action(raw_action, action_map)
        for key in (
            "url",
            "url_contains",
            "browser",
            "browser_app",
            "profile_id",
            "session_id",
            "persistent",
            "target_app",
            "x",
            "y",
            "point",
            "points",
            "point_order",
            "coordinate",
            "coordinates",
            "normalized_point",
            "normalized_x",
            "normalized_y",
            "x1",
            "y1",
            "x2",
            "y2",
            "from_x",
            "from_y",
            "to_x",
            "to_y",
            "text",
            "value",
            "key",
            "modifier",
            "modifiers",
            "amount",
            "target",
            "scope",
            "app",
            "application",
            "name",
            "title",
            "title_contains",
            "window",
            "window_index",
            "tab_index",
            "button",
            "include_screenshot",
            "coordinate_space",
            "physical",
            "virtual_only",
            "crop",
            "zoom",
            "crop_box",
            "zoom_box",
            "source",
            "detail",
            "crop_x",
            "crop_y",
            "crop_width",
            "crop_height",
            "normalized_box",
            "normalized_width",
            "normalized_height",
            "normalized_right",
            "normalized_bottom",
            "box_format",
            "width_height",
            "focus",
            "open",
            "launch",
            "limit",
            "include_installed",
            "include_installed_apps",
            "mode",
            "method",
            "driver",
            "background",
            "foreground",
            "fallback",
        ):
            if key in arguments:
                raw_payload[key] = arguments.get(key)
    if "dry_run" in arguments:
        raw_payload["dry_run"] = arguments.get("dry_run")
    if "approval_token" in arguments:
        raw_payload["approval_token"] = arguments.get("approval_token")
    raw_payload = _drop_redundant_background_flag(raw_payload)
    raw_payload = _normalize_browser_open_url_payload(action, raw_payload, raw_action)
    return action, raw_payload


_BROWSER_APP_ALIAS_KEYS = ("app", "application", "name", "browser", "browser_app", "target_app")
_BROWSER_APP_ALIASES = {
    "atlas",
    "chatgpt",
    "chatgptatlas",
    "chrome",
    "firefox",
    "googlechrome",
    "msedge",
    "safari",
    "vivaldi",
}
_COMPUTER_USE_FOREGROUND_APP_ALIASES = {
    "atlas",
    "chatgptatlas",
}
_COMPUTER_USE_FOREGROUND_DEFAULT_ACTIONS = {"computer.type", "computer.key", "computer.scroll"}


def _normalized_app_alias(value):
    return re.sub(r"[^a-z0-9]+", "", str(value or "").strip().casefold())


def _is_browser_app_alias(value):
    return _normalized_app_alias(value) in _BROWSER_APP_ALIASES


def _computer_use_debug_foreground_enabled():
    value = str(os.environ.get("RUMI_COMPUTER_USE_DEBUG_FOREGROUND") or "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _computer_use_prefers_foreground(action, payload, context):
    if action not in _COMPUTER_USE_FOREGROUND_DEFAULT_ACTIONS:
        return False
    if not isinstance(context, dict) or not _truthy(context.get("user_requested_computer_use")):
        return False
    if payload.get("physical") is True or payload.get("virtual_only") is True:
        return False
    if _truthy(context.get("computer_use_foreground_preferred")):
        return True
    if _computer_use_debug_foreground_enabled():
        return True
    target_alias = _normalized_app_alias(context.get("computer_use_target_app"))
    return target_alias in _COMPUTER_USE_FOREGROUND_APP_ALIASES


def _payload_with_computer_use_foreground_preference(action, payload, context):
    next_payload = dict(payload or {})
    if not _computer_use_prefers_foreground(action, next_payload, context):
        return next_payload
    next_payload.pop("background", None)
    next_payload.setdefault("fallback", "foreground")
    return next_payload


def _drop_redundant_background_flag(payload):
    payload = dict(payload or {})
    if payload.get("background") is True:
        mode = str(payload.get("mode") or payload.get("method") or payload.get("driver") or "").strip()
        if mode:
            payload.pop("background", None)
    return payload


def _payload_with_target_app_override(payload, target_app):
    target = str(target_app or "").strip()
    if not target:
        return dict(payload or {})
    next_payload = dict(payload or {})
    target_alias = _normalized_app_alias(target)
    existing_values = [
        str(next_payload.get(key) or "").strip()
        for key in _BROWSER_APP_ALIAS_KEYS
        if str(next_payload.get(key) or "").strip()
    ]
    if any(_normalized_app_alias(value) == target_alias for value in existing_values):
        return next_payload
    if not existing_values or any(_is_browser_app_alias(value) for value in existing_values):
        for key in _BROWSER_APP_ALIAS_KEYS:
            if _is_browser_app_alias(next_payload.get(key)):
                next_payload.pop(key, None)
        next_payload["app"] = target
    return next_payload


def _computer_use_payload_with_context_defaults(action, payload, context):
    payload = dict(payload or {})
    if not isinstance(context, dict):
        return payload
    target_app = context.get("computer_use_target_app")
    target_title = context.get("computer_use_target_title")
    physical_clicks = _truthy(context.get("computer_use_physical_clicks"))
    if action == "browser.open_url":
        payload = _payload_with_target_app_override(payload, target_app)
        if not payload.get("url"):
            inferred_url = _single_browser_open_url_from_context(context)
            if inferred_url:
                payload["url"] = inferred_url
        return payload
    if action.startswith("computer.") and action not in {"computer.windows", "computer.apps"}:
        if isinstance(target_app, str) and target_app.strip() and (
            action not in {"computer.select_app", "computer.show_app"}
            or _truthy(context.get("user_requested_computer_use"))
        ):
            payload = _payload_with_target_app_override(payload, target_app)
        if (
            isinstance(target_title, str)
            and target_title.strip()
            and action not in {"computer.select_app", "computer.show_app"}
        ):
            payload.setdefault("title", target_title.strip())
        if physical_clicks and action == "computer.click" and "physical" not in payload:
            payload["physical"] = True
        payload = _payload_with_computer_use_foreground_preference(action, payload, context)
        if _should_default_computer_use_background(action, payload, context):
            payload["background"] = True
    return payload


def _should_default_computer_use_background(action, payload, context):
    if not _truthy(context.get("user_requested_computer_use")):
        return False
    if action not in {"computer.type", "computer.key", "computer.scroll", "computer.click"}:
        return False
    if payload.get("background") is not None:
        return False
    if payload.get("foreground") is not None:
        return False
    if payload.get("fallback") is not None:
        return False
    if payload.get("physical") is True or payload.get("virtual_only") is True:
        return False
    mode = str(payload.get("mode") or payload.get("method") or payload.get("driver") or "").strip()
    return not mode


def _controller_browser_open_url_approval_payload(payload):
    payload = dict(payload or {})
    url = str(payload.get("url") or "").strip()
    if not url:
        return payload
    profile_id = _browser_profile_id(
        payload.get("profile_id")
        or payload.get("session_id")
        or _active_browser_computer_profile_id()
    )
    target_app = _browser_app_name_from_payload(payload)
    return {
        "url": url,
        "profile_id": profile_id,
        "persistent": payload.get("persistent", True) is not False,
        "target_app": target_app,
    }


def _browser_computer_controller_approval_payloads(action, payload, context=None):
    payload = dict(payload or {})
    if action != "browser.open_url":
        return [payload]
    payloads = []
    for candidate in (
        payload,
        _computer_use_payload_with_context_defaults(action, payload, context),
    ):
        controller_payload = _controller_browser_open_url_approval_payload(candidate)
        if controller_payload not in payloads:
            payloads.append(controller_payload)
    return payloads or [payload]


def _browser_profile_id(value):
    raw = str(value or "default").strip().lower()
    cleaned = re.sub(r"[^a-z0-9._-]+", "-", raw).strip(".-_")
    return (cleaned or "default")[:64]


def _active_browser_computer_profile_id():
    try:
        sessions_path = (
            Path(__file__).resolve().parents[3]
            / "rumi_default_tools_pack"
            / "user_data"
            / "shared"
            / "browser_sessions.json"
        )
        sessions = json.loads(sessions_path.read_text(encoding="utf-8"))
        if isinstance(sessions, dict):
            return _browser_profile_id(sessions.get("active_profile_id") or "default")
    except Exception:
        pass
    return "default"


def _browser_app_name_from_payload(payload):
    payload = payload if isinstance(payload, dict) else {}
    return str(
        payload.get("app")
        or payload.get("application")
        or payload.get("target_app")
        or payload.get("browser")
        or payload.get("browser_app")
        or ""
    ).strip()


def _conversation_tool_artifact_root(context):
    if not isinstance(context, dict):
        return None
    workspace = context.get("conversation_workspace_dir")
    if not isinstance(workspace, str) or not workspace:
        return None
    return Path(workspace) / "tools" / "computer"


def _conversation_browser_companion_artifact_root(context):
    if not isinstance(context, dict):
        return None
    workspace = context.get("conversation_workspace_dir")
    if not isinstance(workspace, str) or not workspace:
        return None
    return Path(workspace) / "tools" / "browser_companion"


def _tool_value(tool_def, key):
    if key in tool_def:
        return tool_def.get(key)
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict) and key in metadata:
        return metadata.get(key)
    execution = tool_def.get("execution")
    if isinstance(execution, dict):
        return execution.get(key)
    return None


def _requires_approval(tool_def):
    return requires_approval_for_security(tool_def)


def _requires_rumi_api_request_approval(tool_def, arguments):
    if not isinstance(tool_def, dict) or not _requires_approval(tool_def):
        return False
    if _tool_approval_tool_name(tool_def) != "rumi_api":
        return False
    execution = tool_def.get("execution")
    if not isinstance(execution, dict):
        return False
    if str(execution.get("qualified_name") or "").strip() != "rumi_default_tools_pack:rumi_api":
        return False
    if not isinstance(arguments, dict):
        return False
    return str(arguments.get("action") or "list_routes").strip() == "request"


def _tool_has_autonomous_internal_approval(tool_def, arguments, context):
    if not isinstance(tool_def, dict):
        return False
    return autonomous_tool_execution_allowed(
        _tool_approval_tool_name(tool_def),
        arguments if isinstance(arguments, dict) else {},
        context if isinstance(context, dict) else {},
    )


def _tool_approval_tool_name(tool_def):
    return str(tool_def.get("name") or tool_def.get("tool_id") or "tool").strip() or "tool"


def _tool_approval_operation(tool_def):
    return "tool.{}".format(_tool_approval_tool_name(tool_def))


def _tool_approval_scope(tool_def, arguments):
    tool_name = _tool_approval_tool_name(tool_def)
    if tool_name in {"browser_computer", "browser_use", "computer_use"} and isinstance(arguments, dict):
        action, payload = _browser_computer_action_payload(tool_name, arguments)
        if str(action or "").startswith(("browser.", "computer.")):
            return str(action), _approval_hash_arguments(
                _browser_computer_request_arguments(tool_name, action, payload)
            )
    if tool_name == "browser_companion" and isinstance(arguments, dict):
        action = str(arguments.get("action") or "session").strip() or "session"
        if action.startswith("page.") or action in {
            "navigate",
            "snapshot",
            "capture",
            "extract",
            "click",
            "type",
            "press",
            "scroll",
        }:
            normalized = {
                "navigate": "page.navigate",
                "snapshot": "page.snapshot",
                "capture": "page.capture",
                "extract": "page.extract",
                "click": "page.click",
                "type": "page.type",
                "press": "page.press",
                "scroll": "page.scroll",
            }.get(action, action)
            return normalized, _approval_hash_arguments(_approval_replayable_arguments(arguments))
    return _tool_approval_operation(tool_def), _approval_replayable_arguments(arguments)


def _tool_approval_display_arguments(tool_def, arguments, approval_args):
    tool_name = _tool_approval_tool_name(tool_def)
    if (
        tool_name in {"browser_computer", "browser_use", "computer_use", "browser_companion"}
        and isinstance(arguments, dict)
    ):
        return dict(arguments)
    return approval_args


def _tool_approval_display_payload(tool_def, arguments, approval_args):
    tool_name = _tool_approval_tool_name(tool_def)
    if tool_name in {"browser_computer", "browser_use", "computer_use"} and isinstance(arguments, dict):
        _, payload = _browser_computer_action_payload(tool_name, arguments)
        return dict(payload)
    if tool_name == "browser_companion" and isinstance(arguments, dict):
        return {key: value for key, value in dict(arguments).items() if key != "action"}
    return approval_args


def _approval_hash_arguments(arguments):
    if not isinstance(arguments, dict):
        return {}
    def sanitize(value):
        if isinstance(value, dict):
            return {
                key: sanitize(item)
                for key, item in value.items()
                if key not in {
                    "approval_token",
                    "approved",
                    "computer_use_haze_sequence_id",
                    "computer_use_sequence_id",
                }
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(dict(arguments))


def _tool_handles_deferred_approval_consumption(tool_def):
    return _tool_approval_tool_name(tool_def) in {"browser_computer", "browser_use", "computer_use"}


def _browser_computer_request_arguments(tool_name, action, payload):
    return {
        "action": str(action or "browser.session"),
        "payload": dict(payload or {}),
    }


def _browser_computer_payload_only_request_arguments(action, payload):
    return dict(payload or {})


def _browser_computer_legacy_request_arguments(tool_name, action, payload):
    if tool_name == "browser_computer":
        return _browser_computer_request_arguments(tool_name, action, payload)
    return {
        "action": str(action or ""),
        **dict(payload or {}),
    }


def _browser_computer_controller_request_arguments(action, payload):
    return {
        "action": str(action or ""),
        "payload": dict(payload or {}),
    }


def _tool_approval_risk_level(tool_def):
    return "high" if _is_high_risk_approval(tool_def) else "medium"


def _approval_token_from_arguments(arguments):
    if not isinstance(arguments, dict):
        return ""
    return str(arguments.get("approval_token") or "").strip()


def _approval_replayable_arguments(arguments):
    if not isinstance(arguments, dict):
        return {}
    replay_args = dict(arguments)
    for key in ("approval_token", "_headers", "_method", "_raw_body", "_raw_body_base64"):
        replay_args.pop(key, None)
    return replay_args


def _approval_token_from_context(context, tool_def, arguments=None, *extra_keys):
    if not isinstance(context, dict):
        return ""
    tokens = context.get("tool_approval_tokens")
    if not isinstance(tokens, dict):
        return ""
    tool_name = _tool_approval_tool_name(tool_def)
    scoped_operation, _ = _tool_approval_scope(tool_def, arguments if isinstance(arguments, dict) else {})
    extra = [str(key or "").strip() for key in extra_keys if str(key or "").strip()]
    if tool_name in {"browser_computer", "browser_use", "computer_use"}:
        keys = [scoped_operation, *extra]
        has_action_scoped_token = any(
            str(key or "").strip().startswith(("browser.", "computer."))
            for key in tokens
        )
        if not has_action_scoped_token:
            keys.extend([tool_name, _tool_approval_operation(tool_def)])
    else:
        keys = [
            tool_name,
            _tool_approval_operation(tool_def),
            scoped_operation,
            *extra,
        ]
    for key in keys:
        token = str(tokens.get(key) or "").strip()
        if token:
            return token
    return ""


def _preflight_profile_tool_permission(tool_name, tool_def, arguments, context, policy):
    decision = resolve_profile_tool_permission(
        tool_def,
        tool_name,
        arguments if isinstance(arguments, dict) else {},
        policy if isinstance(policy, dict) else {},
    )
    if decision is None:
        return context, None
    _audit_profile_tool_permission(context, decision)
    status = str(decision.get("status") or "")
    if status == "denied":
        return context, _tool_permission_denied_result(tool_def, arguments, decision)
    if status == "dry_run":
        return context, _tool_permission_dry_run_result(tool_def, arguments, decision)
    if status == "approval_required":
        approved_context, approval_error = _context_with_profile_tool_permission_token(
            context,
            tool_def,
            arguments,
            decision,
        )
        if approval_error is not None:
            return approved_context, approval_error
        if (
            isinstance(approved_context, dict)
            and approved_context.get("_tool_permission_policy_approved") is True
        ):
            return approved_context, None
        return context, _approval_required_tool_response_for_context(tool_def, arguments or {}, context)
    if status == "allowed":
        return _context_with_profile_tool_permission_allow(context, tool_def, arguments, decision)
    return context, None


def _trusted_full_access_workspace_binding(
    context,
    workspace_id,
):
    """Create a scoped binding when the optional workspace provider is absent.

    This compatibility path is only available to the authenticated local UI in
    full-access mode. It never accepts a path supplied in tool arguments.
    """

    if not isinstance(context, dict):
        return None
    policy = policy_from_context(context)
    if (
        context.get("_defaultspack_local_ui_authenticated") is not True
        or str(policy.get("action_approval_mode") or "").strip().lower()
        != "full"
        or not _truthy(policy.get("full_access"))
    ):
        return None
    raw_root = str(context.get("workspace_root") or "").strip()
    if not raw_root or not Path(raw_root).is_absolute():
        return None
    try:
        root = Path(raw_root).resolve(strict=True)
        if not root.is_dir():
            return None
        root_stat = root.stat()
    except OSError:
        return None
    binding = {
        "workspace_id": str(workspace_id),
        "access": "read_only",
        "mount_revision": "authenticated-local-ui-full-access",
        "canonical_root": str(root),
        "root_st_dev": int(root_stat.st_dev),
        "root_st_ino": int(root_stat.st_ino),
        "binding_source": "authenticated_local_ui_fallback",
    }
    binding["root_identity"] = hashlib.sha256(
        json.dumps(
            binding,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return binding


def _preflight_delegated_approval(
    tool_name,
    tool_def,
    arguments,
    context,
):
    """Resolve ``agent`` mode through an isolated reviewer, never blanket-yolo."""

    from domain.tool.approval_reviewer import (
        delegated_approval_requested,
        review_tool_action,
    )

    next_context = dict(context or {}) if isinstance(context, dict) else {}
    if not delegated_approval_requested(next_context):
        return next_context, None
    if isinstance(tool_def, dict) and (
        is_safe_first_party_memo_tool(tool_def)
        or is_sandbox_capability_tool(tool_def)
    ):
        return next_context, None
    needs_review = bool(
        _requires_approval(tool_def)
        or _frontend_permission_resolver_failure_requires_approval(
            tool_def,
            tool_name,
        )
    )
    if not needs_review:
        try:
            needs_review = (
                ToolPermissionResolver().resolve(
                    tool_def,
                    context=next_context,
                ).get("permission")
                == "confirm"
            )
        except Exception:
            needs_review = False
    if not needs_review:
        return next_context, None
    review = review_tool_action(
        tool_name,
        tool_def if isinstance(tool_def, dict) else {},
        arguments if isinstance(arguments, dict) else {},
        next_context,
    )
    audit_tool_policy(
        next_context,
        "delegated_approval_review",
        {
            "tool_name": tool_name,
            "decision": review.get("decision"),
            "reason": review.get("reason"),
            "review_id": review.get("review_id"),
            "history_json_path": review.get("history_json_path"),
        },
    )
    decision = str(review.get("decision") or "").strip().lower()
    if decision == "approve":
        sealed = seal_tool_context(
            next_context,
            {
                "allowed": True,
                "action": "allow",
                "source": "approval_reviewer",
                "review_id": review.get("review_id"),
            },
        )
        mark_tool_server_approval_context(sealed)
        sealed["_delegated_approval_review"] = dict(review)
        return sealed, None
    if decision == "deny":
        return next_context, {
            "result": "Delegated reviewer denied tool '{}': {}".format(
                tool_name,
                review.get("reason") or "unsafe action",
            ),
            "is_error": True,
            "widget": {
                "type": "tool_execution_denied",
                "tool_name": tool_name,
                "reason": review.get("reason"),
                "delegated_review": review,
            },
            "error_type": "delegated_approval_denied",
        }
    response = _approval_required_tool_response(
        tool_def,
        arguments or {},
        next_context,
    )
    if isinstance(response.get("widget"), dict):
        response["widget"]["delegated_review"] = review
        response["widget"]["display_summary"] = str(
            review.get("reason")
            or response["widget"].get("display_summary")
            or "Delegated reviewer requested user approval."
        )
    return next_context, response


def _preflight_frontend_tool_permission(tool_name, tool_def, arguments, context, policy):
    if not isinstance(policy, dict):
        policy = {}
    try:
        resolution = ToolPermissionResolver().resolve(tool_def, context=context if isinstance(context, dict) else {})
    except Exception:
        if _frontend_permission_resolver_failure_requires_approval(tool_def, tool_name):
            decision = _frontend_permission_decision(
                tool_name,
                tool_def,
                {
                    "action_class": infer_action_class(tool_def if isinstance(tool_def, dict) else {}),
                    "permission": "confirm",
                    "minimum_permission": "confirm",
                    "service_id": None,
                    "sources": [{"source": "frontend_settings_error", "value": "confirm"}],
                },
                "confirm",
                reason="confirmation required because Settings permission resolution failed",
            )
            _audit_frontend_tool_permission(context, decision, decision.get("resolution"))
            return context, _approval_required_tool_response_for_context(tool_def, arguments or {}, context)
        return context, None
    permission = str(resolution.get("permission") or "auto").strip().lower()
    if permission == "block":
        decision = _frontend_permission_decision(
            tool_name,
            tool_def,
            resolution,
            permission,
            reason="blocked by Settings",
        )
        _audit_frontend_tool_permission(context, decision, resolution)
        return context, _tool_permission_denied_result(tool_def, arguments, decision)
    if isinstance(tool_def, dict) and is_safe_first_party_memo_tool(tool_def):
        return context, None
    if permission == "auto":
        return context, None
    if permission != "confirm":
        return context, None
    if _context_has_tool_server_approval(context):
        return context, None
    if str(policy.get("action_approval_mode") or "").strip().lower() == "full" or _truthy(policy.get("full_access")):
        return context, None
    decision = _frontend_permission_decision(
        tool_name,
        tool_def,
        resolution,
        permission,
        reason="confirmation required by Settings",
    )
    _audit_frontend_tool_permission(context, decision, resolution)
    approved_context, approval_error = _context_with_frontend_tool_permission_token(
        context,
        tool_def,
        arguments,
        decision,
    )
    if approval_error is not None:
        return approved_context, approval_error
    if isinstance(approved_context, dict) and approved_context.get("_frontend_tool_permission_approved") is True:
        return approved_context, None
    return context, _approval_required_tool_response_for_context(tool_def, arguments or {}, context)


def _frontend_permission_decision(tool_name, tool_def, resolution, permission, *, reason):
    permission = str(permission or "auto").strip().lower()
    resolution = resolution if isinstance(resolution, dict) else {}
    decision = {
        "tool_name": tool_name,
        "action": resolution.get("action_class") or infer_action_class(tool_def if isinstance(tool_def, dict) else {}),
        "status": "denied" if permission == "block" else "approval_required",
        "mode": permission,
        "risk": resolution.get("minimum_permission"),
        "risk_level": "high" if permission == "block" else "medium",
        "matched_by": "frontend_settings",
        "matched_value": resolution.get("service_id"),
        "reason": reason,
        "audit_required": True,
        "resolution": dict(resolution),
    }
    return decision


def _frontend_permission_resolver_failure_requires_approval(tool_def, tool_name):
    tool = tool_def if isinstance(tool_def, dict) else {}
    action_class = infer_action_class(tool)
    if action_class in _FRONTEND_PERMISSION_FAIL_CLOSED_ACTIONS:
        return True
    try:
        risk = resolve_tool_risk(tool, tool_name)
    except Exception:
        risk = ""
    return str(risk or "").strip().lower() in _FRONTEND_PERMISSION_FAIL_CLOSED_RISKS


def _context_with_frontend_tool_permission_token(context, tool_def, arguments, decision):
    next_context = dict(context or {}) if isinstance(context, dict) else {}
    if _is_policy_allow_context(next_context):
        next_context["_frontend_tool_permission_approved"] = True
        return next_context, None
    token = _approval_token_from_context(next_context, tool_def, arguments) or _approval_token_from_arguments(arguments)
    if not token:
        return next_context, None
    token_result = _verify_profile_tool_permission_token(next_context, tool_def, arguments, token)
    verification = token_result.get("verification")
    if verification is not None and verification.valid:
        approved_context = _seal_profile_tool_permission_context(next_context, decision, source="frontend_settings_approval")
        approved_context["_frontend_tool_permission_approved"] = True
        return _attach_tool_approval_token(approved_context, tool_def, token_result), None
    code = str(getattr(verification, "code", "") or "")
    if code in _STALE_APPROVAL_TOKEN_CODES:
        response = _approval_required_tool_response_for_context(tool_def, arguments or {}, next_context)
        if isinstance(response.get("widget"), dict):
            response["widget"]["stale_approval_token"] = True
            response["widget"]["stale_approval_code"] = code
        return next_context, response
    return next_context, {
        "result": getattr(verification, "message", None) or "approval token is invalid",
        "is_error": True,
        "widget": None,
    }


def _context_with_profile_tool_permission_allow(context, tool_def, arguments, decision):
    next_context = _seal_profile_tool_permission_context(context, decision, source="policy_allow")
    if not isinstance(tool_def, dict) or not _requires_approval(tool_def):
        return next_context, None
    token_result = _issue_profile_tool_permission_token(next_context, tool_def, arguments, decision)
    if token_result.get("error"):
        return next_context, {
            "result": token_result["error"],
            "is_error": True,
            "widget": None,
        }
    return _attach_tool_approval_token(next_context, tool_def, token_result), None


def _context_with_profile_tool_permission_token(context, tool_def, arguments, decision):
    next_context = dict(context or {}) if isinstance(context, dict) else {}
    if _is_policy_allow_context(next_context):
        next_context["_tool_permission_policy_approved"] = True
        return next_context, None
    token = _approval_token_from_context(next_context, tool_def, arguments) or _approval_token_from_arguments(arguments)
    if not token:
        return next_context, None
    token_result = _verify_profile_tool_permission_token(next_context, tool_def, arguments, token)
    verification = token_result.get("verification")
    if verification is not None and verification.valid:
        approved_context = _seal_profile_tool_permission_context(next_context, decision, source="approval_token")
        approved_context["_tool_permission_policy_approved"] = True
        return _attach_tool_approval_token(approved_context, tool_def, token_result), None
    code = str(getattr(verification, "code", "") or "")
    if code in _STALE_APPROVAL_TOKEN_CODES:
        response = _approval_required_tool_response_for_context(tool_def, arguments or {}, next_context)
        if isinstance(response.get("widget"), dict):
            response["widget"]["stale_approval_token"] = True
            response["widget"]["stale_approval_code"] = code
        return next_context, response
    return next_context, {
        "result": getattr(verification, "message", None) or "approval token is invalid",
        "is_error": True,
        "widget": None,
    }


def _seal_profile_tool_permission_context(context, decision, *, source):
    sealed_decision = {
        "allowed": True,
        "action": "allow",
        "source": source,
        "policy_mode": decision.get("mode"),
        "policy_action": decision.get("action"),
        "risk": decision.get("risk"),
        "risk_level": decision.get("risk_level"),
        "tool_name": decision.get("tool_name"),
        "matched_by": decision.get("matched_by"),
        "matched_value": decision.get("matched_value"),
    }
    next_context = seal_tool_context(context if isinstance(context, dict) else {}, sealed_decision)
    next_context["_tool_permission_policy_decision"] = dict(decision)
    return next_context


def _issue_profile_tool_permission_token(context, tool_def, arguments, decision):
    try:
        approval = _approval_module()
        operation, approval_args = _tool_approval_scope(tool_def, arguments)
        pack_id = _approval_pack_id_from_context(context)
        conversation_id = _approval_conversation_id_from_context(context)
        request = approval.create_approval_request(
            operation,
            str(decision.get("risk_level") or _tool_approval_risk_level(tool_def)),
            approval_args,
            details={
                "tool_name": _tool_approval_tool_name(tool_def),
                "action": operation,
                "function_id": operation,
                "pack_id": pack_id,
                "conversation_id": conversation_id,
                "arguments": _tool_approval_display_arguments(tool_def, arguments or {}, approval_args),
                "auto_approved_by": "tool_permission_policy",
                "policy_decision": {
                    "mode": decision.get("mode"),
                    "matched_by": decision.get("matched_by"),
                    "matched_value": decision.get("matched_value"),
                    "risk": decision.get("risk"),
                    "risk_level": decision.get("risk_level"),
                },
            },
        )
        approved = approval.approve(request["request_id"])
        token = str(approved.get("token") or "").strip()
        if not token:
            return {"error": approved.get("reason") or "tool permission approval token was not issued"}
        return {
            "token": token,
            "operation": operation,
            "args_hash": approval.hash_arguments(approval_args),
            "pack_id": pack_id,
            "conversation_id": conversation_id,
            "request_id": request["request_id"],
        }
    except Exception as exc:
        return {"error": "tool permission approval token could not be issued: {}".format(exc)}


def _verify_profile_tool_permission_token(context, tool_def, arguments, token):
    approval = _approval_module()
    operation, approval_args = _tool_approval_scope(tool_def, arguments)
    pack_id = _approval_pack_id_from_context(context)
    conversation_id = _approval_conversation_id_from_context(context)
    args_hash = approval.hash_arguments(approval_args)
    verification = approval.verify_execution_token(
        token,
        operation,
        args_hash,
        pack_id=pack_id,
        conversation_id=conversation_id,
        consume=False,
    )
    return {
        "token": token,
        "operation": operation,
        "args_hash": args_hash,
        "pack_id": pack_id,
        "conversation_id": conversation_id,
        "verification": verification,
    }


def _attach_tool_approval_token(context, tool_def, token_result):
    token = str(token_result.get("token") or "").strip()
    if not token:
        return context
    next_context = dict(context or {}) if isinstance(context, dict) else {}
    operation = str(token_result.get("operation") or _tool_approval_operation(tool_def))
    tokens = mapping_or_empty(next_context.get("tool_approval_tokens"))
    for key in _dedupe_approval_token_keys(tool_def, operation):
        tokens.setdefault(key, token)
    next_context["tool_approval_tokens"] = tokens
    next_context["_tool_server_approved"] = True
    next_context["_tool_server_approval_token_valid"] = True
    next_context["_tool_server_approval_token"] = token
    next_context["_tool_server_approval_operation"] = operation
    next_context["_tool_server_approval_args_hash"] = str(token_result.get("args_hash") or "")
    next_context["_tool_server_approval_pack_id"] = str(token_result.get("pack_id") or "")
    next_context["_tool_server_approval_conversation_id"] = str(token_result.get("conversation_id") or "")
    next_context["_tool_permission_policy_approved"] = True
    return next_context


def _dedupe_approval_token_keys(tool_def, operation):
    keys = [
        operation,
        _tool_approval_tool_name(tool_def),
        _tool_approval_operation(tool_def),
        "tool.{}".format(_tool_approval_tool_name(tool_def)),
    ]
    seen = set()
    result = []
    for key in keys:
        value = str(key or "").strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def _approval_pack_id_from_context(context):
    context = context if isinstance(context, dict) else {}
    return str(context.get("owner_pack") or context.get("pack_id") or context.get("_source_pack_id") or "defaultspack")


def _approval_conversation_id_from_context(context):
    context = context if isinstance(context, dict) else {}
    return str(context.get("conversation_id") or context.get("conversation_turn_id") or "")


def _audit_profile_tool_permission(context, decision):
    if not isinstance(decision, dict) or decision.get("audit_required") is False:
        return
    try:
        audit_tool_policy(
            context if isinstance(context, dict) else {},
            "tool_permission_policy_decision",
            {
                "tool_name": decision.get("tool_name"),
                "action": decision.get("action"),
                "status": decision.get("status"),
                "mode": decision.get("mode"),
                "risk": decision.get("risk"),
                "risk_level": decision.get("risk_level"),
                "matched_by": decision.get("matched_by"),
                "matched_value": decision.get("matched_value"),
                "reason": decision.get("reason"),
            },
        )
    except Exception:
        pass


def _audit_frontend_tool_permission(context, decision, resolution):
    if not isinstance(decision, dict) or decision.get("audit_required") is False:
        return
    try:
        audit_tool_policy(
            context if isinstance(context, dict) else {},
            "frontend_tool_permission_decision",
            {
                "tool_name": decision.get("tool_name"),
                "action": decision.get("action"),
                "status": decision.get("status"),
                "mode": decision.get("mode"),
                "service_id": resolution.get("service_id") if isinstance(resolution, dict) else None,
                "permission_sources": resolution.get("sources") if isinstance(resolution, dict) else None,
                "reason": decision.get("reason"),
            },
        )
    except Exception:
        pass


def _tool_permission_denied_result(tool_def, arguments, decision):
    tool_name = _tool_approval_tool_name(tool_def)
    return {
        "result": "Tool '{}' denied by tool permission policy".format(tool_name),
        "is_error": True,
        "widget": {
            "type": "tool_permission_policy",
            "status": "denied",
            "tool_name": tool_name,
            "action": decision.get("action"),
            "risk_level": decision.get("risk_level"),
            "reason": decision.get("reason"),
            "arguments": _redact_sensitive_arguments(arguments if isinstance(arguments, dict) else {}),
        },
        "rejected_by_tool_permission_policy": True,
        "tool_permission_policy_decision": dict(decision),
    }


def _tool_permission_dry_run_result(tool_def, arguments, decision):
    tool_name = _tool_approval_tool_name(tool_def)
    return {
        "result": "Tool '{}' dry-run by tool permission policy".format(tool_name),
        "is_error": False,
        "widget": {
            "type": "tool_permission_policy",
            "status": "dry_run",
            "tool_name": tool_name,
            "action": decision.get("action"),
            "risk_level": decision.get("risk_level"),
            "reason": decision.get("reason"),
            "arguments": _redact_sensitive_arguments(arguments if isinstance(arguments, dict) else {}),
        },
        "dry_run": True,
        "tool_permission_policy_decision": dict(decision),
    }


def _preflight_user_requested_computer_approval(tool_name, tool_def, arguments, context):
    if tool_name not in {"browser_computer", "browser_use", "computer_use"}:
        return None
    if not isinstance(context, dict) or not context.get("user_requested_computer_use"):
        return None
    if not isinstance(tool_def, dict) or not _requires_approval(tool_def):
        return None
    if _context_has_tool_server_approval(context):
        return None
    if _approval_token_from_context(context, tool_def, arguments) or _approval_token_from_arguments(arguments):
        return None
    approval_arguments = _browser_computer_preflight_approval_arguments(tool_name, arguments, context)
    invalid_response = _invalid_user_requested_computer_approval_response(tool_name, approval_arguments, context)
    if invalid_response is not None:
        return invalid_response
    display_arguments = _browser_computer_preflight_display_arguments(tool_name, arguments, context)
    return _approval_required_tool_response(
        tool_def,
        approval_arguments,
        context,
        display_arguments=display_arguments,
    )


def _invalid_user_requested_computer_approval_response(tool_name, approval_arguments, context):
    if tool_name not in {"browser_computer", "browser_use", "computer_use"}:
        return None
    if not isinstance(context, dict) or not context.get("user_requested_computer_use"):
        return None
    if not isinstance(approval_arguments, dict):
        return None
    action = str(approval_arguments.get("action") or "").strip()
    payload = mapping_or_empty(approval_arguments.get("payload"))
    if action == "browser.open_url" and not str(payload.get("url") or "").strip():
        return _computer_use_invalid_arguments_result(
            tool_name,
            action,
            "browser.open_url requires a non-empty url. Retry with the required url argument, "
            "or use context/apps/windows/screenshot first to inspect the target.",
        )
    if action in {"computer.select_app", "computer.show_app"} and not any(
        str(payload.get(key) or "").strip() for key in ("app", "application", "name")
    ):
        return _computer_use_invalid_arguments_result(
            tool_name,
            action,
            f"{action} requires a non-empty app, application, or name. Retry with the required "
            "app argument, or use context/apps/windows/screenshot first to inspect available apps/windows.",
        )
    return None


def _computer_use_invalid_arguments_result(tool_name, action, message):
    return {
        "result": f"Tool '{tool_name}' rejected invalid {action} arguments: {message}",
        "is_error": True,
        "widget": None,
        "error_type": "invalid_computer_use_arguments",
        "rejected_by_tool_validation": True,
    }


def _browser_computer_preflight_approval_arguments(tool_name, arguments, context):
    if tool_name not in {"browser_computer", "browser_use", "computer_use"}:
        return arguments or {}
    if not isinstance(arguments, dict):
        return {}
    action, payload = _browser_computer_action_payload(tool_name, arguments)
    if not str(action or "").startswith(("browser.", "computer.")):
        return dict(arguments)
    if action == "browser.open_url":
        approval_payload = _computer_use_payload_with_context_defaults(action, payload, context)
        if (
            isinstance(context, dict)
            and context.get("user_requested_computer_use")
            and not any(key in approval_payload for key in ("persistent", "profile_id", "session_id"))
        ):
            approval_payload = dict(approval_payload)
            approval_payload["persistent"] = False
        approval_payload = _controller_browser_open_url_approval_payload(approval_payload)
        return _browser_computer_controller_request_arguments(action, approval_payload)
    payload = _computer_use_payload_with_context_defaults(action, payload, context)
    return _browser_computer_controller_request_arguments(action, payload)


def _browser_computer_preflight_display_arguments(tool_name, arguments, context):
    if tool_name not in {"browser_use", "computer_use"} or not isinstance(arguments, dict):
        return None
    action, payload = _browser_computer_action_payload(tool_name, arguments)
    if action != "browser.open_url":
        return None
    display_payload = _computer_use_payload_with_context_defaults(action, payload, context)
    for key in ("profile_id", "persistent", "target_app"):
        if key not in payload:
            display_payload.pop(key, None)
    return _browser_computer_controller_request_arguments(action, display_payload)


def _approval_required_tool_response_for_context(tool_def, arguments, context=None):
    tool_name = _tool_approval_tool_name(tool_def if isinstance(tool_def, dict) else {})
    approval_arguments = _browser_computer_preflight_approval_arguments(
        tool_name,
        arguments if isinstance(arguments, dict) else {},
        context,
    )
    invalid_response = _invalid_user_requested_computer_approval_response(tool_name, approval_arguments, context)
    if invalid_response is not None:
        return invalid_response
    display_arguments = _browser_computer_preflight_display_arguments(tool_name, arguments, context)
    return _approval_required_tool_response(
        tool_def,
        approval_arguments,
        context,
        display_arguments=display_arguments,
    )


def _context_with_tool_approval_token(context, tool_def, arguments, *extra_lookup_keys):
    next_context = dict(context or {}) if isinstance(context, dict) else {}
    if not isinstance(tool_def, dict):
        return next_context, None
    if _tool_has_autonomous_internal_approval(tool_def, arguments, next_context):
        mark_tool_server_approval_context(next_context)
        return next_context, None
    if not _requires_approval(tool_def):
        return next_context, None
    if _legacy_internal_tool_server_approval_context(next_context, tool_def):
        mark_tool_server_approval_context(next_context)
        return next_context, None
    if _context_has_tool_server_approval(next_context):
        return next_context, None
    token = _approval_token_from_context(next_context, tool_def, arguments, *extra_lookup_keys) or _approval_token_from_arguments(arguments)
    if not token:
        return next_context, None
    approval = _approval_module()
    pack_id = str(next_context.get("owner_pack") or next_context.get("pack_id") or next_context.get("_source_pack_id") or "defaultspack")
    conversation_id = str(next_context.get("conversation_id") or next_context.get("conversation_turn_id") or "")
    operation, approval_args = _tool_approval_scope(tool_def, arguments)
    args_hash = approval.hash_arguments(approval_args)
    candidates = [(operation, args_hash, pack_id, conversation_id)]
    if (
        _tool_approval_tool_name(tool_def) in {"browser_computer", "browser_use", "computer_use"}
        and isinstance(arguments, dict)
    ):
        action, payload = _browser_computer_action_payload(_tool_approval_tool_name(tool_def), arguments)
        if (
            isinstance(next_context, dict)
            and next_context.get("user_requested_computer_use")
            and action == "browser.open_url"
            and not any(key in payload for key in ("persistent", "profile_id", "session_id"))
        ):
            payload = dict(payload)
            payload["persistent"] = False
        payload_candidates = [payload]
        context_payload = _computer_use_payload_with_context_defaults(action, payload, next_context)
        if context_payload != payload:
            payload_candidates.append(context_payload)
        for candidate_payload in payload_candidates:
            payload_only_args = _approval_hash_arguments(
                _browser_computer_payload_only_request_arguments(action, candidate_payload)
            )
            candidates.append(
                (operation, approval.hash_arguments(payload_only_args), pack_id, conversation_id),
            )
            for controller_payload in _browser_computer_controller_approval_payloads(
                action,
                candidate_payload,
                next_context,
            ):
                controller_args = _approval_hash_arguments(
                    _browser_computer_controller_request_arguments(action, controller_payload)
                )
                candidates.append(
                    (operation, approval.hash_arguments(controller_args), pack_id, conversation_id),
                )
            legacy_scoped_args = _approval_hash_arguments(_browser_computer_legacy_request_arguments(
                _tool_approval_tool_name(tool_def),
                action,
                candidate_payload,
            ))
            candidates.append(
                (operation, approval.hash_arguments(legacy_scoped_args), pack_id, conversation_id),
            )
        legacy_args_hash = approval.hash_arguments(_approval_replayable_arguments(arguments))
        legacy_operation = _tool_approval_operation(tool_def)
        candidates.extend(
            [
                (
                    legacy_operation,
                    legacy_args_hash,
                    pack_id,
                    conversation_id,
                ),
                (
                    legacy_operation,
                    legacy_args_hash,
                    "",
                    "",
                ),
            ]
        )
    verification = None
    verified_operation = operation
    verified_args_hash = args_hash
    verified_pack_id = pack_id
    verified_conversation_id = conversation_id
    for candidate_operation, candidate_args_hash, candidate_pack_id, candidate_conversation_id in candidates:
        candidate_verification = approval.verify_execution_token(
            token,
            candidate_operation,
            candidate_args_hash,
            pack_id=candidate_pack_id,
            conversation_id=candidate_conversation_id,
            consume=False,
        )
        if candidate_verification.valid:
            verification = candidate_verification
            verified_operation = candidate_operation
            verified_args_hash = candidate_args_hash
            verified_pack_id = candidate_pack_id
            verified_conversation_id = candidate_conversation_id
            break
        if verification is None:
            verification = candidate_verification
    if verification is None:
        return next_context, {
            "result": "approval token could not be verified",
            "is_error": True,
            "widget": None,
        }
    if verification.valid:
        mark_tool_server_approval_context(next_context)
        next_context["_tool_server_approval_token"] = token
        next_context["_tool_server_approval_operation"] = verified_operation
        next_context["_tool_server_approval_args_hash"] = verified_args_hash
        next_context["_tool_server_approval_pack_id"] = verified_pack_id
        next_context["_tool_server_approval_conversation_id"] = verified_conversation_id
        return next_context, None
    if _tool_approval_tool_name(tool_def) in {"browser_computer", "browser_use", "computer_use"}:
        return next_context, _approval_required_tool_response_for_context(tool_def, arguments, next_context)
    if verification.code == "APPROVAL_TOKEN_USED":
        return next_context, {
            "result": verification.message or "approval token has already been used",
            "is_error": True,
            "widget": None,
        }
    if verification.code in _STALE_APPROVAL_TOKEN_CODES:
        return next_context, _approval_required_tool_response_for_context(tool_def, arguments, next_context)
    return next_context, {
        "result": verification.message or "approval token is invalid",
        "is_error": True,
        "widget": None,
    }


def _approval_required_tool_response(tool_def, arguments, context=None, *, display_arguments=None):
    tool_name = _tool_approval_tool_name(tool_def)
    operation, approval_args = _tool_approval_scope(tool_def, arguments)
    risk_level = _tool_approval_risk_level(tool_def)
    args = approval_args
    visible_arguments = display_arguments if isinstance(display_arguments, dict) else arguments
    display_args = _tool_approval_display_arguments(tool_def, visible_arguments, approval_args)
    display_payload = _tool_approval_display_payload(tool_def, visible_arguments, approval_args)
    context = context if isinstance(context, dict) else {}
    request = _approval_module().create_approval_request(
        operation,
        risk_level,
        args,
        details={
            "tool_name": tool_name,
            "action": operation,
            "function_id": operation,
            "pack_id": str(context.get("owner_pack") or context.get("pack_id") or context.get("_source_pack_id") or "defaultspack"),
            "conversation_id": str(context.get("conversation_id") or context.get("conversation_turn_id") or ""),
            "arguments": args,
        },
    )
    is_computer_tool = tool_name in {"browser_computer", "browser_use", "computer_use"}
    prompt = _COMPUTER_APPROVAL_PROMPT if is_computer_tool else ""
    recovery = (
        {
            "kind": "approval_required",
            "requires_approval": True,
            "prompt": prompt,
            "note": (
                "foreground/on-screen operation is available after approval; "
                "approve the request or choose foreground work."
            ),
            "recommended_next_actions": ["approve_request", "choose_foreground_work"],
        }
        if is_computer_tool
        else None
    )
    return {
        "result": prompt or "Tool '{}' requires approval".format(tool_name),
        "is_error": False,
        "widget": {
            "type": "approval_request",
            "tool_name": tool_name,
            "approval_required": True,
            "requires_approval": True,
            "risk_level": risk_level,
            "operation": operation,
            "action": operation,
            "arguments": _redact_sensitive_arguments(display_args),
            "payload": _redact_sensitive_arguments(display_payload),
            "approval_request_id": request["request_id"],
            "args_hash": request["args_hash"],
            "expires_at": request["expires_at"],
            "display_summary": request["display_summary"],
            **({"message": prompt, "user_prompt": prompt} if prompt else {}),
            **({"recovery": recovery} if recovery else {}),
        },
        **({"message": prompt, "user_prompt": prompt} if prompt else {}),
        **({"recovery": recovery} if recovery else {}),
    }


def _pack_not_approved_tool_response(tool_def, response, *, include_widget=True):
    reason = str(getattr(response, "error", None) or "Pack not approved")
    widget = None
    if include_widget:
        widget = {
            "type": "tool_execution_denied",
            "tool_name": _tool_approval_tool_name(tool_def),
            "reason": reason,
        }
    return {
        "result": reason,
        "is_error": True,
        "widget": widget,
        "error_type": "pack_not_approved",
        "pack_not_approved_reason": reason,
    }


def _redact_sensitive_arguments(value):
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_argument_key(key_text):
                redacted[key_text] = "[redacted]"
            else:
                redacted[key_text] = _redact_sensitive_arguments(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_arguments(item) for item in value]
    return value


def _is_sensitive_argument_key(key):
    lowered = str(key or "").lower()
    return any(
        marker in lowered
        for marker in ("api_key", "authorization", "bearer", "credential", "password", "secret", "token")
    )


def _is_high_risk_approval(tool_def):
    if str(_tool_value(tool_def, "risk") or "").strip().lower() == "high":
        return True
    grants = _tool_value(tool_def, "capability_grants")
    if isinstance(grants, list):
        high_risk_prefixes = (
            "browser.",
            "computer.",
            "file.write",
            "git.write",
            "network.send",
            "terminal.exec",
        )
        for grant in grants:
            if str(grant or "").strip().startswith(high_risk_prefixes):
                return True
    return _is_shell_or_git(tool_def)


def _is_shell_or_git(tool_def):
    action_type = str(_tool_value(tool_def, "action_type") or "")
    category = str(_tool_value(tool_def, "category") or "")
    return action_type == "shell" or category in {"shell", "git"}


def _is_policy_allow_context(context):
    return internal_tool_decision_allows(context)


def _context_has_tool_server_approval(context):
    if not isinstance(context, dict):
        return False
    policy = policy_from_context(context)
    if _truthy(policy.get("yolo_mode")) or _is_policy_allow_context(context):
        return True
    return tool_server_approval_context_is_internal(context)


def _legacy_internal_tool_server_approval_context(context, tool_def):
    if not isinstance(context, dict) or context.get("_tool_server_approved") is not True:
        return False
    if tool_server_approval_context_is_internal(context):
        return True
    if context.get("_tool_server_approval_token_valid") is True:
        return False
    if _is_policy_allow_context(context):
        return True
    tool_name = _tool_approval_tool_name(tool_def if isinstance(tool_def, dict) else {})
    if tool_name in {"browser_computer", "browser_use", "computer_use"}:
        return _has_internal_runtime_handle(context)
    source_pack_id = str(_tool_value(tool_def, "source_pack_id") or "").strip()
    if not is_trusted_pack_id(source_pack_id):
        return False
    for key in ("principal_id", "pack_id", "_source_pack_id", "owner_pack"):
        value = str(context.get(key) or "").strip()
        if value and value == source_pack_id:
            return True
    return False


def _has_internal_runtime_handle(context):
    if not isinstance(context, dict):
        return False
    for key in ("capability_executor", "_capability_executor"):
        candidate = context.get(key)
        if candidate is not None and callable(getattr(candidate, "execute", None)):
            return True
    for key in ("is_cancelled", "run_event_sink", "stream_event_callback"):
        if callable(context.get(key)):
            return True
    return False


def _function_call_context(context, tool_def):
    if not isinstance(context, dict):
        return {}
    if isinstance(tool_def, dict) and is_sandbox_capability_tool(tool_def):
        return _sandbox_function_call_context(context)
    forwarded = {}
    for key in (
        "workspace_id",
        "workspace_root",
        "conversation_id",
        "company_id",
        "conversation_workspace_dir",
        "profile_id",
        "run_id",
        "request_id",
        "profile_policy",
        "user_requested_computer_use",
        "computer_use_target_app",
        "computer_use_target_title",
        "computer_use_foreground_preferred",
        "computer_use_mouse_keyboard_requested",
        "computer_use_physical_clicks",
    ):
        if key in context and _json_safe_value(context.get(key)):
            forwarded[key] = context.get(key)
    if "workspace_root" not in forwarded and _needs_cwd_workspace_default(tool_def):
        forwarded["workspace_root"] = str(Path.cwd())
    policy = policy_from_context(context)
    if _truthy(policy.get("yolo_mode")) or _is_policy_allow_context(context):
        forwarded["_tool_server_approved"] = True
    if tool_server_approval_context_is_internal(context):
        forwarded["_tool_server_approved"] = True
        forwarded["_tool_server_approval_token_valid"] = True
        for key in (
            "_tool_server_approval_token",
            "_tool_server_approval_operation",
            "_tool_server_approval_args_hash",
            "_tool_server_approval_pack_id",
            "_tool_server_approval_conversation_id",
        ):
            value = context.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                forwarded[key] = value
    return forwarded


def _execution_timeout_seconds(tool_def):
    if not isinstance(tool_def, dict):
        return None
    execution = tool_def.get("execution")
    if not isinstance(execution, dict):
        return None
    raw = execution.get("timeout_seconds")
    if raw is None:
        raw = execution.get("timeout")
    if raw is None:
        return None
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        return None
    if timeout <= 0:
        return None
    return min(timeout, 300.0)


def _sandbox_function_call_context(context):
    forwarded = {}
    for key in (
        "workspace_id",
        "workspace_root",
        "conversation_id",
        "chat_id",
        "profile_id",
        "principal_id",
        "run_id",
        "request_id",
    ):
        if key in context and _json_safe_value(context.get(key)):
            forwarded[key] = context.get(key)
    session_id = str(context.get("_sandbox_session_id") or "").strip()
    if session_id and "/" not in session_id and "\x00" not in session_id:
        forwarded["_sandbox_session_id"] = session_id
    return forwarded


def _needs_cwd_workspace_default(tool_def):
    grants = _tool_value(tool_def, "capability_grants")
    if isinstance(grants, list):
        for grant in grants:
            value = str(grant or "").strip()
            if value.startswith(("file.", "git.", "terminal.")):
                return True
    category = str(_tool_value(tool_def, "category") or "").strip()
    return category in {"coding", "file", "filesystem", "git", "shell", "terminal"}


def _is_explicitly_untrusted_tool(tool_def):
    if not isinstance(tool_def, dict):
        return False
    metadata = tool_def.get("metadata")
    if isinstance(metadata, dict):
        if metadata.get("trusted") is False:
            return True
        source_pack_id = metadata.get("source_pack_id")
        if isinstance(source_pack_id, str) and source_pack_id.strip():
            return not is_trusted_pack_id(source_pack_id)
    source_pack_id = tool_def.get("source_pack_id")
    if isinstance(source_pack_id, str) and source_pack_id.strip():
        return not is_trusted_pack_id(source_pack_id)
    if "trusted" in tool_def and tool_def.get("trusted") is False:
        return True
    return False


def _json_safe_value(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return True
    except (TypeError, ValueError):
        return False
