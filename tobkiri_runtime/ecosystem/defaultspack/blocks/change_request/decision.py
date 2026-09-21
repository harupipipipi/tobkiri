from __future__ import annotations

from blocks._common import ok
from blocks.change_request._helpers import invalid_input_response, mutation_conflict_response, not_found_response, service, service_error_response
from domain.change_request.store import ChangeRequestIdempotencyConflict, ChangeRequestRevisionConflict


def run(input_data, context=None):
    del context
    input_data = input_data or {}
    try:
        return ok(service().submit_decision(str(input_data.get("id") or ""), input_data))
    except KeyError:
        return not_found_response()
    except (ChangeRequestRevisionConflict, ChangeRequestIdempotencyConflict) as exc:
        return mutation_conflict_response(exc)
    except ValueError as exc:
        return invalid_input_response(exc)
    except Exception as exc:
        return service_error_response(exc)
