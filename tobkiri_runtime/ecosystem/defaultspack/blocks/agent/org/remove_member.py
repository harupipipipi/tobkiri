"""
blocks/agent/org/remove_member.py — メンバー削除ブロック

DELETE /api/agent/org/{id}/members/{agent_id}

input_data:
    id       : str (必須) 組織 ID
    agent_id : str (必須) エージェント ID
"""


from blocks._common import ok, error
from domain.agent.org_manager import OrgManager


def run(input_data, context):
    if not isinstance(input_data, dict):
        return error("input_data must be a dict")

    org_id = input_data.get("id")
    if not org_id:
        return error("id (org_id) is required")

    agent_id = input_data.get("agent_id")
    if not agent_id:
        return error("agent_id is required")

    manager = OrgManager()
    result = manager.remove_member(org_id, agent_id)
    if result is None:
        return error("organization not found: " + str(org_id))
    if result is False:
        return error("agent not found in organization: " + str(agent_id))

    return ok({"org_id": org_id, "agent_id": agent_id, "removed": True})
