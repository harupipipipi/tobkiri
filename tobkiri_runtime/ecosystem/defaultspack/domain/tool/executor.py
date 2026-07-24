from .registry import ToolRegistry
from .mcp_client import McpClient
from .mcp_registry import McpRegistry
from .autonomy import autonomous_tool_execution_allowed
from .eligibility import rejection_result
from .permission_resolver import ToolPermissionResolver
from .schema_adapter import is_tool_rejected_by_policy, policy_from_context
from .service_catalog import infer_action_class
from .security import (
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
from pathlib import Path
import inspect
import json
import logging
import os

logger = logging.getLogger(__name__)


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


def json_dumps(value):
    return json.dumps(value, ensure_ascii=False)


def _approval_module():
    from domain.safety import approval

    return approval


class ToolExecutor:
    """ツール実行エンジン"""

    def __init__(self):
        try:
            from domain.integrations.secrets import load_integration_secrets_into_env

            load_integration_secrets_into_env()
        except Exception:
            pass
        self._registry = ToolRegistry()
        self._mcp_client = McpClient()

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
            return _approval_required_tool_response(tool_def, arguments or {}, approved_context)
        forwarded_context = _function_call_context(approved_context, tool_def)
        if forwarded_context:
            request["context"] = forwarded_context
        self._ensure_shared_function_registered(qualified_name)
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
            if (
                isinstance(context, dict)
                and tool_server_approval_context_is_internal(context)
                and getattr(response, "error_type", "") == "pack_not_approved"
                and str(request.get("type") or "").strip() == "function.call"
            ):
                qualified_name = str(request.get("qualified_name") or "").strip()
                pack_id, _, _ = qualified_name.partition(":")
                if pack_id and self._dev_auto_approve_pack(pack_id):
                    response = executor.execute(principal_id, request)
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
            return True, None
        approved = manager.is_pack_approved_and_verified(pack_id)
        if isinstance(approved, tuple):
            return bool(approved[0]), approved[1]
        return bool(approved), None

    def _dev_auto_approve_pack(self, pack_id, capability_executor=None):
        rumi_env = os.environ.get("RUMI_ENVIRONMENT", "").lower()
        auto_approve = os.environ.get("RUMI_AUTO_APPROVE_LOCAL", "").lower()
        if rumi_env not in {"development", "dev"} or auto_approve != "true":
            return False
        manager = getattr(capability_executor, "_approval_manager", None)
        if manager is None:
            try:
                from core_runtime.approval_manager import get_approval_manager

                manager = get_approval_manager()
            except Exception:
                manager = None
        if manager is None:
            return False
        try:
            if hasattr(manager, "scan_packs"):
                manager.scan_packs()
            result = manager.approve(pack_id)
            return bool(getattr(result, "success", False))
        except Exception:
            return False

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
                approved, reason = self._function_call_pack_approval_status(capability_executor, pack_id)
                if not approved and self._dev_auto_approve_pack(pack_id, capability_executor):
                    approved, reason = self._function_call_pack_approval_status(capability_executor, pack_id)
                if not approved:
                    return {
                        "result": "Pack not approved: {}".format(pack_id),
                        "is_error": True,
                        "widget": {
                            "type": "tool_execution_denied",
                            "tool_name": _tool_approval_tool_name(tool_def),
                            "reason": "Pack not approved: {}".format(pack_id),
                        },
                        "error_type": "pack_not_approved",
                        "pack_not_approved_reason": reason,
                    }
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
    def _ensure_shared_function_registered(qualified_name):
        try:
            from core_runtime.di_container import get_container

            registry = get_container().get("function_registry")
        except Exception:
            return
        try:
            if registry.get(qualified_name) is not None:
                return
        except Exception:
            return
        ToolExecutor._load_pack_functions_into_registry(registry)

    @staticmethod
    def _load_pack_functions_into_registry(registry):
        from core_runtime.resolved_profile_scope import effective_pack_ids

        ecosystem_dir = Path(__file__).resolve().parents[3]
        effective = effective_pack_ids()
        for pack_id in sorted(effective):
            pack_root = ecosystem_dir / pack_id
            if not pack_root.is_dir() or not (pack_root / "ecosystem.json").exists():
                continue
            try:
                pack_manifest = json.loads((pack_root / "ecosystem.json").read_text(encoding="utf-8"))
            except Exception:
                pack_manifest = {}
            pack_id = str(pack_manifest.get("pack_id") or pack_root.name).strip() or pack_root.name
            functions_root = pack_root / "functions"
            if not functions_root.exists():
                continue
            for function_dir in sorted(path for path in functions_root.iterdir() if path.is_dir()):
                manifest_path = function_dir / "manifest.json"
                if not manifest_path.is_file():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                function_id = str(manifest.get("function_id") or function_dir.name).strip()
                if not function_id:
                    continue
                try:
                    registry.register(
                        pack_id=pack_id,
                        function_id=function_id,
                        manifest=manifest,
                        function_dir=function_dir,
                    )
                except Exception:
                    continue

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
                    return _approval_required_tool_response(tool_def, arguments or {}, context)
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
                return _approval_required_tool_response(tool_def, arguments or {}, context)
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
            return _approval_required_tool_response(tool_def, next_arguments, next_context)

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
        elif tool_name == "file_reader":
            from blocks.coding.file_read import run as file_read_run

            path = arguments.get("path", "")
            call_context = context if isinstance(context, dict) else {}
            result = file_read_run(
                {
                    "path": path,
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
    capability_graph = context.get("capability_graph") if isinstance(context.get("capability_graph"), dict) else {}
    connected = capability_graph.get("connected_tools") if isinstance(capability_graph.get("connected_tools"), list) else []
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

    operators = {
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


def _browser_computer_action_payload(tool_name, arguments):
    arguments = arguments if isinstance(arguments, dict) else {}
    if tool_name == "browser_computer":
        return str(arguments.get("action", "browser.session")), dict(arguments.get("payload") or {})

    raw_payload = dict(arguments.get("payload") or {})
    raw_action = str(arguments.get("action") or "").strip()
    if tool_name == "browser_use":
        action_map = {
            "": "browser.session",
            "session": "browser.session",
            "open_url": "browser.open_url",
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
        action = action_map.get(raw_action, raw_action)
        for key in (
            "url",
            "url_contains",
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
        ):
            if key in arguments:
                raw_payload[key] = arguments.get(key)
    else:
        action_map = {
            "": "computer.screenshot",
            "open_url": "browser.open_url",
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
        action = action_map.get(raw_action, raw_action)
        for key in (
            "url",
            "url_contains",
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
        ):
            if key in arguments:
                raw_payload[key] = arguments.get(key)
    if "dry_run" in arguments:
        raw_payload["dry_run"] = arguments.get("dry_run")
    if "approval_token" in arguments:
        raw_payload["approval_token"] = arguments.get("approval_token")
    return action, raw_payload


def _computer_use_payload_with_context_defaults(action, payload, context):
    payload = dict(payload or {})
    if not isinstance(context, dict):
        return payload
    target_app = context.get("computer_use_target_app")
    target_title = context.get("computer_use_target_title")
    physical_clicks = _truthy(context.get("computer_use_physical_clicks"))
    if action == "browser.open_url":
        if isinstance(target_app, str) and target_app.strip() and not any(
            payload.get(key) for key in ("app", "application", "browser", "browser_app")
        ):
            payload["app"] = target_app.strip()
        return payload
    if action.startswith("computer.") and action not in {"computer.windows", "computer.apps"}:
        if isinstance(target_app, str) and target_app.strip():
            if action in {"computer.select_app", "computer.show_app"}:
                if not any(payload.get(key) for key in ("app", "application", "name")):
                    payload["app"] = target_app.strip()
            else:
                payload.setdefault("app", target_app.strip())
        if (
            isinstance(target_title, str)
            and target_title.strip()
            and action not in {"computer.select_app", "computer.show_app"}
        ):
            payload.setdefault("title", target_title.strip())
        if physical_clicks and action == "computer.click" and "physical" not in payload:
            payload["physical"] = True
    return payload


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
                if key not in {"approval_token", "approved"}
            }
        if isinstance(value, list):
            return [sanitize(item) for item in value]
        return value

    return sanitize(dict(arguments))


def _browser_computer_request_arguments(tool_name, action, payload):
    if tool_name == "browser_computer":
        return {
            "action": str(action or "browser.session"),
            "payload": dict(payload or {}),
        }
    return dict(payload or {})


def _browser_computer_legacy_request_arguments(tool_name, action, payload):
    if tool_name == "browser_computer":
        return _browser_computer_request_arguments(tool_name, action, payload)
    return {
        "action": str(action or ""),
        **dict(payload or {}),
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
        return context, _approval_required_tool_response(tool_def, arguments or {}, context)
    if status == "allowed":
        return _context_with_profile_tool_permission_allow(context, tool_def, arguments, decision)
    return context, None


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
            return context, _approval_required_tool_response(tool_def, arguments or {}, context)
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
    return context, _approval_required_tool_response(tool_def, arguments or {}, context)


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
        response = _approval_required_tool_response(tool_def, arguments or {}, next_context)
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
        response = _approval_required_tool_response(tool_def, arguments or {}, next_context)
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
    tokens = dict(next_context.get("tool_approval_tokens") if isinstance(next_context.get("tool_approval_tokens"), dict) else {})
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
    return _approval_required_tool_response(tool_def, arguments or {}, context)


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
        legacy_scoped_args = _approval_hash_arguments(_browser_computer_legacy_request_arguments(
            _tool_approval_tool_name(tool_def),
            action,
            payload,
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
    if verification.valid:
        mark_tool_server_approval_context(next_context)
        next_context["_tool_server_approval_token"] = token
        next_context["_tool_server_approval_operation"] = verified_operation
        next_context["_tool_server_approval_args_hash"] = verified_args_hash
        next_context["_tool_server_approval_pack_id"] = verified_pack_id
        next_context["_tool_server_approval_conversation_id"] = verified_conversation_id
        return next_context, None
    if _tool_approval_tool_name(tool_def) in {"browser_computer", "browser_use", "computer_use"}:
        return next_context, _approval_required_tool_response(tool_def, arguments, next_context)
    if verification.code == "APPROVAL_TOKEN_USED":
        return next_context, {
            "result": verification.message or "approval token has already been used",
            "is_error": True,
            "widget": None,
        }
    if verification.code in _STALE_APPROVAL_TOKEN_CODES:
        return next_context, _approval_required_tool_response(tool_def, arguments, next_context)
    return next_context, {
        "result": verification.message or "approval token is invalid",
        "is_error": True,
        "widget": None,
    }


def _approval_required_tool_response(tool_def, arguments, context=None):
    tool_name = _tool_approval_tool_name(tool_def)
    operation, approval_args = _tool_approval_scope(tool_def, arguments)
    risk_level = _tool_approval_risk_level(tool_def)
    args = approval_args
    display_args = _tool_approval_display_arguments(tool_def, arguments, approval_args)
    display_payload = _tool_approval_display_payload(tool_def, arguments, approval_args)
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
            "arguments": display_args,
        },
    )
    return {
        "result": "Tool '{}' requires approval".format(tool_name),
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
        },
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
