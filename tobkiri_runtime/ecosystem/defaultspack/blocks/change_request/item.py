from __future__ import annotations

from blocks._common import error, ok
from blocks.change_request._helpers import invalid_input_response, mutation_conflict_response, not_found_response, service, service_error_response
from domain.change_request.store import ChangeRequestIdempotencyConflict, ChangeRequestRevisionConflict


def run(input_data, context=None):
    del context
    input_data = input_data or {}
    cr_id = str(input_data.get("id") or "").strip()
    method = str(input_data.get("_method") or "GET").upper()
    change_requests = service()
    try:
        if method == "GET":
            record = change_requests.get(cr_id)
            if record is None:
                return not_found_response()
            return ok(record)
        if method == "PATCH":
            return ok(change_requests.update_metadata(cr_id, input_data))
        return error("unsupported method", code="METHOD_NOT_ALLOWED")
    except KeyError:
        return not_found_response()
    except (ChangeRequestRevisionConflict, ChangeRequestIdempotencyConflict) as exc:
        return mutation_conflict_response(exc)
    except ValueError as exc:
        return invalid_input_response(exc)
    except Exception as exc:
        return service_error_response(exc)
