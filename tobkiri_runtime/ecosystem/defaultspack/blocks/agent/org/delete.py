"""
blocks/agent/org/delete.py — 組織削除ブロック

DELETE /api/agent/org/{id}

input_data:
    id : str (必須) 組織 ID
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id is required")

    manager = OrgManager()
    deleted = manager.delete_org(org_id)
    if not deleted:
        return error("organization not found: " + str(org_id))

    return ok({"org_id": org_id, "deleted": True})
