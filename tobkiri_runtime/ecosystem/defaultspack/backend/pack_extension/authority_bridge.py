"""Bridge defaultspack pack-review requests into core authority approvals."""

from __future__ import annotations

import os
from typing import Any


PACK_AUTHORITY_RESOURCE_KIND = "defaultspack.pack_request"


def _profile_id(value: str = "") -> str:
    for candidate in (
        value,
        os.environ.get("RUMI_PROFILE_ID"),
        os.environ.get("RUMI_ACTIVE_PROFILE_ID"),
        "default",
    ):
        cleaned = str(candidate or "").strip()
        if cleaned:
            return cleaned
    return "default"


def _pack_authority_principal(profile_id: str) -> str:
    return f"profile:{_profile_id(profile_id)}__surface:defaultspack__node:pack-review"


def pack_request_resource(request: Any) -> dict[str, Any]:
    if hasattr(request, "to_dict"):
        data = request.to_dict()
    else:
        data = dict(request or {})
    request_id = str(data.get("request_id") or "").strip()
    mode = str(data.get("mode") or "").strip()
    actor = str(data.get("actor") or data.get("pack_id") or "defaultspack").strip()
    target_pack_id = str(data.get("target_pack_id") or "").strip()
    staging_id = str(data.get("staging_id") or "").strip()
    return {
        "kind": PACK_AUTHORITY_RESOURCE_KIND,
        "pack_id": actor or "defaultspack",
        "target_pack_id": target_pack_id,
        "pack_request_id": request_id,
        "mode": mode,
        "staging_id": staging_id,
        "slot": str(data.get("slot") or "default").strip() or "default",
        "changed_paths": list(data.get("changed_paths") or []),
        "detected_pack_ids": list(data.get("detected_pack_ids") or []),
    }


def ensure_authority_request_for_pack_request(
    request: Any,
    *,
    profile_id: str = "",
) -> dict[str, Any]:
    resource = pack_request_resource(request)
    if not resource.get("pack_request_id"):
        return {"success": False, "error": "pack request id is missing"}
    try:
        from core_runtime.authority import get_authority_service

        resolved_profile = _profile_id(profile_id)
        decision = get_authority_service().check(
            principal_id=_pack_authority_principal(resolved_profile),
            permission_id="pack.approve",
            resource=resource,
            reason=f"Pack request {resource['pack_request_id']} requires approval",
            profile_id=resolved_profile,
            node_id="pack-review",
        )
        data = decision.to_dict()
        data["success"] = True
        return data
    except Exception as exc:
        return {"success": False, "error": str(exc)}


def sync_pending_pack_requests_to_authority(*, profile_id: str = "") -> dict[str, Any]:
    try:
        from .extension_manager import get_extension_manager

        requests = get_extension_manager().list_pending()
    except Exception as exc:
        return {"success": False, "error": str(exc), "synced": 0}
    synced = []
    for request in requests:
        decision = ensure_authority_request_for_pack_request(request, profile_id=profile_id)
        if decision.get("success") and decision.get("request_id"):
            synced.append(
                {
                    "pack_request_id": getattr(request, "request_id", ""),
                    "authority_request_id": decision.get("request_id"),
                }
            )
    return {"success": True, "synced": len(synced), "requests": synced}


def apply_pack_decision_for_authority_request(
    authority_request_id: str,
    *,
    decision: str,
    reviewer: str = "mobile-authority",
    notes: str = "",
) -> dict[str, Any]:
    try:
        from core_runtime.authority import get_authority_service
        from .extension_manager import get_extension_manager

        authority_request = get_authority_service().get_request(authority_request_id).get("request")
    except Exception as exc:
        return {"success": False, "error": str(exc)}
    if not isinstance(authority_request, dict):
        return {"success": False, "error": "authority request not found"}
    if str(authority_request.get("permission_id") or "") != "pack.approve":
        return {"success": True, "skipped": True, "reason": "not a pack approval"}
    resource = authority_request.get("resource") if isinstance(authority_request.get("resource"), dict) else {}
    if str(resource.get("kind") or "") != PACK_AUTHORITY_RESOURCE_KIND:
        return {"success": True, "skipped": True, "reason": "not a defaultspack pack request"}
    pack_request_id = str(resource.get("pack_request_id") or "").strip()
    if not pack_request_id:
        return {"success": False, "error": "pack request id is missing"}

    manager = get_extension_manager()
    normalized = str(decision or "").strip().lower()
    if normalized == "approve":
        result = manager.approve_request(
            request_id=pack_request_id,
            reviewer=reviewer,
            decision_notes=notes,
        )
    elif normalized == "deny":
        result = manager.reject_request(
            request_id=pack_request_id,
            reviewer=reviewer,
            reason=notes or "denied by mobile authority",
        )
    else:
        return {"success": False, "error": f"unsupported decision: {decision}"}
    if isinstance(result, dict) and result.get("error"):
        return {"success": False, **result}
    return {"success": True, "pack_request": result}
