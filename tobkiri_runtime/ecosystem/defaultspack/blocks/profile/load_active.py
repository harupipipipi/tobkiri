import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from core_runtime.resolved_profile_scope import persisted_resolved_profile


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    plan = persisted_resolved_profile()
    if plan is None:
        return error("Pack v4 resolved Profile is not active", "PROFILE_NOT_ACTIVE")
    requested = str(data.get("profile_id") or plan.profile_id).strip()
    if requested != str(plan.profile_id):
        return error("requested Profile is not active", "PROFILE_NOT_ACTIVE")
    return ok({
        "version": 4,
        "profile_id": str(plan.profile_id),
        "profile_revision": str(plan.profile_revision),
        "plan_hash": str(plan.plan_hash),
        "effective_pack_set": list(plan.effective_pack_set),
    })
