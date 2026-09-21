from __future__ import annotations

from blocks._common import error
from domain.change_request import ChangeRequestService
from domain.change_request.store import ChangeRequestIdempotencyConflict, ChangeRequestRevisionConflict


def service() -> ChangeRequestService:
    return ChangeRequestService()


def not_found_response(
    message: str = "change request not found",
    *,
    code: str = "CHANGE_REQUEST_NOT_FOUND",
):
    result = error(message, code=code)
    result["_http_status"] = 404
    return result


def invalid_input_response(exc: ValueError):
    return error(str(exc), code="INVALID_INPUT")


def mutation_conflict_response(exc: ValueError):
    if isinstance(exc, ChangeRequestIdempotencyConflict):
        code = exc.code
    elif isinstance(exc, ChangeRequestRevisionConflict):
        code = exc.code
    else:
        code = "CHANGE_REQUEST_CONFLICT"
    result = error(str(exc), code=code)
    result["_http_status"] = 409
    return result


def service_error_response(exc: Exception):
    return error(str(exc), code="CHANGE_REQUEST_ERROR")
