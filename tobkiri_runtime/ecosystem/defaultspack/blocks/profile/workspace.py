import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from blocks._common import error, ok
from core_runtime.resolved_profile_scope import persisted_resolved_profile
from core_runtime.profile_workspace import ProfileWorkspaceManager


def run(input_data, context):
    del context
    data = input_data if isinstance(input_data, dict) else {}
    plan = persisted_resolved_profile()
    if plan is None:
        return error("Pack v4 resolved Profile is not active", "PROFILE_NOT_ACTIVE")
    profile_id = str(data.get("profile_id") or plan.profile_id).strip()
    if profile_id != str(plan.profile_id):
        return error("requested Profile is not active", "PROFILE_NOT_ACTIVE")
    manager = ProfileWorkspaceManager()
    paths = manager.initialize_profile_workspace({"profile_id": profile_id})
    return ok(manager.payload_for_profile(paths.profile_id))
