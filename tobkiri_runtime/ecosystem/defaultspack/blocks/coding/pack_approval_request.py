"""Create a Pack approval request bound to the active Launcher debug lease."""

from __future__ import annotations

from blocks._common import error, ok
from domain.safety import approval


def run(input_data, context=None):
    del context
    target_pack_id = str(input_data.get("pack_id") or "").strip()
    if not target_pack_id:
        return error("'pack_id' is required", code="INVALID_INPUT")

    from core_runtime.approval_manager import get_approval_manager

    manager = get_approval_manager()
    manager.scan_packs()
    snapshot = manager.get_pack_approval_snapshot(target_pack_id)
    if not snapshot.get("success"):
        result = error(
            str(snapshot.get("error") or "Pack snapshot unavailable"),
            code="PACK_SNAPSHOT_UNAVAILABLE",
        )
        result["_http_status"] = 404
        return result

    arguments = {
        "target_pack_id": target_pack_id,
        "snapshot_digest": str(snapshot["snapshot_digest"]),
    }
    request = approval.create_approval_request(
        "pack.approve",
        "high",
        arguments,
        details={
            "arguments": arguments,
            "tool_name": "coding_pack_approve",
            "function_id": "coding_pack_approve",
            "action": "pack.approve",
            "conversation_id": "debug-pack-approval",
            "operation_owner": "core_runtime",
            "target_digest": arguments["snapshot_digest"],
        },
    )
    if not request.get("debug_session_id"):
        approval.mark_obsolete(
            str(request.get("request_id") or ""),
            "active Launcher debug approval is required",
        )
        result = error(
            "active Launcher debug approval is required",
            code="DEBUG_SESSION_REQUIRED",
        )
        result["_http_status"] = 403
        return result
    return ok(
        {
            "approval_required": True,
            "operation": "pack.approve",
            "approval_request_id": request["request_id"],
            "expected_digest": request["args_hash"],
            "snapshot_digest": arguments["snapshot_digest"],
            "expires_at": request["expires_at"],
            "status": str(snapshot.get("status") or "unknown"),
            "file_count": int(snapshot.get("file_count") or 0),
        }
    )
