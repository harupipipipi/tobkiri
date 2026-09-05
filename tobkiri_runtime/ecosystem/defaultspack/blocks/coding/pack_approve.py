"""Execute one exact Pack approval after delegated Launcher authorization."""

from __future__ import annotations

from blocks._common import error, ok
from domain.safety import approval


def run(input_data, context=None):
    del context
    target_pack_id = str(input_data.get("target_pack_id") or "").strip()
    snapshot_digest = str(input_data.get("snapshot_digest") or "").strip()
    token = str(input_data.get("approval_token") or "").strip()
    if not target_pack_id or len(snapshot_digest) != 64:
        return error("Pack approval binding is incomplete", code="INVALID_INPUT")

    verification = approval.verify_execution_token(
        token,
        "pack.approve",
        approval.hash_arguments(input_data),
        consume=True,
    )
    if not verification.valid:
        result = error(
            verification.message or "Pack approval token is invalid",
            code=verification.code or "APPROVAL_INVALID",
        )
        result["_http_status"] = 403
        return result

    from core_runtime.approval_manager import get_approval_manager

    manager = get_approval_manager()
    manager.scan_packs()
    result = manager.approve_if_snapshot(target_pack_id, snapshot_digest)
    if not result.success:
        denied = error(
            str(result.error or "Pack approval failed"),
            code="PACK_APPROVAL_SNAPSHOT_MISMATCH",
        )
        denied["_http_status"] = 409
        return denied
    verified, reason = manager.is_pack_approved_and_verified(target_pack_id)
    if not verified:
        return error(
            str(reason or "Pack approval verification failed"),
            code="PACK_APPROVAL_VERIFY_FAILED",
        )
    return ok({"approved": True, "verified": True})
