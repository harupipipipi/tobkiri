"""Approve, deny, and inspect authority requests."""

from blocks._common import error, ok


def _authority_service():
    from core_runtime.legacy_runtime_removed import removed_authority_service

    return removed_authority_service()


def _failed(result, default_code="AUTHORITY_REQUEST_FAILED"):
    response = error(str(result.get("error") or "authority request failed"), code=default_code)
    try:
        response["_http_status"] = int(result.get("status_code") or 400)
    except (TypeError, ValueError):
        response["_http_status"] = 400
    return response


def _optional_int(value):
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def run(input_data, context=None):
    del context
    payload = input_data if isinstance(input_data, dict) else {}
    action = str(payload.get("action") or "list").strip().lower()
    service = _authority_service()

    if action == "list":
        status = str(payload.get("status") or "all").strip() or "all"
        return ok(service.list_requests(status))

    request_id = str(payload.get("request_id") or payload.get("approval_request_id") or "").strip()
    if not request_id:
        response = error("'request_id' is required", code="INVALID_INPUT")
        response["_http_status"] = 400
        return response

    if action == "approve":
        config = payload.get("config") if isinstance(payload.get("config"), dict) else None
        related_permissions = payload.get("related_permissions")
        if not isinstance(related_permissions, list):
            related_permissions = []
        approval_kwargs = {
            "scope": str(payload.get("scope") or "once").strip() or "once",
            "config": config,
            "expires_in_seconds": _optional_int(payload.get("expires_in_seconds")),
        }
        if related_permissions:
            approval_kwargs["related_permissions"] = [str(item) for item in related_permissions]
        result = service.approve_request(request_id, **approval_kwargs)
        if not result.get("success"):
            return _failed(result, "AUTHORITY_APPROVAL_FAILED")
        return ok(result)

    if action == "deny":
        result = service.deny_request(
            request_id,
            reason=str(payload.get("reason") or ""),
            persist=bool(payload.get("persist")),
        )
        if not result.get("success"):
            return _failed(result, "AUTHORITY_DENY_FAILED")
        return ok(result)

    response = error("unsupported authority request action", code="INVALID_INPUT")
    response["_http_status"] = 400
    return response
