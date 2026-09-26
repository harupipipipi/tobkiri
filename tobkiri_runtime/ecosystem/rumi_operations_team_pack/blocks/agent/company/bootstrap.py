import os
import sys

_PACK_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
_DEFAULTSPACK_ROOT = os.path.join(os.path.dirname(_PACK_ROOT), "defaultspack")
for _path in (_PACK_ROOT, _DEFAULTSPACK_ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from blocks._common import ok, error
from ecosystem.rumi_operations_team_pack.domain.agent.operations_company import OperationsCompanyRuntime, MODEL_ALLOWLIST


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict", "INVALID_INPUT")
    start_nonstop = input_data.get("start_nonstop", True)
    heartbeat_minutes = input_data.get("heartbeat_minutes", 15)
    model = input_data.get("model")
    if model is not None:
        model = str(model).strip()
        if model and model not in MODEL_ALLOWLIST:
            return error("model is not allowed for operations company: " + model, "MODEL_NOT_ALLOWED")
    try:
        heartbeat_minutes = int(heartbeat_minutes)
    except Exception:
        return error("heartbeat_minutes must be an integer", "INVALID_INPUT")
    try:
        status = OperationsCompanyRuntime().bootstrap(
            start_nonstop=bool(start_nonstop),
            heartbeat_minutes=heartbeat_minutes,
            model=model or None,
        )
        return ok(status)
    except Exception as exc:
        return error("operations company bootstrap failed: " + str(exc), "OPERATIONS_COMPANY_ERROR")
