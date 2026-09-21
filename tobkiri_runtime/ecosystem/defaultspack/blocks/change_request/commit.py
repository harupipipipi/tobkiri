from __future__ import annotations

import os

from blocks._common import error, ok
from blocks.coding._approval import approval_invalid_response, approval_required, is_server_approved
from blocks.change_request._helpers import invalid_input_response, mutation_conflict_response, not_found_response, service, service_error_response
from domain.change_request.store import ChangeRequestIdempotencyConflict, ChangeRequestRevisionConflict
from domain.safety.audit import record_attempt, record_execution, record_failure


def _commit_enabled() -> bool:
    value = str(os.environ.get("RUMI_REVIEW_ENABLE_COMMIT") or "").strip().lower()
    return value in {"1", "true", "yes", "on", "enabled"}


def run(input_data, context=None):
    input_data = input_data or {}
    cr_id = str(input_data.get("id") or "").strip()
    operation = "coding.change_request.commit"
    record_attempt(operation, "high", {"id": cr_id, "message": input_data.get("message")})
    if not _commit_enabled():
        record_failure(operation, "high", "phase1_review_only", {"id": cr_id})
        return ok(
            {
                "committed": False,
                "blocked": True,
                "reason": "phase1_review_only",
                "display_summary": "Rumi Review Phase 1 is review-only; commit is disabled by default.",
            }
        )
    if not is_server_approved(context, operation, input_data):
        invalid = approval_invalid_response(operation, input_data, error)
        if invalid:
            return invalid
        return ok(approval_required(operation, "high", args=input_data, id=cr_id, message=input_data.get("message")))
    try:
        result = service().commit(cr_id, input_data)
        if result.get("committed"):
            record_execution(operation, "high", {"id": cr_id}, commit_hash=(result.get("commit") or {}).get("commit_hash"))
        else:
            record_failure(operation, "high", str(result.get("reason") or "blocked"), {"id": cr_id})
        return ok(result)
    except KeyError:
        return not_found_response()
    except (ChangeRequestRevisionConflict, ChangeRequestIdempotencyConflict) as exc:
        record_failure(operation, "high", str(exc), {"id": cr_id})
        return mutation_conflict_response(exc)
    except ValueError as exc:
        return invalid_input_response(exc)
    except Exception as exc:
        record_failure(operation, "high", str(exc), {"id": cr_id})
        return service_error_response(exc)
