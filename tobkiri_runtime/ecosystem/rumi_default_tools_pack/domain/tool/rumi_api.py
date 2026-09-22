from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_PACK_ROOT = Path(__file__).resolve().parents[2]
_DEFAULTSPACK_ROOT = _PACK_ROOT.parent / "defaultspack"
_RUMI_ROOT = _PACK_ROOT.parent.parent
for _path in reversed((str(_RUMI_ROOT), str(_DEFAULTSPACK_ROOT), str(_PACK_ROOT))):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from ecosystem.defaultspack.blocks._common import error, ok  # noqa: E402
from ecosystem.defaultspack.domain.tool_policy.internal_context import (  # noqa: E402
    internal_tool_decision_allows,
)
from core_runtime.di_container import get_container  # noqa: E402
from core_runtime.global_contract_dispatch import (  # noqa: E402
    GlobalContractInvocationError,
    GlobalContractUnavailable,
    captured_profile_id,
    invoke_global_contract,
)

_CONSUMER_PACK_ID = "rumi_default_tools_pack"


def run(arguments: dict[str, Any], context: dict[str, Any] | None = None):
    action = str(arguments.get("action") or "list_routes").strip()
    if action == "list_routes":
        return ok(
            {
                "routes": [],
                "count": 0,
                "dispatch": "captured_v4_qualified_operations_only",
            }
        )
    if action == "request":
        return error(
            "Legacy HTTP routes are disabled; use a captured v4 dispatch session",
            "LEGACY_HTTP_DISABLED",
        )
    if action != "dispatch":
        return error("unsupported action: " + action, "INVALID_ACTION")

    if not _request_allowed(context or {}):
        return ok(
            {
                "approval_required": True,
                "tool_name": "rumi_api",
                "contract_id": str(arguments.get("contract_id") or ""),
                "operation_id": str(arguments.get("operation_id") or ""),
                "reason": "v4 dispatch requires an approved tool context",
            }
        )

    contract_id = str(arguments.get("contract_id") or "").strip()
    operation_id = str(arguments.get("operation_id") or "").strip()
    payload = arguments.get("payload")
    if not contract_id or not operation_id:
        return error(
            "contract_id and operation_id are required",
            "INVALID_QUALIFIED_OPERATION",
        )
    if not isinstance(payload, dict):
        return error("payload must be an object", "INVALID_PAYLOAD")
    if "_contract_consumer_pack_id" in payload:
        return error(
            "contract consumer identity is Host-owned",
            "FORGED_CONSUMER_IDENTITY",
        )

    session = (context or {}).get("v4_dispatch_session")
    if session is None:
        session = get_container().get_or_none("v4_dispatch_session")
    try:
        profile_id = captured_profile_id(session)
        result = invoke_global_contract(
            session,
            contract_id,
            operation_id,
            {**payload, "_contract_consumer_pack_id": _CONSUMER_PACK_ID},
        )
    except GlobalContractUnavailable as exc:
        return error(
            str(exc),
            "V4_DISPATCH_SESSION_REQUIRED",
        )
    except GlobalContractInvocationError as exc:
        return error(str(exc), exc.code)
    except Exception as exc:
        return error("qualified v4 operation failed: " + str(exc), "V4_DISPATCH_FAILED")

    return ok({"profile_id": profile_id, "result": result})


def _request_allowed(context: dict[str, Any]) -> bool:
    if internal_tool_decision_allows(context):
        return True
    policy = context.get("profile_policy") if isinstance(context.get("profile_policy"), dict) else {}
    if bool(policy.get("yolo_mode")):
        return True
    if context.get("_tool_server_approval_token_valid") is True:
        return True
    return bool(
        context.get("_tool_server_approved")
        and any(str(context.get(key) or "").strip() for key in ("principal_id", "pack_id", "_source_pack_id"))
    )
