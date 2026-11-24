"""Read a path-free Pack approval status for delegated CLI verification."""

from __future__ import annotations

from blocks._common import error, ok


def run(input_data, context=None):
    del context
    pack_id = str(input_data.get("pack_id") or "").strip()
    if not pack_id:
        return error("'pack_id' is required", code="INVALID_INPUT")

    from core_runtime.approval_manager import get_approval_manager

    manager = get_approval_manager()
    manager.scan_packs()
    status = manager.get_status(pack_id)
    if status is None:
        result = error("Pack not found", code="PACK_NOT_FOUND")
        result["_http_status"] = 404
        return result
    verified, reason = manager.is_pack_approved_and_verified(pack_id)
    return ok(
        {
            "pack_id": pack_id,
            "status": status.value,
            "approved_and_verified": bool(verified),
            "reason": None if verified else str(reason or "not_approved"),
        }
    )
