from blocks._common import ok, error
from domain.company.supervisor import CompanySupervisor

from ._helpers import company_id_from, invalid, require_dict


def run(input_data, context, *, settings_owner=None):
    if require_dict(input_data) is None:
        return invalid("input_data must be a dict")
    company_id = company_id_from(input_data)
    if not company_id:
        return invalid("company_id is required")
    stale_after_seconds = input_data.get("stale_after_seconds", 600)
    if not isinstance(stale_after_seconds, int) or stale_after_seconds < 1:
        stale_after_seconds = 600
    auto_dispatch = bool(input_data.get("auto_dispatch", False))
    auto_summarize = bool(input_data.get("auto_summarize", False))
    auto_mark_stale = bool(input_data.get("auto_mark_stale", False))
    try:
        return ok(
            CompanySupervisor(settings_owner=settings_owner).tick(
                company_id,
                stale_after_seconds=stale_after_seconds,
                auto_dispatch=auto_dispatch,
                auto_summarize=auto_summarize,
                auto_mark_stale=auto_mark_stale,
            )
        )
    except Exception as exc:
        return error("company supervisor tick failed: " + str(exc), "COMPANY_SUPERVISOR_ERROR")
