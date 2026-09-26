import os
import sys

_PACK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DEFAULTSPACK_ROOT = os.path.join(os.path.dirname(_PACK_ROOT), "defaultspack")
for _path in (_PACK_ROOT, _DEFAULTSPACK_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from blocks._common import error, ok
from ecosystem.rumi_operations_team_pack.domain.agent.mimo_coding_company import MimoCodingCompanyRuntime


def _truthy(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return False


def run(input_data, context):
    payload = input_data if isinstance(input_data, dict) else {}
    recover_scheduled_approvals = _truthy(payload.get("recover_scheduled_approvals"))
    include_desktop_monitoring = _truthy(payload.get("include_desktop_monitoring"))
    try:
        return ok(
            MimoCodingCompanyRuntime().status(
                recover_scheduled_approvals=recover_scheduled_approvals,
                sync_observability=True,
                include_desktop_monitoring=include_desktop_monitoring,
            )
        )
    except Exception as exc:
        return error("MiMo coding company status failed: " + str(exc), "MIMO_CODING_COMPANY_ERROR")
