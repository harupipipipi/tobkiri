from __future__ import annotations

from blocks._common import error, ok
from blocks.change_request._helpers import invalid_input_response, mutation_conflict_response, not_found_response, service, service_error_response
from domain.change_request.store import ChangeRequestIdempotencyConflict, ChangeRequestRevisionConflict


def run(input_data, context=None):
    del context
    input_data = input_data or {}
    cr_id = str(input_data.get("id") or "").strip()
    comment_id = str(input_data.get("comment_id") or input_data.get("thread_id") or "").strip()
    method = str(input_data.get("_method") or ("PATCH" if comment_id else "GET")).upper()
    change_requests = service()
    try:
        if method == "GET":
            record = change_requests.get(cr_id)
            if record is None:
                return not_found_response()
            if comment_id:
                comments = record.get("comments") if isinstance(record.get("comments"), list) else []
                for comment in comments:
                    if isinstance(comment, dict) and comment.get("id") == comment_id:
                        return ok({"comment": comment, "change_request": record})
                return not_found_response("review comment not found", code="CHANGE_REQUEST_COMMENT_NOT_FOUND")
            return ok(
                {
                    "change_request": record,
                    "comments": record.get("comments") or [],
                    "review_threads": record.get("review_threads") or [],
                }
            )
        if method == "POST":
            return ok(change_requests.add_comment(cr_id, input_data))
        if method in {"PATCH", "PUT"}:
            if not comment_id:
                return error("'comment_id' is required", code="INVALID_INPUT")
            return ok(change_requests.update_comment(cr_id, comment_id, input_data))
        return error("unsupported method", code="METHOD_NOT_ALLOWED")
    except KeyError:
        return not_found_response("change request or review comment not found")
    except (ChangeRequestRevisionConflict, ChangeRequestIdempotencyConflict) as exc:
        return mutation_conflict_response(exc)
    except ValueError as exc:
        return invalid_input_response(exc)
    except Exception as exc:
        return service_error_response(exc)
